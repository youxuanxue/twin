#!/usr/bin/env bash
set -euo pipefail

wheel="${TWIN_WHEEL:?TWIN_WHEEL must name an exact wheel}"
if [[ "$wheel" != /* || ! -f "$wheel" ]]; then
  echo "TWIN_WHEEL must be an absolute path to an existing wheel" >&2
  exit 2
fi

runtime=""
ci_requires_container=0
case "${CI:-}" in
  ""|0|false|False) ;;
  *) ci_requires_container=1 ;;
esac
requested="${TWIN_CONTAINER_RUNTIME:-}"
if [[ "$requested" == "docker" || "$requested" == "podman" ]]; then
  if command -v "$requested" >/dev/null 2>&1 && "$requested" info >/dev/null 2>&1; then
    runtime="$requested"
  fi
fi
if [[ -z "$runtime" ]] && command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  runtime="docker"
fi
if [[ -z "$runtime" ]] && command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
  runtime="podman"
fi
if [[ -z "$runtime" ]]; then
  if [[ "${TWIN_REQUIRE_CONTAINER:-0}" == "1" || "$ci_requires_container" == "1" ]]; then
    echo "FAIL: no supported container runtime" >&2
    exit 1
  fi
  echo "SKIP: no supported container runtime"
  exit 77
fi

stage_parent="${TWIN_STAGE_PARENT:-${TMPDIR:-/tmp}}"
if [[ "$(uname -s)" == "Darwin" && -d "$HOME" ]]; then
  stage_parent="${TWIN_STAGE_PARENT:-$HOME}"
fi
stage="$(python3 - "$stage_parent" <<'PY'
import sys
import tempfile
from pathlib import Path

print(Path(tempfile.mkdtemp(prefix="twin-smoke-", dir=sys.argv[1])).resolve())
PY
)"
trap 'rm -rf "$stage"' EXIT
cp "$wheel" "$stage/$(basename "$wheel")"
cp tests/smoke_installed.py "$stage/smoke_installed.py"

"$runtime" run --rm --network none \
  --mount "type=bind,src=$stage,dst=/stage,readonly" \
  python:3.9-slim-bookworm sh -ec '
    for forbidden in \
      /Users/feng/Codes/twin \
      /Users/feng/Codes/dev-rules \
      /Users/feng/Codes/agent-skills \
      /Users/feng/Codes \
      /Users/feng; do
      if [ -e "$forbidden" ]; then
        echo "forbidden host path is present: $forbidden" >&2
        exit 1
      fi
    done

    export HOME="$(mktemp -d)"
    export TWIN_BIN=/opt/twin-venv/bin/twin
    wheel="$(find /stage -maxdepth 1 -name "xuejiao_twin-*.whl" -print -quit)"
    test -n "$wheel"
    python -m venv /opt/twin-venv
    /opt/twin-venv/bin/python -m pip install --no-deps "$wheel"
    mkdir -p "$HOME/.cursor/skills"
    "$TWIN_BIN" setup --json >/dev/null
    "$TWIN_BIN" contract --json >/dev/null
    "$TWIN_BIN" doctor --json >/dev/null

    /opt/twin-venv/bin/python - <<"PY"
import importlib
import os
import sysconfig
from pathlib import Path

home = Path(os.environ["HOME"])
package_root = Path(importlib.import_module("twin").__file__).resolve().parent
data_root = Path(sysconfig.get_path("data")) / "share" / "twin"
skill_root = home / ".twin" / "skills" / "twin"
for root in (package_root, data_root, skill_root):
    if not root.is_dir():
        raise SystemExit(f"installed root is missing: {root}")
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"installed root contains symlink: {path}")
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for forbidden in (
                "$DEV_RULES",
                "/Users/feng/Codes/twin",
                "/Users/feng/Codes/dev-rules",
                "/Users/feng/Codes/agent-skills",
                "scripts.twin",
                "src/twin",
            ):
                if forbidden in text:
                    raise SystemExit(f"installed text references a checkout: {path}")

installed_skill = (home / ".twin" / "skills" / "twin").resolve()
for relative in (
    ".cursor/skills/twin",
    ".codex/skills/twin",
    ".gemini/antigravity-cli/skills/twin",
):
    entry = home / relative
    if not entry.is_symlink() or entry.resolve() != installed_skill:
        raise SystemExit(f"host Twin link is invalid: {entry}")
claude_registry = home / ".claude" / "skills"
if not claude_registry.is_symlink() or claude_registry.resolve() != (home / ".cursor" / "skills").resolve():
    raise SystemExit(f"shared Claude registry link is invalid: {claude_registry}")
PY

    /opt/twin-venv/bin/python /stage/smoke_installed.py
  '
