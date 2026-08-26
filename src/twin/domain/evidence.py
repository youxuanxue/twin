from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def evidence_exists(
    workspace: Path,
    entry: str,
    staged_artifacts: Mapping[str, bytes] | None = None,
    recorded_artifacts: Mapping[str, Mapping[str, object]] | None = None,
) -> bool:
    relative = entry.removeprefix("command:") if entry.startswith("command:") else entry
    staged = staged_artifacts.get(relative) if staged_artifacts is not None else None
    if staged is not None:
        return _successful_command_bytes(staged) if entry.startswith("command:") else True
    if recorded_artifacts is not None and relative not in recorded_artifacts:
        return False
    if entry.startswith("command:"):
        return _successful_command(workspace, relative)
    return _stored_artifact(workspace, entry)


def _stored_artifact(workspace: Path, relative: str) -> bool:
    path = _safe_path(workspace, relative)
    return path is not None and path.is_file()


def _successful_command(workspace: Path, relative: str) -> bool:
    path = _safe_path(workspace, relative)
    if path is None or not path.is_file():
        return False
    try:
        return _successful_command_bytes(path.read_bytes())
    except OSError:
        return False


def _successful_command_bytes(body: bytes) -> bool:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("exit_code") == 0


def _safe_path(workspace: Path, relative: str) -> Path | None:
    candidate = Path(relative)
    if not relative or candidate.is_absolute():
        return None
    path = (workspace / candidate).resolve()
    try:
        path.relative_to(workspace.resolve() / "artifacts")
    except ValueError:
        return None
    return path
