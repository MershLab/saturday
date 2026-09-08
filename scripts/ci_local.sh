#!/usr/bin/env bash
# Run every check .github/workflows/ci.yml runs, in the same order, so a push
# cannot fail on something that was reproducible here.
#
# Keep the step list in sync with the `test` job in ci.yml. The lint step is
# the one that bites: ruff is a dev dependency, so a venv built without
# .[dev] silently has no ruff and `pytest` alone looks like a full check.
#
# Usage: scripts/ci_local.sh          (stops at the first failure, like CI)
#        scripts/ci_local.sh --all    (runs everything, reports at the end)
set -uo pipefail

cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

ALL=0
[ "${1:-}" = "--all" ] && ALL=1

failed=()
run() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  # exit code of the command itself, never of a pipe stage
  if "$@"; then
    printf '[ok] %s\n' "$name"
  else
    local code=$?
    printf '[FAIL] %s (exit %d)\n' "$name" "$code"
    failed+=("$name")
    [ "$ALL" -eq 1 ] || { printf '\nstopped at first failure; use --all to run the rest\n'; exit "$code"; }
  fi
}

"$PY" -m ruff --version >/dev/null 2>&1 || {
  printf 'ruff is missing: pip install -e ".[dev]"\n' >&2
  printf 'CI lints before it tests, so without ruff this script cannot tell you what CI will say.\n' >&2
  exit 127
}

run "lint"   "$PY" -m ruff check .
run "demo"   "$PY" examples/offline_demo.py
run "doctor" "$PY" -m saturday doctor --provider ollama --offline
run "tests"  "$PY" -m pytest -q

# The `browser` job is a second job in ci.yml, not a step of `test`, and it is
# the only thing that runs the UI against a real page. Skipping it locally is
# how a change to app.js reaches CI unexercised: `node --check` gets the syntax
# and nothing else. Skipped rather than failed when playwright is absent, since
# it is an optional extra.
if "$PY" -c "import playwright" >/dev/null 2>&1; then
  run "browser" "$PY" -m pytest tests/test_webui.py -q -k "ui_" -p no:randomly
else
  printf '\n=== browser ===\n[skip] playwright not installed: pip install -e ".[dev,browser]"\n'
  printf '       (CI still runs these, so a UI change is unverified until you do)\n'
fi

printf '\n'
if [ ${#failed[@]} -eq 0 ]; then
  printf 'all CI checks passed\n'
else
  printf 'failed: %s\n' "${failed[*]}"
  exit 1
fi
