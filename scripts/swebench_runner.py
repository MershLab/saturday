"""SWE-bench Verified/Lite batch runner for Saturday.

Follows the mini-swe-agent pattern (the reference rig for custom harnesses on
SWE-bench): one Docker eval container per instance, the agent edits /testbed
inside it, the resulting patch lands in preds.json, and grading happens with
either the free cloud evaluator (sb-cli) or the local SWE-bench harness.

Prerequisites (NOT auto-installed — this is an operator script, not part of
the zero-dependency core):
  pip install datasets                 # dataset loading only
  docker info                         # running daemon; x86_64 images
  export <PROVIDER>_API_KEY=...       # e.g. DEEPSEEK_API_KEY / OPENAI_API_KEY

Eval images: either build them locally with `python -m swebench.harness.prepare_images`
or pull the prebuilt ones from ghcr.io/epoch-research and pass
--image-source ghcr (this script applies the __ -> _1776_ tag substitution).

Smoke test first:
  python scripts/swebench_runner.py --limit 2 --smoke
Grading:
  sb-cli submit swe-bench_verified test --predictions_path runs/<id>/preds.json --run_id <id>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DATASETS = {
    "verified": "princeton-nlp/SWE-bench_Verified",
    "lite": "princeton-nlp/SWE-bench_Lite",
    "full": "princeton-nlp/SWE-bench",
}

# inside-container bootstrap: install saturday from the read-only repo mount,
# disable every surface a headless container cannot use, then run unattended.
# SATURDAY_GUARDRAILS=0 is REQUIRED here: guardrails fail closed with no
# approver, so unattended runs would block legitimate fixes like git reset.
# --max-steps is explicit because `saturday run --ci` caps the default at 25;
# long-horizon instances need far more turns than that.
RUN_TEMPLATE = r"""
set -e
export SATURDAY_SANDBOXED=1
export SATURDAY_GUARDRAILS=0
pip install --quiet --no-deps -e /opt/harness
cd /testbed
saturday run --ci \
  --max-steps "${SATURDAY_MAX_STEPS:-100}" \
  --disable web,browser,computer_use,memory,subagents \
  --provider "$SATURDAY_PROVIDER" ${SATURDAY_MODEL:+--model "$SATURDAY_MODEL"} \
  ${SATURDAY_MAX_RUN_TOKENS:+--max-run-tokens $SATURDAY_MAX_RUN_TOKENS} \
  "$(cat /task/task.md)"
