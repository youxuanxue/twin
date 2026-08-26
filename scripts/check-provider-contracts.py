#!/usr/bin/env python3
"""Verify pinned provider help transcripts against Twin's argv contracts."""
from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from twin.runtime.config import validate_provider_help  # noqa: E402


PINNED_HELP = {
    "claude": "claude-2.1.246.txt",
    "codex": "codex-0.149.1.txt",
    "gemini": "gemini-0.57.0.txt",
}


def main() -> int:
    fixture_root = REPOSITORY_ROOT / "tests" / "fixtures" / "provider_help"
    errors: list[str] = []
    for provider, filename in PINNED_HELP.items():
        path = fixture_root / filename
        try:
            transcript = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{provider} help fixture is unavailable: {exc}")
            continue
        errors.extend(validate_provider_help(provider, transcript))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
