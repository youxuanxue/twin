#!/usr/bin/env bash
set -euo pipefail

tmp_wheels="$(mktemp -d)"
tmp_venv="$(mktemp -d)"
tmp_home="$(mktemp -d)"
trap 'rm -rf "$tmp_wheels" "$tmp_venv" "$tmp_home"' EXIT

ci_requires_container=0
case "${CI:-}" in
  ""|0|false|False) ;;
  *) ci_requires_container=1 ;;
esac

runtime_surface=(README.md docs skills src/twin pyproject.toml)
forbidden_patterns=(
  'DEV_RULES'
  '/Codes/dev-rules'
  '/Codes/agent-skills'
  'scripts\.twin'
  'scaffold'
  'bootstrap'
  'active-pointer'
  'legacy command registration'
)
for pattern in "${forbidden_patterns[@]}"; do
  if rg -F -n --hidden -e "$pattern" "${runtime_surface[@]}"; then
    echo "forbidden standalone-runtime reference: $pattern" >&2
    exit 1
  fi
done

PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m pip wheel --no-deps --wheel-dir "$tmp_wheels" .
wheel="$(find "$tmp_wheels" -maxdepth 1 -name 'xuejiao_twin-*.whl' -print -quit)"
test -n "$wheel"
python3 -m venv "$tmp_venv"
"$tmp_venv/bin/python" -m pip install --no-deps "$wheel"
mkdir -p "$tmp_home/.cursor/skills"
HOME="$tmp_home" "$tmp_venv/bin/twin" setup
HOME="$tmp_home" "$tmp_venv/bin/twin" contract --json
HOME="$tmp_home" "$tmp_venv/bin/twin" doctor --json

set +e
TWIN_WHEEL="$wheel" TWIN_REQUIRE_CONTAINER="${TWIN_REQUIRE_CONTAINER:-0}" \
  bash scripts/smoke-clean-home.sh
smoke_status=$?
set -e
if [[ "$smoke_status" -eq 77 ]]; then
  if [[ "${TWIN_REQUIRE_CONTAINER:-0}" == "1" || "$ci_requires_container" == "1" ]]; then
    echo "container smoke is required" >&2
    exit 1
  fi
  echo "SKIP: clean-home container smoke"
elif [[ "$smoke_status" -ne 0 ]]; then
  exit "$smoke_status"
fi