git add -A
git diff --cached "$BASE_COMMIT" > /out/patch.diff || git diff --cached > /out/patch.diff
"""

# env vars the CONTAINER needs beyond the explicit -e list: any provider
# credential shape we can recognize (never logged, passed by name only)
_CRED_RX = None


def _cred_env_keys(env: dict) -> list[str]:
    import re

    global _CRED_RX
    if _CRED_RX is None:
        _CRED_RX = re.compile(r"API_KEY|TOKEN|SECRET", re.IGNORECASE)
    reserved = {"SATURDAY_PROVIDER", "SATURDAY_MODEL", "SATURDAY_MAX_RUN_TOKENS",
                "SATURDAY_MAX_STEPS", "BASE_COMMIT"}
    return sorted(k for k in env if k not in reserved and _CRED_RX.search(k))


def resolve_image(instance_id: str, source: str) -> str:
    if source == "local":
        return f"sweb.eval.x86_64.{instance_id}:latest"
    return f"ghcr.io/epoch-research/swe-bench.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def run_instance(inst: dict, out_dir: Path, args) -> dict:
    iid = inst["instance_id"]
    inst_dir = out_dir / iid
    inst_dir.mkdir(parents=True, exist_ok=True)
    preds_path = out_dir / "preds.json"
    task_file = inst_dir / "task.md"
    task_file.write_text(inst["problem_statement"], encoding="utf-8")
    patch_file = inst_dir / "patch.diff"
    # docker on Windows rejects backslash paths in volume specs
    inst_dir_posix = inst_dir.resolve().as_posix()
    harness_posix = Path(__file__).resolve().parents[1].as_posix()

    image = resolve_image(iid, args.image_source)
    # full host env for the docker CLIENT (PATH/HOME/DOCKER_* must survive);
    # only explicitly -e-named vars reach the container
    env = dict(os.environ)
    env.update({
        "SATURDAY_PROVIDER": args.provider,
        "SATURDAY_MODEL": args.model or "",
        "SATURDAY_MAX_RUN_TOKENS": str(args.max_run_tokens or ""),
        "SATURDAY_MAX_STEPS": str(args.max_steps),
        "BASE_COMMIT": inst["base_commit"],
    })
    cmd = [
        "docker", "run", "--rm", "--name", f"df-sb-{iid}",
        "-e", "SATURDAY_PROVIDER", "-e", "SATURDAY_MODEL", "-e", "SATURDAY_MAX_RUN_TOKENS",
        "-e", "SATURDAY_MAX_STEPS", "-e", "BASE_COMMIT",
        # credentials pass by NAME from the client env — values stay out of argv
        *(f"-e{k}" for k in _cred_env_keys(env)),
        "-v", f"{inst_dir_posix}:/task", "-v", f"{inst_dir_posix}:/out",
        "-v", f"{harness_posix}:/opt/harness:ro",
        *(["--platform", "linux/amd64"] if args.platform else []),
        image,
        "bash", "-c", RUN_TEMPLATE,
    ]
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            timeout=args.timeout_per_instance,
        )
        ok = proc.returncode == 0 and patch_file.exists()
        log = (proc.stdout or "") + "\n=== stderr ===\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        # subprocess kills only the local docker CLIENT; without this the
        # eval container runs on orphaned for hours
        subprocess.run(["docker", "rm", "-f", f"df-sb-{iid}"], capture_output=True, timeout=60)
        ok, log = False, f"[timeout after {args.timeout_per_instance}s; container killed]"
    (inst_dir / "run.log").write_text(log[-200_000:], encoding="utf-8")

    patch = patch_file.read_text(encoding="utf-8") if patch_file.exists() else ""
    entry = {"model_name_or_path": f"{args.provider}/{args.model or 'default'}", "model_patch": patch}
    _write_pred(preds_path, iid, entry)
    return {"instance_id": iid, "ok": ok, "seconds": round(time.time() - start, 1), "patch_bytes": len(patch)}


_PRED_LOCK = __import__("threading").Lock()


def _write_pred(preds_path: Path, iid: str, entry: dict):
    with _PRED_LOCK:
        data = {}
        if preds_path.exists():
            try:
                data = json.loads(preds_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data[iid] = entry
        tmp = preds_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(preds_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subset", default="verified", choices=list(DATASETS))
    ap.add_argument("--slice", default="", help="python slice, e.g. 0:5")
    ap.add_argument("--limit", type=int, default=0, help="first N instances")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--provider", default=os.environ.get("SATURDAY_PROVIDER", "deepseek"))
    ap.add_argument("--model", default=os.environ.get("SATURDAY_MODEL", ""))
    ap.add_argument("--max-run-tokens", type=int, default=4_000_000)
    ap.add_argument("--max-steps", type=int, default=100,
                    help="tool turns per instance (--ci caps the default at 25)")
    ap.add_argument("--timeout-per-instance", type=int, default=3600)
    ap.add_argument("--image-source", default="local", choices=["local", "ghcr"])
    ap.add_argument("--platform", dest="platform", action="store_true", default=False,
                    help="pass --platform linux/amd64 to docker (Apple Silicon)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    try:
        from datasets import load_dataset
    except ImportError:
        print("missing dependency: pip install datasets", file=sys.stderr)
        return 2

    run_id = args.out or f"swebench-{args.subset}-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir = Path("runs") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    instances = list(load_dataset(DATASETS[args.subset], split="test"))
    if args.slice:
        # single value N means "first N" (like --limit), not python [N:]
        spec = args.slice if ":" in args.slice else f"0:{args.slice}"
        lo, _, hi = spec.partition(":")
        instances = instances[int(lo or 0): int(hi) if hi else None]
    if args.limit:
        instances = instances[: args.limit]
    print(f"{len(instances)} instances -> {out_dir} (image source: {args.image_source})")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_instance, i, out_dir, args): i["instance_id"] for i in instances}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            print(f"  {r['instance_id']}: {'OK' if r['ok'] else 'FAIL'} ({r['seconds']}s, {r['patch_bytes']}B)")

    done = sum(1 for r in results if r["ok"])
    print(f"\nfinished: {done}/{len(results)} produced patches -> {out_dir/'preds.json'}")
    print("grade with:")
    print(f"  sb-cli submit swe-bench_{args.subset} test --predictions_path {out_dir}/preds.json --run_id {run_id}")
    print("or locally:")
    print(f"  python -m swebench.harness.run_evaluation --predictions_path {out_dir}/preds.json --run_id {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
