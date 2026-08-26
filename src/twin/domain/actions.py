from __future__ import annotations

import hashlib
import hmac
import secrets


def issue_action(
    state: dict[str, object], *, kind: str, workspace: str, route: str, run_id: str | None = None,
    item_id: str | None = None,
) -> dict[str, object]:
    """Attach one pending action to a prospective next state and return its token once."""
    revision = state.get("state_revision")
    if not isinstance(revision, int):
        raise ValueError("invalid state revision")
    token = secrets.token_urlsafe(32)
    next_revision = revision + 1
    state["pending_action"] = {
        "kind": kind,
        "state_revision": next_revision,
        "route": route,
        "token_hash": _hash_token(token),
        "run_id": run_id,
    }
    metadata: dict[str, object] = {}
    if run_id is not None:
        metadata["run_id"] = run_id
    if item_id is not None:
        metadata["item_id"] = item_id
    return {
        "contract_version": 1,
        "action": kind,
        "workspace": workspace,
        "supervisor_route": route,
        "state_revision": next_revision,
        "action_token": token,
        "context": {"metadata": metadata},
        "expected_output": {},
        "submit": {"command": _submit_command(kind, workspace)},
    }


def validate_submission(
    state: dict[str, object], *, kind: str, route: str, revision: int, token: str, run_id: str | None = None
) -> None:
    pending = state.get("pending_action")
    if not isinstance(pending, dict):
        raise ValueError("stale or consumed action")
    if pending.get("route") != route:
        raise ValueError("supervisor route mismatch")
    if pending.get("kind") != kind:
        raise ValueError("action kind mismatch")
    if pending.get("state_revision") != revision or state.get("state_revision") != revision:
        raise ValueError("stale or consumed action")
    expected_run_id = pending.get("run_id")
    if expected_run_id != run_id:
        raise ValueError("run ID mismatch")
    token_hash = pending.get("token_hash")
    if not isinstance(token_hash, str) or not hmac.compare_digest(token_hash, _hash_token(token)):
        raise ValueError("invalid action token")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _submit_command(kind: str, workspace: str) -> str:
    commands = {
        "author_plan": "twin submit-plan",
        "worker_instruction": "twin submit-instruction",
        "review": "twin submit-review",
    }
    return f"{commands.get(kind, 'twin submit-action')} --workspace {workspace}"
