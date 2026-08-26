#!/usr/bin/env python3
"""Generate or verify Twin's agent-facing CLI integration document."""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from twin.cli import build_parser  # noqa: E402
from twin.contract import render_agent_integration  # noqa: E402
from twin.resources import ResourceCatalog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = REPOSITORY_ROOT / "docs" / "agent-integration.md"
    rendered = render_agent_integration(
        build_parser(), ResourceCatalog(REPOSITORY_ROOT)
    )
    if args.check:
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        if current != rendered:
            sys.stderr.writelines(difflib.unified_diff(
                current.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=str(target),
                tofile="generated agent contract",
            ))
            print(
                "agent contract is stale; run scripts/export_agent_contract.py",
                file=sys.stderr,
            )
            return 1
        return 0
    target.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
