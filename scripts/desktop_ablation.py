#!/usr/bin/env python
"""Desktop grounding ablation rig — measure the harness, not the model.

Runs a verifiable local task suite under different grounding layers
(structural / textual / visual) and writes runs/ablation-<ts>.json plus a
summary table. Variants: full, no-textual, no-structural, vision-only.

For publication-grade numbers, replace the task list with the official
Windows Agent Arena harness (microsoft/Windows-Agent-Arena) — the variant
matrix and metrics are identical. This is the measurement rig for "how much
does Saturday's harness contribute".

Usage:
  python scripts/desktop_ablation.py --variants full,vision-only
  python scripts/desktop_ablation.py --dry
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    from saturday.ablation import VARIANTS, run_ablation

    ap = argparse.ArgumentParser(prog="saturday-ablation", description="Grounding-layer ablation rig")
    ap.add_argument("--variants", default=",".join(VARIANTS), help="comma-separated: " + ",".join(VARIANTS))
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ap.add_argument("--run-dir", default="runs", help="output dir (default runs/)")
    ap.add_argument("--workspace", default=".", help="agent workspace (default cwd)")
    ap.add_argument("--dry", action="store_true", help="print the matrix only, run nothing")
    args = ap.parse_args()

    variants = [v.strip() for v in args.variants.split(",") if v.strip() in VARIANTS]
    from saturday.ablation import make_tasks

    wanted = [t.strip() for t in args.tasks.split(",") if t.strip()]
    tasks = [t for t in make_tasks() if t["id"] in wanted] if wanted else None
    if args.dry:
        for v in variants:
            for t in (tasks or make_tasks()):
                print(f"would run  variant={v:<14} task={t['id']}")
        return 0

    payload = run_ablation(tasks=tasks, variants=variants, workspace=args.workspace, out_dir=args.run_dir)
    for r in payload["results"]:
        marks = "PASS" if r["ok"] else "FAIL"
        print(f"{r['variant']:<14} {r['task']:<16} {marks:4} steps={r['steps']:<3} "
              f"tokens={r['tokens']:<6} {r['seconds']:>6.1f}s  {r['detail'][:100]}")
    print()
    for variant, s in payload["summary"].items():
        print(f"{variant:<14} {s['passed']}/{s['total']} pass  avg_steps={s['avg_steps']} "
              f"avg_tokens={s['avg_tokens']:.0f} avg={s['avg_seconds']}s")
    print(f"\nresults -> {payload['_path']}")
    failed = any(not r["ok"] for r in payload["results"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
