from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol


_BLOCKED_ENVIRONMENT_KEYS = frozenset({"DEV_RULES", "PERSONA_PATH", "TWIN_PERSONA_PATH"})


def clean_worker_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    if environment is None:
        return {}
    return {
        str(key): str(value)
        for key, value in environment.items()
        if str(key) not in _BLOCKED_ENVIRONMENT_KEYS
    }


@dataclass(frozen=True)
class WorkerTurnRequest:
    prompt: str
    cwd: Path
    provider: str
    session_id: str
    timeout_seconds: float
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cwd", Path(self.cwd))
        object.__setattr__(self, "environment", clean_worker_environment(self.environment))


@dataclass(frozen=True)
class WorkerTurnResult:
    output_text: str
    returncode: int
    session_id: str
    events: tuple[dict[str, object], ...]
    timed_out: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(dict(event) for event in self.events))


class WorkerRuntime(Protocol):
    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult: ...


class WorkspaceIsolation(Protocol):
    def prepare(self, repo_root: Path, workspace_id: str) -> Path: ...
    def cleanup(self, repo_root: Path, workspace_id: str) -> bool: ...
