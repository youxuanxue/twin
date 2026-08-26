from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


_SAFE_DETAIL_KEYS = {
    "action", "artifact", "artifacts", "bytes", "count", "exit_code", "from_route",
    "item_id", "kind", "length", "relative", "route", "run_id", "sha256", "size",
    "state_revision", "status", "timed_out", "to_route", "workspace_id",
}
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def redacted_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key, child in value.items():
        if not isinstance(key, str) or key not in _SAFE_DETAIL_KEYS:
            continue
        redacted = _redact(key, child)
        if redacted is not None:
            result[key] = redacted
    return result


def _redact(key: str, value: object) -> object | None:
    if isinstance(value, dict):
        return {
            child_key: _redact(child_key, child)
            for child_key, child in value.items()
            if isinstance(child_key, str)
            and child_key in _SAFE_DETAIL_KEYS
            and _redact(child_key, child) is not None
        }
    if isinstance(value, list):
        return [redacted for child in value if (redacted := _redact(key, child)) is not None]
    if isinstance(value, str):
        if key == "sha256":
            return value if re.fullmatch(r"[0-9a-f]{64}", value) else None
        return value if _SAFE_TEXT.fullmatch(value) else None
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def event_record(
    *, workspace_id: str, state_revision: int, event: dict[str, object]
) -> dict[str, object]:
    name = event.get("event")
    if not isinstance(name, str) or _SAFE_TEXT.fullmatch(name) is None:
        raise ValueError("event name is required")
    supplied_workspace = event.get("workspace_id")
    if supplied_workspace is not None and supplied_workspace != workspace_id:
        raise ValueError("event workspace_id mismatch")
    supplied_revision = event.get("state_revision")
    if supplied_revision is not None and supplied_revision != state_revision:
        raise ValueError("event state_revision mismatch")
    return {
        "schema_version": 1,
        "recorded_at": now_utc(),
        "event": name,
        "workspace_id": workspace_id,
        "state_revision": state_revision,
        "details": redacted_metadata(event.get("details", {})),
    }


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
