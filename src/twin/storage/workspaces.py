from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from twin.paths import TwinPaths
from twin.storage.atomic import write_bytes, write_text
from twin.storage.events import append_jsonl, event_record, now_utc
from twin.storage.locks import exclusive_lock
from twin.yaml_codec import dump_yaml


class WorkspaceStore:
    def __init__(self, paths: TwinPaths) -> None:
        self.paths = paths

    def create(self, request: str, repo_root: Path, route: str) -> str:
        canonical_repo = repo_root.expanduser().resolve()
        if not canonical_repo.is_dir():
            raise ValueError(f"target repository does not exist: {canonical_repo}")
        workspace_id = self._new_workspace_id(request)
        workspace = self.paths.workspaces / workspace_id
        workspace.mkdir(parents=True, exist_ok=False)
        (workspace / "artifacts").mkdir()
        state: dict[str, object] = {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "status": "awaiting_plan",
            "state_revision": 0,
            "supervisor_route": route,
            "pending_action": None,
            "current_run_id": None,
            "current_item_id": None,
            "terminal_summary": None,
        }
        self._write_json(workspace / "meta.json", {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "created_at": now_utc(),
            "repo_root": str(canonical_repo),
        })
        goal_text = request.strip() or "Untitled goal"
        dump_yaml(workspace / "goal.yaml", {
            "schema_version": 1,
            "id": workspace_id,
            "one_liner": goal_text,
            "core_goal": goal_text,
            "acceptance_criteria": [],
            "non_goals": [],
        })
        dump_yaml(workspace / "plan.yaml", {
            "schema_version": 1,
            "goal_id": workspace_id,
            "items": [],
            "verification": [],
        })
        self._write_json(workspace / "state.json", state)
        pointer = self.paths.active_workspaces / self._project_key(canonical_repo)
        write_text(pointer, workspace_id + "\n")
        self.append_event(workspace, {"event": "workspace_created", "details": {}})
        return workspace_id

    def resolve(self, ref: str | None, project_root: Path) -> Path:
        if ref is None:
            pointer = self.paths.active_workspaces / self._project_key(project_root.expanduser().resolve())
            if not pointer.is_file():
                raise ValueError("workspace is required")
            ref = pointer.read_text(encoding="utf-8").strip()
            if not ref:
                raise ValueError("active workspace pointer is empty")
        candidate = Path(ref).expanduser()
        if candidate.is_absolute():
            workspace = candidate.resolve()
        else:
            if candidate.name != ref or not ref:
                raise ValueError("workspace reference must be an ID")
            workspace = (self.paths.workspaces / ref).resolve()
        self._require_workspace_path(workspace)
        if not workspace.is_dir():
            raise ValueError(f"workspace does not exist: {workspace}")
        return self._display_path(workspace)

    def load_state(self, workspace: Path) -> dict[str, object]:
        workspace = self._workspace_path(workspace)
        value = self._read_json(workspace / "state.json")
        if not isinstance(value, dict):
            raise ValueError("state must be an object")
        return value

    def replace_state(
        self, workspace: Path, expected_revision: int, value: dict[str, object]
    ) -> None:
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            current = self.load_state(workspace)
            revision = current.get("state_revision")
            if revision != expected_revision:
                raise ValueError(
                    f"state revision mismatch: expected {expected_revision}, found {revision}"
                )
            if value.get("workspace_id") != current.get("workspace_id"):
                raise ValueError("state workspace_id mismatch")
            value = dict(value)
            next_revision = expected_revision + 1
            value["state_revision"] = next_revision
            self._write_json(workspace / "state.json", value)
            workspace_id = value["workspace_id"]
            if not isinstance(workspace_id, str):
                raise ValueError("state workspace_id mismatch")
            append_jsonl(
                workspace / "events.jsonl",
                event_record(
                    workspace_id=workspace_id,
                    state_revision=next_revision,
                    event={"event": "state_replaced", "details": {}},
                ),
            )

    def append_event(self, workspace: Path, event: dict[str, object]) -> None:
        workspace = self._workspace_path(workspace)
        with self._lock(workspace):
            state = self.load_state(workspace)
            workspace_id = state.get("workspace_id")
            revision = state.get("state_revision")
            if not isinstance(workspace_id, str) or not isinstance(revision, int):
                raise ValueError("invalid workspace state")
            append_jsonl(
                workspace / "events.jsonl",
                event_record(workspace_id=workspace_id, state_revision=revision, event=event),
            )

    def write_artifact(
        self, workspace: Path, relative: str, body: bytes
    ) -> dict[str, object]:
        workspace = self._workspace_path(workspace)
        target = self._artifact_path(workspace, relative)
        write_bytes(target, body)
        metadata: dict[str, object] = {
            "relative": str(target.relative_to(workspace)),
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
        }
        self.append_event(workspace, {"event": "artifact_written", "details": metadata})
        return metadata

    def _new_workspace_id(self, request: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")[:48] or "workspace"
        return f"{stamp}-{slug}-{secrets.token_hex(4)}"

    def _project_key(self, repo_root: Path) -> str:
        return hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()

    def _lock(self, workspace: Path):
        return exclusive_lock(self.paths.locks / f"{workspace.name}.lock")

    def _workspace_path(self, workspace: Path) -> Path:
        resolved = workspace.expanduser().resolve()
        self._require_workspace_path(resolved)
        if not resolved.is_dir():
            raise ValueError(f"workspace does not exist: {resolved}")
        return resolved

    def _require_workspace_path(self, path: Path) -> None:
        root = self.paths.workspaces.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"workspace path is outside store root: {path}") from exc

    def _artifact_path(self, workspace: Path, relative: str) -> Path:
        candidate = Path(relative)
        if not relative or candidate.is_absolute():
            raise ValueError("artifact path must be relative")
        target = (workspace / candidate).resolve()
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("artifact path escapes workspace") from exc
        if target == workspace or target.name in {
            "meta.json", "goal.yaml", "plan.yaml", "state.json", "events.jsonl"
        }:
            raise ValueError("artifact path is reserved")
        return target

    @staticmethod
    def _display_path(path: Path) -> Path:
        """Preserve macOS's conventional /var spelling for user-facing refs."""
        rendered = str(path)
        if rendered.startswith("/private/var/"):
            return Path(rendered[len("/private"):])
        return path

    @staticmethod
    def _read_json(path: Path) -> object:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
