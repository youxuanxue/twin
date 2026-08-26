from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Mapping

from twin.domain.actions import command_descriptor, issue_action, validate_submission
from twin.domain.evidence import evidence_exists
from twin.domain.integrity import WorkspaceSnapshot
from twin.domain.plan import (
    apply_updates,
    choose_next_item,
    completion_gaps,
    materialize_run_evidence,
    validate_plan,
    validate_ready_plan,
)
from twin.domain.state import require_mutable, transition
from twin.resources import ResourceCatalog
from twin.runtime.protocols import (
    WorkerRuntime,
    WorkerTurnRequest,
    WorkerTurnResult,
    WorkspaceIsolation,
)
from twin.schema import validate_document
from twin.storage.workspaces import WorkspaceStore
from twin.yaml_codec import decode_yaml, encode_yaml


class TwinService:
    def __init__(
        self,
        store: WorkspaceStore,
        runtime: WorkerRuntime | None = None,
        isolation: WorkspaceIsolation | None = None,
        *,
        timeout_seconds: float = 300,
        resources: ResourceCatalog | None = None,
        worker_provider: str = "codex",
        runtime_adapter: str = "injected",
        runtime_config_digest: str = "injected",
        provider_contract_version: int = 1,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.isolation = isolation
        self.timeout_seconds = timeout_seconds
        self.resources = resources or ResourceCatalog()
        self.store.bind_integrity(self.resources)
        self.worker_provider = worker_provider
        self.runtime_adapter = runtime_adapter
        self.runtime_config_digest = runtime_config_digest
        self.provider_contract_version = provider_contract_version

    def start(self, goal: str, repo_root: Path, route: str) -> dict[str, object]:
        workspace_id = self.store.create(goal, repo_root, route)
        workspace = self.store.resolve(workspace_id, repo_root)
        snapshot = self.store.inspect(workspace, self.resources)
        state = dict(snapshot.state)
        repository_root = Path(str(snapshot.meta["repo_root"]))
        action = issue_action(
            state,
            kind="author_plan",
            workspace=workspace_id,
            route=route,
            repository_root=repository_root,
            context={
                "goal_request": goal,
                "goal": snapshot.goal,
                "plan": snapshot.plan,
            },
            expected_output={
                "payload": {
                    "format": "json",
                    "required": ["goal", "plan"],
                    "schema_paths": {
                        "goal": str(self.resources.schema("goal")),
                        "plan": str(self.resources.schema("plan")),
                    },
                },
            },
            next_argv=None,
        )
        self.store.commit_action(
            workspace,
            0,
            state,
            documents={},
            artifacts={},
            event={"event": "author_plan_issued", "details": {"route": route}},
            validate_current=lambda current: None,
        )
        return action

    def run(self, workspace_ref: str | None, repo_root: Path, route: str) -> dict[str, object]:
        workspace = self.store.resolve(workspace_ref, repo_root)
        with self.store.worker_runtime_lock(workspace):
            return self._run_locked(workspace=workspace, route=route)

    def _run_locked(self, *, workspace: Path, route: str) -> dict[str, object]:
        snapshot = self.store.inspect(workspace, self.resources)
        state = dict(snapshot.state)
        require_mutable(state)
        if state.get("supervisor_route") != route:
            raise ValueError("supervisor route mismatch")
        if state.get("status") == "worker_running":
            if state.get("pending_action") is not None:
                raise ValueError("invalid worker-running pending action")
            return self._resume_worker_runtime(workspace=workspace, route=route)
        if state.get("pending_action") is not None:
            raise ValueError("pending action")
        if state.get("status") != "ready":
            raise ValueError("workspace is not ready")
        if self.runtime is None:
            raise ValueError("worker runtime is not configured")
        item = choose_next_item(snapshot.plan)
        if item is None or not isinstance(item.get("id"), str):
            raise ValueError("no runnable plan item")
        transition(state, "worker_running")
        run_id = "run-" + secrets.token_hex(12)
        state["current_run_id"] = run_id
        state["current_item_id"] = item["id"]
        request = self._new_run_request(
            snapshot=snapshot,
            run_id=run_id,
            item_id=item["id"],
        )
        request_relative = f"runs/{run_id}/request.json"
        self.store.commit_action(
            workspace,
            self._revision(state),
            state,
            documents={},
            artifacts={request_relative: self._json_bytes(request)},
            event={
                "event": "worker_started",
                "details": {
                    "run_id": run_id,
                    "item_id": item["id"],
                    "provider": self.worker_provider,
                    "adapter": self.runtime_adapter,
                },
            },
            validate_current=lambda current: self._validate_current_worker_start(
                current, route
            ),
        )
        return self._resume_worker_runtime(workspace=workspace, route=route)

    def submit_plan(self, workspace_ref: str, route: str, revision: int, token: str, payload: dict[str, object]) -> dict[str, object]:
        workspace = self._resolve_submission(workspace_ref)
        state = self._state(workspace)
        require_mutable(state)
        validate_submission(state, kind="author_plan", route=route, revision=revision, token=token)
        goal, plan = self._validate_plan_payload(payload, str(state["workspace_id"]))
        transition(state, "ready")
        state["pending_action"] = None
        self.store.commit_action(
            workspace, revision, state,
            documents={"goal.yaml": encode_yaml(goal), "plan.yaml": encode_yaml(plan)},
            artifacts={}, event={"event": "plan_submitted", "details": {"route": route}},
            validate_current=lambda current: self._validate_current_submission(
                current, "author_plan", route, revision, token
            ),
        )
        return self._result(workspace, self._state(workspace))

    def submit_review(self, workspace_ref: str, route: str, revision: int, token: str, run_id: str, payload: dict[str, object]) -> dict[str, object]:
        workspace = self._resolve_submission(workspace_ref)
        state = self._state(workspace)
        require_mutable(state)
        validate_submission(state, kind="review", route=route, revision=revision, token=token, run_id=run_id)
        decision = payload.get("decision")
        if decision == "accepted":
            snapshot = self.store.inspect(workspace, self.resources)
            gaps = completion_gaps(
                snapshot.goal,
                snapshot.plan,
                lambda entry: evidence_exists(
                    workspace, entry, recorded_artifacts=snapshot.artifacts
                ),
            )
            if gaps:
                raise ValueError("missing evidence")
            transition(state, "accepted_done")
        elif decision == "changes_requested":
            transition(state, "ready")
            state["current_run_id"] = None
            state["current_item_id"] = None
        elif decision == "needs_human":
            transition(state, "needs_human")
        elif decision == "failed":
            transition(state, "failed")
        else:
            raise ValueError("invalid review decision")
        state["pending_action"] = None
        self.store.commit_action(
            workspace,
            revision,
            state,
            documents={},
            artifacts={},
            event={
                "event": "review_submitted",
                "details": {"route": route, "run_id": run_id, "status": state["status"]},
            },
            validate_current=lambda current: self._validate_current_submission(
                current, "review", route, revision, token, run_id
            ),
        )
        return self._result(workspace, self._state(workspace))

    def respond(self, workspace_ref: str | None, repo_root: Path, answer: str) -> dict[str, object]:
        workspace = self.store.resolve(workspace_ref, repo_root)
        state = self._state(workspace)
        require_mutable(state)
        if state.get("status") != "needs_human":
            raise ValueError("workspace is not awaiting human response")
        body = answer.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        artifact: dict[str, object] = {
            "relative": f"artifacts/human/{digest}.txt",
            "sha256": digest,
            "bytes": len(body),
        }
        transition(state, "ready")
        state["current_run_id"] = None
        state["current_item_id"] = None
        self.store.commit_action(
            workspace, self._revision(state), state,
            documents={}, artifacts={str(artifact["relative"]): body},
            event={
                "event": "human_response",
                "details": {"artifact": artifact["relative"], "length": len(body), "sha256": digest},
            },
            validate_current=self._validate_current_human_response,
        )
        result = self._result(workspace, self._state(workspace))
        result["artifact"] = artifact
        return result

    def handoff(self, workspace_ref: str, repo_root: Path, from_route: str, to_route: str) -> dict[str, object]:
        workspace = self.store.resolve(workspace_ref, repo_root)
        state = self._state(workspace)
        require_mutable(state)
        if state.get("pending_action") is not None:
            raise ValueError("pending action")
        if state.get("supervisor_route") != from_route:
            raise ValueError("supervisor route mismatch")
        state["supervisor_route"] = to_route
        self.store.commit_action(
            workspace, self._revision(state), state,
            documents={}, artifacts={},
            event={"event": "handoff", "details": {"from_route": from_route, "to_route": to_route}},
            validate_current=lambda current: self._validate_current_handoff(current, from_route),
        )
        return self._result(workspace, self._state(workspace))

    def status(self, workspace_ref: str | None, repo_root: Path) -> dict[str, object]:
        workspace = self.store.resolve(workspace_ref, repo_root)
        return self._result(workspace, self._state(workspace))

    def _validate_plan_payload(self, payload: dict[str, object], workspace_id: str) -> tuple[dict[str, object], dict[str, object]]:
        goal = payload.get("goal")
        plan = payload.get("plan")
        if not isinstance(goal, dict) or not isinstance(plan, dict):
            raise ValueError("plan payload requires goal and plan objects")
        goal = dict(goal)
        plan = dict(plan)
        goal["id"] = workspace_id
        plan["goal_id"] = workspace_id
        errors = (
            validate_document(goal, "goal", self.resources)
            + validate_document(plan, "plan", self.resources)
            + validate_ready_plan(goal, plan)
        )
        if choose_next_item(plan) is None:
            errors.append("plan requires at least one runnable item")
        if errors:
            raise ValueError("; ".join(errors))
        encoded_goal = encode_yaml(goal)
        encoded_plan = encode_yaml(plan)
        reloaded_goal = decode_yaml(encoded_goal.decode("utf-8"), source="goal.yaml")
        reloaded_plan = decode_yaml(encoded_plan.decode("utf-8"), source="plan.yaml")
        persisted_errors = (
            validate_document(reloaded_goal, "goal", self.resources)
            + validate_document(reloaded_plan, "plan", self.resources)
            + validate_ready_plan(reloaded_goal, reloaded_plan)
        )
        if choose_next_item(reloaded_plan) is None:
            persisted_errors.append("plan requires at least one runnable item")
        if persisted_errors or reloaded_goal != goal or reloaded_plan != plan:
            raise ValueError("persisted goal/plan failed round-trip validation")
        return reloaded_goal, reloaded_plan

    def _resolve_submission(self, workspace_ref: str) -> Path:
        # Submission APIs intentionally accept only IDs; the active pointer is not a valid authority.
        return self.store.resolve_submission(workspace_ref)

    def _state(self, workspace: Path) -> dict[str, object]:
        state = dict(self.store.inspect(workspace, self.resources).state)
        workspace_id = state.get("workspace_id")
        if workspace_id != workspace.name:
            raise ValueError("state workspace_id mismatch")
        return state

    @staticmethod
    def _validate_current_submission(
        state: dict[str, object], kind: str, route: str, revision: int, token: str,
        run_id: str | None = None,
    ) -> None:
        require_mutable(state)
        validate_submission(
            state, kind=kind, route=route, revision=revision, token=token, run_id=run_id
        )

    @staticmethod
    def _validate_current_human_response(state: dict[str, object]) -> None:
        require_mutable(state)
        if state.get("status") != "needs_human":
            raise ValueError("workspace is not awaiting human response")

    @staticmethod
    def _validate_current_handoff(state: dict[str, object], from_route: str) -> None:
        require_mutable(state)
        if state.get("pending_action") is not None:
            raise ValueError("pending action")
        if state.get("supervisor_route") != from_route:
            raise ValueError("supervisor route mismatch")

    @staticmethod
    def _revision(state: dict[str, object]) -> int:
        revision = state.get("state_revision")
        if not isinstance(revision, int):
            raise ValueError("invalid state revision")
        return revision

    def _result(self, workspace: Path, state: dict[str, object]) -> dict[str, object]:
        workspace_id = str(state["workspace_id"])
        status = state["status"]
        route = state["supervisor_route"]
        next_command = None
        if status in {"ready", "worker_running"}:
            if not isinstance(route, str):
                raise ValueError("invalid supervisor route")
            next_command = command_descriptor([
                "twin", "run", workspace_id, "--supervisor", route, "--json",
            ])
        return {
            "workspace": workspace_id,
            "status": status,
            "state_revision": state["state_revision"],
            "supervisor_route": route,
            "next_command": next_command,
        }

    def _run_worker_runtime(
        self, *, workspace: Path, route: str,
        request_payload: dict[str, object],
    ) -> dict[str, object]:
        workspace_id = str(request_payload["workspace_id"])
        run_id = str(request_payload["run_id"])
        item_id = str(request_payload["item_id"])
        repository_root = Path(str(request_payload["repository_root"])).expanduser().resolve()
        cwd = repository_root
        cleanup_result: bool | None = None
        cleanup_error: str | None = None
        prepared = False
        request = WorkerTurnRequest(
            prompt=str(request_payload["prompt"]),
            cwd=cwd,
            provider=str(request_payload["provider"]),
            session_id=str(request_payload.get("session_id") or ""),
            timeout_seconds=float(request_payload["timeout_seconds"]),
            environment=dict(request_payload.get("environment") or {}),
        )
        result: WorkerTurnResult | None = None
        if self.isolation is not None:
            try:
                cwd = self.isolation.prepare(cwd, workspace_id)
            except Exception as exc:
                result = WorkerTurnResult(
                    output_text=f"isolation prepare failed: {exc}",
                    returncode=1,
                    session_id=request.session_id,
                    events=({
                        "event": "failure",
                        "failure_kind": "isolation_prepare_failed",
                        "error": str(exc),
                    },),
                )
            else:
                prepared = True
                request = WorkerTurnRequest(
                    prompt=request.prompt,
                    cwd=cwd,
                    provider=request.provider,
                    session_id=request.session_id,
                    timeout_seconds=request.timeout_seconds,
                    environment=request.environment,
                )
        assert self.runtime is not None
        if result is None:
            try:
                result = self.runtime.run_turn(request)
            except Exception as exc:
                result = WorkerTurnResult(
                    output_text=f"runtime failed: {exc}",
                    returncode=1,
                    session_id=request.session_id,
                    events=({"event": "failure", "failure_kind": "runtime_exception", "error": str(exc)},),
                )
            if self.isolation is not None and prepared:
                try:
                    cleanup_result = self.isolation.cleanup(repository_root, workspace_id)
                except Exception as exc:
                    cleanup_result = False
                    cleanup_error = str(exc)
        return self._publish_worker_result(
            workspace=workspace,
            route=route,
            request=request,
            result=result,
            cleanup_result=cleanup_result,
            cleanup_error=cleanup_error,
            run_id=run_id,
            item_id=item_id,
            workspace_id=workspace_id,
        )

    def _worker_prompt(
        self,
        goal: dict[str, object],
        plan: dict[str, object],
        *,
        workspace_id: str,
        run_id: str,
        item_id: str,
        repository_root: str,
    ) -> str:
        persona = self.resources.persona("worker").read_text(encoding="utf-8")
        run_context = {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "item_id": item_id,
            "repository_root": repository_root,
        }
        materialized_plan = materialize_run_evidence(plan, item_id, run_id)
        return "\n\n".join((
            persona,
            "## Twin run context",
            json.dumps(run_context, ensure_ascii=False, indent=2, sort_keys=True),
            "## Twin worker submission contract",
            self.resources.schema("worker-submission").read_text(encoding="utf-8"),
            "## goal.yaml",
            encode_yaml(goal).decode("utf-8"),
            "## plan.yaml",
            encode_yaml(materialized_plan).decode("utf-8"),
        ))

    def _publish_worker_result(
        self,
        *,
        workspace: Path,
        route: str,
        run_id: str,
        item_id: str,
        workspace_id: str,
        request: WorkerTurnRequest,
        result: WorkerTurnResult,
        cleanup_result: bool | None,
        cleanup_error: str | None,
    ) -> dict[str, object]:
        base = f"runs/{run_id}"
        snapshot = self.store.inspect(workspace, self.resources)
        repository_root = Path(str(snapshot.meta["repo_root"]))
        state = dict(snapshot.state)
        if state.get("status") != "worker_running" or state.get("current_run_id") != run_id:
            raise ValueError("worker run state mismatch")
        plan = dict(snapshot.plan)
        staged_artifacts: dict[str, bytes] = {}
        result_status = "failed"
        if result.returncode == 0 and not result.timed_out:
            submission_errors = self._apply_worker_submission(
                workspace=workspace,
                goal=snapshot.goal,
                plan=plan,
                result=result,
                run_id=run_id,
                item_id=item_id,
                artifacts=staged_artifacts,
                recorded_artifacts=snapshot.artifacts,
            )
            if submission_errors:
                result = WorkerTurnResult(
                    output_text=result.output_text,
                    returncode=1,
                    session_id=result.session_id,
                    events=(*result.events, {
                        "event": "failure",
                        "failure_kind": "invalid_submission",
                        "error": "; ".join(submission_errors),
                    }),
                    timed_out=result.timed_out,
                    submission=None,
                )
            else:
                result_status = "completed"
        result_payload = {
            "schema_version": 1,
            "run_id": run_id,
            "output_text": result.output_text,
            "returncode": result.returncode,
            "session_id": result.session_id,
            "events": list(result.events),
            "timed_out": result.timed_out,
            "submission": None if result.submission is None else dict(result.submission),
            "cleanup": cleanup_result,
            "cleanup_error": cleanup_error,
        }
        result_relative = f"{base}/result.json"
        result_body = self._json_bytes(result_payload)
        staged_artifacts[result_relative] = result_body
        request_relative = f"{base}/request.json"
        request_metadata = snapshot.artifacts.get(request_relative)
        if not isinstance(request_metadata, dict):
            raise ValueError("worker run request audit record is missing")
        request_metadata = dict(request_metadata)
        result_metadata = self._artifact_metadata(result_relative, result_body)
        evidence = {
            "schema_version": 1,
            "run_id": run_id,
            "item_id": item_id,
            "request": request_metadata,
            "result": result_metadata,
            "evidence": [
                *[
                    self._artifact_metadata(relative, body)
                    for relative, body in staged_artifacts.items()
                    if relative != result_relative
                ],
            ],
            "status": result_status,
        }
        errors = validate_document(evidence, "run-evidence", self.resources)
        if errors:
            raise ValueError("; ".join(errors))
        evidence_relative = f"{base}/evidence.json"
        evidence_body = self._json_bytes(evidence)
        staged_artifacts[evidence_relative] = evidence_body
        transition(state, "review_required")
        review_action = issue_action(
            state,
            kind="review",
            workspace=workspace_id,
            route=route,
            run_id=run_id,
            repository_root=repository_root,
            context={
                "run": {
                    "run_id": run_id,
                    "item_id": item_id,
                    "status": result_status,
                    "request": request_metadata,
                    "result": result_metadata,
                    "evidence": self._artifact_metadata(evidence_relative, evidence_body),
                },
            },
            expected_output={
                "payload": {
                    "format": "json",
                    "required": ["decision"],
                    "decision_values": ["accepted", "changes_requested", "needs_human", "failed"],
                },
            },
            next_argv=None,
        )
        self.store.commit_action(
            workspace,
            self._revision(state),
            state,
            documents={"plan.yaml": encode_yaml(plan)} if result_status == "completed" else {},
            artifacts=staged_artifacts,
            event={
                "event": "worker_completed",
                "details": {
                    "run_id": run_id,
                    "item_id": item_id,
                    "status": result_status,
                    "timed_out": result.timed_out,
                },
            },
            validate_current=lambda current: self._validate_current_worker_run(
                current, route, run_id
            ),
        )
        return review_action

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def _command_result_artifacts(self, results: object, run_id: str) -> dict[str, bytes]:
        if results is None:
            return {}
        if not isinstance(results, list):
            raise ValueError("command_results must be a list")
        artifacts: dict[str, bytes] = {}
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("relative"), str) or not isinstance(result.get("exit_code"), int):
                raise ValueError("invalid command result")
            relative = result["relative"]
            if not relative.startswith(f"artifacts/runs/{run_id}/"):
                raise ValueError("command result artifact must be bound to run")
            if relative in artifacts:
                raise ValueError(f"duplicate worker artifact: {relative}")
            artifacts[relative] = self._json_bytes({
                "exit_code": result["exit_code"],
                "argv": result.get("argv", []),
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
            })
        return artifacts

    def _resume_worker_runtime(
        self, *, workspace: Path, route: str,
    ) -> dict[str, object]:
        if self.runtime is None:
            raise ValueError("worker runtime is not configured")
        snapshot = self.store.inspect(workspace, self.resources)
        state = dict(snapshot.state)
        run_id = state.get("current_run_id")
        if not isinstance(run_id, str):
            raise ValueError("worker run is missing run ID")
        request_payload = snapshot.run_requests.get(run_id)
        if not isinstance(request_payload, dict):
            raise ValueError("invalid worker run request")
        if (
            request_payload.get("adapter") != self.runtime_adapter
            or request_payload.get("provider") != self.worker_provider
            or request_payload.get("config_digest") != self.runtime_config_digest
            or request_payload.get("provider_contract_version")
            != self.provider_contract_version
        ):
            raise ValueError("worker runtime configuration mismatch")
        return self._run_worker_runtime(
            workspace=workspace,
            route=route,
            request_payload=request_payload,
        )

    def _new_run_request(
        self, *, snapshot: WorkspaceSnapshot, run_id: str, item_id: str,
    ) -> dict[str, object]:
        state = snapshot.state
        return {
            "schema_version": 1,
            "workspace_id": state["workspace_id"],
            "run_id": run_id,
            "item_id": item_id,
            "repository_root": snapshot.meta["repo_root"],
            "repository_identity": state["repository_identity"],
            "adapter": self.runtime_adapter,
            "provider": self.worker_provider,
            "config_digest": self.runtime_config_digest,
            "provider_contract_version": self.provider_contract_version,
            "prompt": self._worker_prompt(
                snapshot.goal,
                snapshot.plan,
                workspace_id=str(state["workspace_id"]),
                run_id=run_id,
                item_id=item_id,
                repository_root=str(snapshot.meta["repo_root"]),
            ),
            "session_id": "",
            "timeout_seconds": self.timeout_seconds,
            "environment": {},
        }

    def _apply_worker_submission(
        self,
        *,
        workspace: Path,
        goal: dict[str, object],
        plan: dict[str, object],
        result: WorkerTurnResult,
        run_id: str,
        item_id: str,
        artifacts: dict[str, bytes],
        recorded_artifacts: Mapping[str, Mapping[str, object]],
    ) -> list[str]:
        submission = result.submission
        if not isinstance(submission, Mapping):
            return ["worker result is missing a submission payload"]
        payload = dict(submission)
        errors = validate_document(payload, "worker-submission", self.resources)
        if errors:
            return errors
        materialized = materialize_run_evidence(plan, item_id, run_id)
        plan.clear()
        plan.update(materialized)
        errors.extend(apply_updates(plan, payload.get("updates")))
        errors.extend(validate_ready_plan(goal, plan))
        try:
            command_artifacts = self._command_result_artifacts(
                payload.get("command_results"), run_id
            )
            material_artifacts = self._material_artifacts(payload.get("artifacts"))
        except ValueError as exc:
            errors.append(str(exc))
            return errors
        worker_artifacts: dict[str, bytes] = {}
        for candidate in (command_artifacts, material_artifacts):
            for relative, body in candidate.items():
                if relative in artifacts or relative in worker_artifacts:
                    errors.append(f"duplicate worker artifact: {relative}")
                    continue
                worker_artifacts[relative] = body
        if errors:
            return list(dict.fromkeys(errors))
        artifacts.update(worker_artifacts)
        gaps = completion_gaps(
            goal,
            plan,
            lambda entry: evidence_exists(
                workspace, entry, artifacts, recorded_artifacts
            ),
        )
        evidence_gaps = [
            gap for gap in gaps
            if gap.endswith("missing evidence") or gap.endswith("undeclared evidence")
        ]
        if evidence_gaps:
            errors.append(evidence_gaps[0].split(": ", 1)[1])
        return list(dict.fromkeys(errors))

    @staticmethod
    def _material_artifacts(values: object) -> dict[str, bytes]:
        if not isinstance(values, list):
            raise ValueError("artifacts must be a list")
        artifacts: dict[str, bytes] = {}
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("invalid worker artifact")
            relative = value.get("relative")
            content = value.get("content")
            if (
                not isinstance(relative, str)
                or not relative.startswith("artifacts/")
                or ".." in Path(relative).parts
                or not isinstance(content, str)
            ):
                raise ValueError("invalid worker artifact")
            if relative in artifacts:
                raise ValueError(f"duplicate worker artifact: {relative}")
            artifacts[relative] = content.encode("utf-8")
        return artifacts

    @staticmethod
    def _artifact_metadata(relative: str, body: bytes) -> dict[str, object]:
        return {
            "relative": relative,
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
        }

    @staticmethod
    def _validate_current_worker_start(state: dict[str, object], route: str) -> None:
        require_mutable(state)
        if state.get("status") != "ready" or state.get("pending_action") is not None:
            raise ValueError("workspace is not ready")
        if state.get("supervisor_route") != route:
            raise ValueError("supervisor route mismatch")

    @staticmethod
    def _validate_current_worker_run(
        state: dict[str, object], route: str, run_id: str,
    ) -> None:
        require_mutable(state)
        if state.get("status") != "worker_running":
            raise ValueError("worker run state mismatch")
        if state.get("pending_action") is not None:
            raise ValueError("invalid worker-running pending action")
        if state.get("supervisor_route") != route:
            raise ValueError("supervisor route mismatch")
        if state.get("current_run_id") != run_id:
            raise ValueError("worker run state mismatch")
