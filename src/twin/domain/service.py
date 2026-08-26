from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Callable

from twin.domain.actions import issue_action, validate_submission
from twin.domain.evidence import evidence_exists
from twin.domain.plan import apply_updates, choose_next_item, completion_gaps, validate_plan
from twin.domain.state import require_mutable, transition
from twin.resources import ResourceCatalog
from twin.schema import validate_document
from twin.storage.workspaces import WorkspaceStore
from twin.yaml_codec import encode_yaml, load_yaml


class TwinService:
    def __init__(self, store: WorkspaceStore, runtime: Callable[[dict[str, object]], object] | None = None) -> None:
        self.store = store
        self.runtime = runtime
        self.resources = ResourceCatalog(Path(__file__).resolve().parents[3])

    def start(self, goal: str, repo_root: Path, route: str) -> dict[str, object]:
        workspace_id = self.store.create(goal, repo_root, route)
        workspace = self.store.resolve(workspace_id, repo_root)
        state = self.store.load_state(workspace)
        action = issue_action(state, kind="author_plan", workspace=workspace_id, route=route)
        self.store.replace_state(workspace, 0, state)
        return action

    def run(self, workspace_ref: str | None, repo_root: Path, route: str) -> dict[str, object]:
        workspace = self.store.resolve(workspace_ref, repo_root)
        state = self._state(workspace)
        require_mutable(state)
        if state.get("supervisor_route") != route:
            raise ValueError("supervisor route mismatch")
        if state.get("pending_action") is not None:
            raise ValueError("pending action")
        if state.get("status") != "ready":
            raise ValueError("workspace is not ready")
        item = choose_next_item(load_yaml(workspace / "plan.yaml"))
        if item is None or not isinstance(item.get("id"), str):
            raise ValueError("no runnable plan item")
        transition(state, "worker_running")
        run_id = "run-" + secrets.token_hex(12)
        state["current_run_id"] = run_id
        state["current_item_id"] = item["id"]
        action = issue_action(
            state, kind="worker_instruction", workspace=str(state["workspace_id"]), route=route,
            run_id=run_id, item_id=item["id"],
        )
        self.store.replace_state(workspace, self._revision(state), state)
        if self.runtime is not None:
            self.runtime(action)
        return action

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

    def submit_instruction(self, workspace_ref: str, route: str, revision: int, token: str, run_id: str, payload: dict[str, object]) -> dict[str, object]:
        workspace = self._resolve_submission(workspace_ref)
        state = self._state(workspace)
        require_mutable(state)
        validate_submission(state, kind="worker_instruction", route=route, revision=revision, token=token, run_id=run_id)
        plan = load_yaml(workspace / "plan.yaml")
        errors = apply_updates(plan, payload.get("updates"))
        errors.extend(validate_plan(load_yaml(workspace / "goal.yaml"), plan))
        if errors:
            raise ValueError("; ".join(errors))
        command_artifacts = self._command_result_artifacts(payload.get("command_results"), run_id)
        goal = load_yaml(workspace / "goal.yaml")
        gaps = completion_gaps(
            goal, plan, lambda entry: evidence_exists(workspace, entry, command_artifacts)
        )
        evidence_gaps = [
            gap for gap in gaps
            if gap.endswith("missing evidence") or gap.endswith("undeclared evidence")
        ]
        if evidence_gaps:
            raise ValueError(evidence_gaps[0].split(": ", 1)[1])
        transition(state, "review_required")
        action = issue_action(state, kind="review", workspace=str(state["workspace_id"]), route=route, run_id=run_id)
        self.store.commit_action(
            workspace, revision, state,
            documents={"plan.yaml": encode_yaml(plan)}, artifacts=command_artifacts,
            event={"event": "instruction_submitted", "details": {"route": route, "run_id": run_id}},
            validate_current=lambda current: self._validate_current_submission(
                current, "worker_instruction", route, revision, token, run_id
            ),
        )
        return action

    def submit_review(self, workspace_ref: str, route: str, revision: int, token: str, run_id: str, payload: dict[str, object]) -> dict[str, object]:
        workspace = self._resolve_submission(workspace_ref)
        state = self._state(workspace)
        require_mutable(state)
        validate_submission(state, kind="review", route=route, revision=revision, token=token, run_id=run_id)
        decision = payload.get("decision")
        if decision == "accepted":
            gaps = completion_gaps(
                load_yaml(workspace / "goal.yaml"), load_yaml(workspace / "plan.yaml"),
                lambda entry: evidence_exists(workspace, entry),
            )
            if gaps:
                raise ValueError("missing evidence")
            transition(state, "accepted_done")
        elif decision == "changes_requested":
            transition(state, "ready")
        elif decision == "needs_human":
            transition(state, "needs_human")
        elif decision == "failed":
            transition(state, "failed")
        else:
            raise ValueError("invalid review decision")
        state["pending_action"] = None
        self.store.replace_state(workspace, revision, state)
        return self._result(workspace, self._state(workspace))

    def respond(self, workspace_ref: str | None, repo_root: Path, answer: str) -> dict[str, object]:
        workspace = self.store.resolve(workspace_ref, repo_root)
        state = self._state(workspace)
        require_mutable(state)
        if state.get("status") != "needs_human":
            raise ValueError("workspace is not awaiting human response")
        body = answer.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        artifact = self.store.write_artifact(workspace, f"artifacts/human/{digest}.txt", body)
        self.store.append_event(workspace, {"event": "human_response", "details": {"artifact": artifact["relative"], "length": len(body), "sha256": digest}})
        transition(state, "ready")
        self.store.replace_state(workspace, self._revision(state), state)
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
        self.store.replace_state(workspace, self._revision(state), state)
        self.store.append_event(workspace, {"event": "handoff", "details": {"from_route": from_route, "to_route": to_route}})
        return self._result(workspace, self._state(workspace))

    def status(self, workspace_ref: str | None, repo_root: Path) -> dict[str, object]:
        workspace = self.store.resolve(workspace_ref, repo_root)
        state = self._state(workspace)
        workspace_id = state.get("workspace_id")
        if workspace_id != workspace.name:
            raise ValueError("state workspace_id mismatch")
        for raw in (workspace / "events.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(raw)
            if event.get("workspace_id") != workspace_id:
                raise ValueError("event workspace_id mismatch")
        return self._result(workspace, state)

    def _validate_plan_payload(self, payload: dict[str, object], workspace_id: str) -> tuple[dict[str, object], dict[str, object]]:
        goal = payload.get("goal")
        plan = payload.get("plan")
        if not isinstance(goal, dict) or not isinstance(plan, dict):
            raise ValueError("plan payload requires goal and plan objects")
        goal = dict(goal)
        plan = dict(plan)
        goal["id"] = workspace_id
        plan["goal_id"] = workspace_id
        errors = validate_document(goal, "goal", self.resources) + validate_document(plan, "plan", self.resources) + validate_plan(goal, plan)
        if errors:
            raise ValueError("; ".join(errors))
        return goal, plan

    def _resolve_submission(self, workspace_ref: str) -> Path:
        # Submission APIs intentionally accept only IDs; the active pointer is not a valid authority.
        return self.store.resolve(workspace_ref, self.store.paths.workspaces)

    def _state(self, workspace: Path) -> dict[str, object]:
        state = self.store.load_state(workspace)
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
    def _revision(state: dict[str, object]) -> int:
        revision = state.get("state_revision")
        if not isinstance(revision, int):
            raise ValueError("invalid state revision")
        return revision

    def _result(self, workspace: Path, state: dict[str, object]) -> dict[str, object]:
        return {"workspace": str(state["workspace_id"]), "status": state["status"], "state_revision": state["state_revision"], "supervisor_route": state["supervisor_route"]}

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
            artifacts[relative] = json.dumps(
                {"exit_code": result["exit_code"]}, sort_keys=True
            ).encode("utf-8")
        return artifacts
