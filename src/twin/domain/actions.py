from __future__ import annotations

import hashlib
import hmac
import secrets
import shlex
from pathlib import Path


def issue_action(
    state: dict[str, object], *, kind: str, workspace: str, route: str, run_id: str | None = None,
    repository_root: Path,
    context: dict[str, object],
    expected_output: dict[str, object],
    next_argv: list[str] | None,
) -> dict[str, object]:
    """Attach one pending action to a prospective next state and return its token once."""
    revision = state.get("state_revision")
    if not isinstance(revision, int):
        raise ValueError("invalid state revision")
    token = secrets.token_urlsafe(32)
    next_revision = revision + 1
    repository = repository_root.expanduser().resolve()
    repository_identity = _repository_identity(repository)
    state["pending_action"] = {
        "kind": kind,
        "state_revision": next_revision,
        "route": route,
        "token_hash": _hash_token(token),
        "run_id": run_id,
        "repository_identity": repository_identity,
    }
    submit_argv = _submit_argv(
        kind=kind,
        workspace=workspace,
        route=route,
        revision=next_revision,
        token=token,
        run_id=run_id,
    )
    return {
        "contract_version": 1,
        "action": kind,
        "workspace": workspace,
        "supervisor_route": route,
        "state_revision": next_revision,
        "action_token": token,
        "repository": {
            "root": str(repository),
            "identity": repository_identity,
        },
        "context": context,
        "expected_output": expected_output,
        "submit": {
            **command_descriptor(submit_argv),
            "stdin": {"format": "json", "source": "payload"},
        },
        "next_command": None if next_argv is None else command_descriptor(next_argv),
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
    repository_identity = state.get("repository_identity")
    if pending.get("repository_identity") != repository_identity:
        raise ValueError("action repository mismatch")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _submit_argv(
    *, kind: str, workspace: str, route: str, revision: int, token: str,
    run_id: str | None,
) -> list[str]:
    commands = {
        "author_plan": "twin submit-plan",
        "review": "twin submit-review",
    }
    command = commands.get(kind)
    if command is None:
        raise ValueError(f"unsupported action kind: {kind}")
    argv = [
        *command.split(),
        "--workspace", workspace,
        "--supervisor", route,
        "--state-revision", str(revision),
        f"--action-token={token}",
    ]
    if run_id is not None:
        argv.extend(("--run-id", run_id))
    argv.extend(("--payload-file", "-", "--json"))
    return argv


def command_descriptor(argv: list[str]) -> dict[str, object]:
    return {"argv": list(argv), "command": shlex.join(argv)}


def _repository_identity(repository_root: Path) -> str:
    return hashlib.sha256(str(repository_root).encode("utf-8")).hexdigest()
