from __future__ import annotations

import json
from pathlib import Path


def evidence_exists(workspace: Path, entry: str) -> bool:
    if entry.startswith("command:"):
        return _successful_command(workspace, entry.removeprefix("command:"))
    return _stored_artifact(workspace, entry)


def _stored_artifact(workspace: Path, relative: str) -> bool:
    path = _safe_path(workspace, relative)
    return path is not None and path.is_file()


def _successful_command(workspace: Path, relative: str) -> bool:
    path = _safe_path(workspace, relative)
    if path is None or not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
