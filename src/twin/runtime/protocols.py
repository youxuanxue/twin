from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol


_AMBIENT_ENVIRONMENT_ALLOWLIST = frozenset({
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "WINDIR",
})


def clean_worker_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    if environment is None:
        return {}
    return {
        str(key): str(value)
        for key, value in environment.items()
    }


def worker_process_environment(
    ambient: Mapping[str, str], requested: Mapping[str, str] | None,
) -> dict[str, str]:
    """Propagate only portable host settings plus explicit worker settings."""
    inherited = {
        str(key): str(value)
        for key, value in ambient.items()
        if str(key) in _AMBIENT_ENVIRONMENT_ALLOWLIST
    }
    inherited.update(clean_worker_environment(requested))
    return inherited


def parse_worker_submission(output_text: str) -> dict[str, object] | None:
    try:
        value = json.loads(output_text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
    submission: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(dict(event) for event in self.events))
        object.__setattr__(
            self,
            "submission",
            None if self.submission is None else dict(self.submission),
        )


class WorkerRuntime(Protocol):
    def run_turn(self, request: WorkerTurnRequest) -> WorkerTurnResult: ...


class WorkspaceIsolation(Protocol):
    def prepare(self, repo_root: Path, workspace_id: str) -> Path: ...
    def cleanup(self, repo_root: Path, workspace_id: str) -> bool: ...
