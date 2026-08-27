from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from twin.domain.evidence import evidence_exists
from twin.domain.plan import completion_gaps, validate_ready_plan
from twin.resources import ResourceCatalog
from twin.schema import validate_document
from twin.yaml_codec import decode_yaml


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class WorkspaceSnapshot:
    meta: dict[str, object]
    state: dict[str, object]
    goal: dict[str, object]
    plan: dict[str, object]
    events: tuple[dict[str, object], ...]
    artifacts: dict[str, dict[str, object]]
    run_requests: dict[str, dict[str, object]]
    run_results: dict[str, dict[str, object]]
    run_evidence: dict[str, dict[str, object]]


def validate_workspace_integrity(
    workspace: Path, resources: ResourceCatalog
) -> WorkspaceSnapshot:
    workspace = workspace.resolve()
    meta = _json_document(workspace / "meta.json", "meta", resources)
    state, state_body = _json_document_with_body(
        workspace / "state.json", "state", resources
    )
    goal = _yaml_document(workspace / "goal.yaml", "goal", resources)
    plan = _yaml_document(workspace / "plan.yaml", "plan", resources)
    workspace_id = workspace.name
    if meta.get("workspace_id") != workspace_id or state.get("workspace_id") != workspace_id:
        raise ValueError("workspace identity mismatch")
    if goal.get("id") != workspace_id or plan.get("goal_id") != workspace_id:
        raise ValueError("goal/plan workspace identity mismatch")
    repo_root = meta.get("repo_root")
    repository_identity = meta.get("repository_identity")
    if not isinstance(repo_root, str) or not isinstance(repository_identity, str):
        raise ValueError("invalid meta")
    canonical_repo_root = str(Path(repo_root).expanduser().resolve())
    expected_identity = hashlib.sha256(canonical_repo_root.encode("utf-8")).hexdigest()
    if repo_root != canonical_repo_root:
        raise ValueError("repository identity mismatch")
    if repository_identity != expected_identity or state.get("repository_identity") != repository_identity:
        raise ValueError("repository identity mismatch")
    status = state.get("status")
    if status != "awaiting_plan":
        ready_errors = validate_ready_plan(goal, plan)
        if ready_errors:
            raise ValueError("invalid ready workspace: " + "; ".join(ready_errors))

    events = _event_stream(
        workspace / "events.jsonl",
        workspace_id,
        state,
        hashlib.sha256(state_body).hexdigest(),
        resources,
    )
    artifacts = _artifact_index(workspace, events)
    requests, results, evidence = _run_records(
        workspace,
        artifacts,
        resources,
        workspace_id=workspace_id,
        repo_root=repo_root,
        repository_identity=repository_identity,
        plan=plan,
    )
    _validate_state_invariants(state, requests, results, evidence)
    if state.get("status") == "accepted_done" and completion_gaps(
        goal,
        plan,
        lambda entry: evidence_exists(
            workspace, entry, recorded_artifacts=artifacts
        ),
    ):
        raise ValueError("accepted completion invariant mismatch")
    return WorkspaceSnapshot(
        meta=meta,
        state=state,
        goal=goal,
        plan=plan,
        events=tuple(events),
        artifacts=artifacts,
        run_requests=requests,
        run_results=results,
        run_evidence=evidence,
    )


def _json_document(
    path: Path, schema_name: str, resources: ResourceCatalog
) -> dict[str, object]:
    value, _ = _json_document_with_body(path, schema_name, resources)
    return value


