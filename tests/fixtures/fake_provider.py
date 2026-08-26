from __future__ import annotations

import json
import os
import sys


def emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def main() -> int:
    mode = sys.argv[1]
    if mode == "env":
        emit({
            "session_id": "env-session",
            "output_text": json.dumps({
                "DEV_RULES": os.environ.get("DEV_RULES"),
                "VISIBLE": os.environ.get("VISIBLE"),
            }, sort_keys=True),
        })
        return 0
    if mode == "malformed":
        print("not json", flush=True)
        return 0
    if mode == "claude-stream":
        emit({
            "type": "assistant",
            "session_id": "claude-session",
            "message": {"content": [{"type": "text", "text": "claude completed"}]},
        })
        return 0
    if mode == "codex":
        emit({"event": "message", "session_id": "codex-session", "output_text": "codex completed"})
        return 0
    if mode == "gemini":
        emit({"event": "message", "session_id": "gemini-session", "text": "gemini completed"})
        return 0
    raise SystemExit(f"unknown fake provider mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
