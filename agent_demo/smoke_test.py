"""End-to-end smoke test: drives the CLI as a real subprocess chain.

Verifies persistence across invocations, correct output, and error codes.
Run:  python smoke_test.py   -> exits 0 if every assertion holds.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
STORE = ROOT / "_smoke" / "inv.json"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

passed = failed = 0


def run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "inventory.cli", "--store", str(STORE), *args],
        capture_output=True, text=True, env=ENV,
    )


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


print("[1] add item")
r = run("add", "WIDGET-1", "Test Widget", "--price", "9.99", "--qty", "10")
check("add exits 0", r.returncode == 0, r.stderr)
check("add confirms sku", "added WIDGET-1" in r.stdout, r.stdout)

print("[2] receive stock (+5)")
r = run("receive", "WIDGET-1", "5")
check("receive reports 15", "now has 15" in r.stdout, r.stdout)

print("[3] fulfill stock (-12)")
r = run("fulfill", "WIDGET-1", "12")
check("fulfill reports 3", "now has 3" in r.stdout, r.stdout)

print("[4] oversell is rejected")
r = run("fulfill", "WIDGET-1", "999")
check("oversell exits nonzero", r.returncode == 1, f"rc={r.returncode}")
check("oversell explains why", "cannot fulfill" in r.stderr, r.stderr)

print("[5] state persisted across processes")
r = run("list")
check("list shows qty=3", "x3" in r.stdout, r.stdout)
check("low-stock flag shown", "REORDER" in r.stdout, r.stdout)

print("[6] stats")
r = run("stats")
check("stats value = 29.97", '"total_value": 29.97' in r.stdout, r.stdout)

print("[7] remove cleans up")
r = run("remove", "WIDGET-1")
check("remove exits 0", r.returncode == 0, r.stderr)
r = run("stats")
check("inventory empty afterwards", '"items": 0' in r.stdout, r.stdout)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