def _json_document_with_body(
    path: Path, schema_name: str, resources: ResourceCatalog
) -> tuple[dict[str, object], bytes]:
    try:
        body = _read_regular(path)
        value = json.loads(body.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {schema_name}") from exc
    errors = validate_document(value, schema_name, resources)
    if not isinstance(value, dict) or errors:
        raise ValueError(f"invalid {schema_name}: {'; '.join(errors)}")
    return value, body


def _yaml_document(
    path: Path, schema_name: str, resources: ResourceCatalog
) -> dict[str, object]:
    try:
        value = decode_yaml(_read_regular(path).decode("utf-8"), source=str(path))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid {schema_name}") from exc
    errors = validate_document(value, schema_name, resources)
    if errors:
        raise ValueError(f"invalid {schema_name}: {'; '.join(errors)}")
    return value


def _event_stream(
    path: Path,
    workspace_id: str,
    state: dict[str, object],
    state_sha256: str,
    resources: ResourceCatalog,
) -> list[dict[str, object]]:
    try:
        lines = _read_regular(path).decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("invalid event stream") from exc
    events: list[dict[str, object]] = []
    revisions: list[int] = []
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid event") from exc
        if isinstance(event, dict) and event.get("workspace_id") != workspace_id:
            raise ValueError("event workspace_id mismatch")
        errors = validate_document(event, "event", resources)
        if not isinstance(event, dict) or errors:
            raise ValueError("invalid event: " + "; ".join(errors))
        revision = event.get("state_revision")
        assert isinstance(revision, int)
        revisions.append(revision)
        events.append(event)
    state_revision = state.get("state_revision")
    if not isinstance(state_revision, int) or not revisions:
        raise ValueError("event revision mismatch")
    if revisions != sorted(revisions) or max(revisions) != state_revision:
        raise ValueError("event revision mismatch")
    if sum(event.get("event") == "workspace_created" for event in events) != 1:
        raise ValueError("event revision mismatch")
    state_bindings: dict[int, str] = {}
    for event in events:
        name = event.get("event")
        revision = event.get("state_revision")
        if name not in {"workspace_created", "state_replaced"}:
            continue
        details = event.get("details")
        digest = details.get("sha256") if isinstance(details, dict) else None
        if (
            not isinstance(revision, int)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or revision in state_bindings
        ):
            raise ValueError("state event binding mismatch")
        state_bindings[revision] = digest
    for revision in range(1, state_revision + 1):
        replaced = sum(
            event.get("event") == "state_replaced" and event.get("state_revision") == revision
            for event in events
        )
        if replaced != 1:
            raise ValueError("event revision mismatch")
    if state_bindings.get(state_revision) != state_sha256:
        raise ValueError("state event binding mismatch")
    return events


def _artifact_index(
    workspace: Path, events: list[dict[str, object]]
) -> dict[str, dict[str, object]]:
    recorded: dict[str, dict[str, object]] = {}
    for event in events:
        if event.get("event") != "artifact_written":
            continue
        details = event.get("details")
        relative = details.get("relative") if isinstance(details, dict) else None
        sha256 = details.get("sha256") if isinstance(details, dict) else None
        size = details.get("bytes") if isinstance(details, dict) else None
        if (
            not isinstance(relative, str)
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
            or not isinstance(size, int)
            or relative in recorded
            or not _artifact_relative(relative)
        ):
            raise ValueError("invalid artifact audit record")
        recorded[relative] = {"relative": relative, "sha256": sha256, "bytes": size}

    present: set[str] = set()
    for root_name in ("artifacts", "runs"):
        root = workspace / root_name
        if root.is_symlink():
            raise ValueError("artifact symlink is not allowed")
        if not root.exists():
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in directories:
                if (current_path / name).is_symlink():
                    raise ValueError("artifact symlink is not allowed")
            for name in files:
                path = current_path / name
                if path.is_symlink():
                    raise ValueError("artifact symlink is not allowed")
                relative = str(path.relative_to(workspace))
                present.add(relative)
                metadata = recorded.get(relative)
                if metadata is None:
                    raise ValueError(f"untracked artifact: {relative}")
                body = _read_regular(path)
                if (
                    metadata["bytes"] != len(body)
                    or metadata["sha256"] != hashlib.sha256(body).hexdigest()
                ):
                    raise ValueError(f"artifact integrity mismatch: {relative}")
    missing = sorted(set(recorded) - present)
    if missing:
        raise ValueError(f"artifact integrity mismatch: {missing[0]}")
    return recorded


def _run_records(
    workspace: Path,
    artifacts: dict[str, dict[str, object]],
    resources: ResourceCatalog,
    *,
    workspace_id: str,
    repo_root: str,
    repository_identity: str,
    plan: dict[str, object],
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
    dict[str, dict[str, object]],
]:
    requests: dict[str, dict[str, object]] = {}
    results: dict[str, dict[str, object]] = {}
    evidence_records: dict[str, dict[str, object]] = {}
    plan_items = {
        item.get("id")
        for item in plan.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for relative in sorted(artifacts):
        parts = Path(relative).parts
        if len(parts) != 3 or parts[0] != "runs":
            continue
        run_id, name = parts[1], parts[2]
        schema_name = {
            "request.json": "run-request",
            "result.json": "run-result",
            "evidence.json": "run-evidence",
        }.get(name)
        if schema_name is None:
            raise ValueError(f"unrecognized run artifact: {relative}")
        value = _json_document(workspace / relative, schema_name, resources)
        if value.get("run_id") != run_id:
            raise ValueError("run artifact identity mismatch")
        if name == "request.json":
            if value.get("workspace_id") != workspace_id:
                raise ValueError("run request workspace mismatch")
            if (
                value.get("repository_root") != repo_root
                or value.get("repository_identity") != repository_identity
            ):
                raise ValueError("run request repository mismatch")
            if value.get("item_id") not in plan_items:
                raise ValueError("run request item mismatch")
        target = {
            "request.json": requests,
            "result.json": results,
            "evidence.json": evidence_records,
        }[name]
        target[run_id] = value
    for run_id, evidence in evidence_records.items():
        request = evidence.get("request")
        result = evidence.get("result")
        if (
            requests.get(run_id) is None
            or results.get(run_id) is None
            or not _metadata_matches(request, artifacts.get(f"runs/{run_id}/request.json"))
            or not _metadata_matches(result, artifacts.get(f"runs/{run_id}/result.json"))
        ):
            raise ValueError("run evidence reference mismatch")
        if evidence.get("item_id") != requests[run_id].get("item_id"):
            raise ValueError("run evidence item mismatch")
        result_status = (
            "completed"
            if results[run_id].get("returncode") == 0
            and results[run_id].get("timed_out") is False
            else "failed"
        )
        if evidence.get("status") != result_status:
            raise ValueError("run evidence status mismatch")
        entries = evidence.get("evidence")
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or not _metadata_matches(entry, artifacts.get(str(entry.get("relative"))))
            for entry in entries
        ):
            raise ValueError("run evidence reference mismatch")
    for run_id in results:
        if run_id not in evidence_records:
            raise ValueError("run evidence reference mismatch")
    return requests, results, evidence_records


def _validate_state_invariants(
    state: dict[str, object],
    requests: dict[str, dict[str, object]],
    results: dict[str, dict[str, object]],
    evidence: dict[str, dict[str, object]],
) -> None:
    status = state.get("status")
    pending = state.get("pending_action")
    revision = state.get("state_revision")
    route = state.get("supervisor_route")
    repository_identity = state.get("repository_identity")
    run_id = state.get("current_run_id")
    item_id = state.get("current_item_id")
    expected_pending = {
        "awaiting_plan": "author_plan",
        "review_required": "review",
    }.get(str(status))
    initial_draft = status == "awaiting_plan" and revision == 0 and pending is None
    if expected_pending is None:
        if pending is not None:
            raise ValueError("pending action invariant mismatch")
    elif not initial_draft:
        if not isinstance(pending, dict) or pending.get("kind") != expected_pending:
            raise ValueError("pending action invariant mismatch")
        if (
            pending.get("state_revision") != revision
            or pending.get("route") != route
            or pending.get("repository_identity") != repository_identity
        ):
            raise ValueError("pending action invariant mismatch")
    if status == "worker_running":
        if not isinstance(run_id, str) or not isinstance(item_id, str):
            raise ValueError("worker run invariant mismatch")
        if run_id not in requests or run_id in results or run_id in evidence:
            raise ValueError("worker run invariant mismatch")
        if requests[run_id].get("item_id") != item_id:
            raise ValueError("worker run item mismatch")
    if status in {"review_required", "needs_human", "accepted_done", "failed"}:
        if (
            not isinstance(run_id, str)
            or not isinstance(item_id, str)
            or run_id not in requests
            or run_id not in evidence
        ):
            raise ValueError("worker run invariant mismatch")
        if requests[run_id].get("item_id") != item_id:
            raise ValueError("worker run item mismatch")
        if isinstance(pending, dict) and pending.get("run_id") != run_id:
            raise ValueError("pending action invariant mismatch")
    if status == "ready" and (run_id is not None or item_id is not None):
        raise ValueError("worker run invariant mismatch")


def _metadata_matches(value: object, expected: object) -> bool:
    return isinstance(value, dict) and isinstance(expected, dict) and all(
        value.get(key) == expected.get(key) for key in ("relative", "sha256", "bytes")
    )


def _artifact_relative(relative: str) -> bool:
    path = Path(relative)
    return (
        not path.is_absolute()
        and path.parts[:1] in {("artifacts",), ("runs",)}
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required regular file is missing: {path}")
    return path.read_bytes()
