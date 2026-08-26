from __future__ import annotations


TERMINAL_STATUSES = frozenset({"accepted_done", "failed"})

_TRANSITIONS = {
    "awaiting_plan": {"ready"},
    "ready": {"worker_running"},
    "worker_running": {"review_required"},
    "review_required": {"ready", "needs_human", "accepted_done", "failed"},
    "needs_human": {"ready"},
}


def require_mutable(state: dict[str, object]) -> None:
    if state.get("status") in TERMINAL_STATUSES:
        raise ValueError("terminal workspace")


def transition(state: dict[str, object], target: str) -> None:
    source = state.get("status")
    if not isinstance(source, str) or target not in _TRANSITIONS.get(source, set()):
        raise ValueError(f"invalid lifecycle transition: {source} -> {target}")
    state["status"] = target
