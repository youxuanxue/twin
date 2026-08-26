#!/usr/bin/env python3
"""Generate or verify the deterministic packaged Twin skill manifest."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from twin.skill_manifest import (  # noqa: E402
    MANIFEST_FILENAME,
    build_skill_manifest,
    render_skill_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    skill_dir = REPOSITORY_ROOT / "skills" / "twin"
    manifest_path = skill_dir / MANIFEST_FILENAME
    rendered = render_skill_manifest(build_skill_manifest(skill_dir))
    if args.check:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            print(f"skill manifest missing: {manifest_path}", file=sys.stderr)
            return 1
        if manifest_path.read_text(encoding="utf-8") != rendered:
            print(
                "skill manifest is stale; run scripts/generate-skill-manifest.py",
                file=sys.stderr,
            )
            return 1
        return 0
    manifest_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
