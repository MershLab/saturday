"""Saturday pinned agentic benchmark — 16 tool-use cases, mechanically verified.

This is the "pinned slice" from BENCHMARKING.md: a stable, deterministic suite
for measuring HARNESS quality (can the agent actually use tools to finish real
work), independent of model choice. Every case runs in an isolated temp
workspace; every verifier inspects end state (files / command exit codes /
exact answers), never the narrative. Trajectories are saved with provenance
stamps for audit.

Run against any provider:

    python scripts/agent_bench.py --provider ollama --model qwen2.5-coder:7b \
        --out eval_runs/agent-bench-qwen7b

Unattended contract (mirrors BENCHMARKING.md): safety_mode=autonomous inside
disposable temp workspaces; hardline blocklist still applies. Results print as
a PASS/FAIL table plus a summary JSON written to <out>/summary.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from saturday.eval.runner import EvalCase, EvalRunner, command_exits_zero, composite, file_created, regex_matches  # noqa: E402

CSV_TEXT = (
    "region,units,revenue\n"
    "north,12,2400\n"
    "south,7,1300\n"
    "east,22,5100\n"
    "west,7,1450\n"
    "north,3,640\n"
)

MEETING_TEXT = "Q1 wrap: the launch code is QUASAR-9. Keep it internal.\nNext sync Monday.\n"
# greet() ships BROKEN (missing space) so edit cases have a real defect to fix.
GREET_TEXT = 'def greet(name):\n    return f"Hello,{name}!"\n\n\ndef farewell(name):\n    return f"Bye, {name}."\n'
# main.py uses BOTH symbols, so renaming farewell in greet.py forces a matching
# update here before `python src/main.py` can exit 0 again.
MAIN_TEXT = "from greet import greet, farewell\n\nprint(greet('Saturday'))\nprint(farewell('agent'))\n"
JOB_TEXT = (
    "import sys\n\n\ndef total(items):\n    s = 0\n    for i in items:\n"
    "        s =+ i   # bug: assignment, not addition\n    return s\n\n\n"
    'if __name__ == "__main__":\n    print(total([1, 2, 3, 4]))\n'
)
CONFIG_TEXT = "app=demo\nenviroment=prod\nretries=3\n"


def seed_fixtures(ws: Path) -> None:
    def seed(rel: str, text: str) -> None:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    seed("data/sales_q1.csv", CSV_TEXT)
    seed("notes/meeting.txt", MEETING_TEXT)
    seed("src/greet.py", GREET_TEXT)
    seed("src/main.py", MAIN_TEXT)
    seed("broken/job.py", JOB_TEXT)
    seed("config.txt", CONFIG_TEXT)
    seed("invoice.json", json.dumps({"invoice": "A-102", "total": 129.5, "currency": "EUR", "lines": 3}))
    for i in range(4):
        seed(f"docs/guide{i}.md", f"# guide {i}\ncontent\n")
    seed("docs/img.png.txt", "not a markdown, despite the png in the name")


def build_cases(root: Path) -> list[EvalCase]:
    """Seed fixtures into `root` and return the 16 cases with verifiers bound
    to that workspace (verifiers capture root at construction time — the
    EvalRunner never injects it)."""
    seed_fixtures(root)

    def fs(p, must=()):
        return file_created(p, must_contain=must, root=str(root))

    def cz(cmd):
        return command_exits_zero(cmd, root=str(root))
    return [
        # ---- file operations -------------------------------------------------
        EvalCase(
            id="file_write_exact",
            task="Create a file named 'launch.txt' in the current directory containing exactly one line: Saturday online",
            verifier=fs("launch.txt", ("Saturday online",)),
        ),
        EvalCase(
            id="file_multi_create",
            task=("Create three files in the current directory: a.txt containing 'alpha', b.txt containing 'bravo', "
                  "and c.txt containing 'charlie'."),
            verifier=composite(
                fs("a.txt", ("alpha",)),
                fs("b.txt", ("bravo",)),
                fs("c.txt", ("charlie",)),
            ),
        ),
        EvalCase(
            id="file_read_extract",
            task="Read notes/meeting.txt and report the launch code in this exact format: code: <value>",
            verifier=regex_matches(r"code:\s*QUASAR-9\b"),
        ),
        EvalCase(
            id="file_json_transform",
            task=("Read invoice.json and create invoice_summary.json in the current directory as a JSON object with "
                  "exactly two keys: id (the invoice value) and total_eur (the numeric total)."),
            verifier=cz(
                "python -c \"import json;d=json.load(open('invoice_summary.json'));assert d=={'id':'A-102','total_eur':129.5},d\"",
            ),
        ),
        # ---- editing ----------------------------------------------------------
        EvalCase(
            id="edit_fix_typo",
            task=("config.txt misspells 'environment' as 'enviroment'. Fix that key in place; keep every other line "
                  "and its order unchanged."),
            verifier=cz(
                "python -c \"lines=[l.strip() for l in open('config.txt') if l.strip()];"
                "assert lines.index('environment=prod')==1 and not any('enviroment' in l for l in lines),lines\"",
            ),
        ),
        EvalCase(
            id="edit_fix_function",
            task=("src/greet.py's greet() must return exactly 'Hello, <name>!' (single space after the comma). It "
                  "currently doesn't. Fix it, then verify from the current directory with: "
                  "python -c \"import sys;sys.path.insert(0,'src');from greet import greet;assert greet('X')=='Hello, X!'\""),
            verifier=cz(
                "python -c \"import sys;sys.path.insert(0,'src');from greet import greet;assert greet('X')=='Hello, X!',repr(greet('X'))\"",
            ),
        ),
        EvalCase(
            id="edit_rename_symbol",
            task=("Rename the function farewell to goodbye everywhere it is used (src/greet.py defines it; src/main.py "
                  "uses it). After the rename, python src/main.py must still run and print both greetings."),
            verifier=composite(
                cz("python -c \"import sys;sys.path.insert(0,'src');import greet as g;assert hasattr(g,'goodbye') and not hasattr(g,'farewell'),dir(g)\""),
                cz("python -c \"import subprocess,sys;p=subprocess.run([sys.executable,'src/main.py'],capture_output=True,text=True);out=p.stdout;assert p.returncode==0 and 'Hello, Saturday!' in out and 'Bye, agent.' in out,(p.returncode,out,p.stderr)\""),
            ),
        ),
        EvalCase(
            id="edit_recover_crash",
            task=("broken/job.py crashes instead of printing 10. Find the bug, make the minimal fix, and verify "
                  "`python broken/job.py` prints 10 and exits 0."),
            verifier=cz(
                "python -c \"import subprocess,sys;p=subprocess.run([sys.executable,'broken/job.py'],capture_output=True,text=True);"
                "assert p.returncode==0 and p.stdout.strip()=='10',(p.returncode,p.stdout,p.stderr)\"",
            ),
        ),
        # ---- shell / python ----------------------------------------------------
        EvalCase(
            id="shell_aggregate_csv",
            task=("Using data/sales_q1.csv: compute the total of the units column across all data rows (exclude the "
                  "header) and write just that number to total_units.txt in the current directory."),
            verifier=cz(
                "python -c \"t=open('total_units.txt').read().strip();assert t=='51',t\"",
            ),
        ),
        _answer_case(
            "answer_csv_sum",
            ("Read data/sales_q1.csv and answer with the sum of the revenue column over all data rows. "
             "Final line must be exactly: revenue_total: <number>"),
            r"revenue_total:\s*10890\b",
        ),
        EvalCase(
            id="script_create_run",
            task=("Write a python script count_files.py in the current directory that prints how many .md files exist "
                  "under docs/ (recursively), then run it. The script must print exactly: 4"),
            verifier=cz(
                "python -c \"import subprocess,sys;p=subprocess.run([sys.executable,'count_files.py'],capture_output=True,text=True);"
                "assert p.returncode==0 and p.stdout.strip()=='4',(p.returncode,p.stdout,p.stderr)\"",
            ),
        ),
        # ---- search ------------------------------------------------------------
        _answer_case(
            "search_find_string",
            "Search the workspace for the string QUASAR and report which file contains it, as: file: <path>",
            r"file:\s*(notes[/\\]meeting\.txt|notes\.meeting)",
        ),
        _answer_case(
            "search_count_md",
            "How many .md files are anywhere under docs/? Final line must be exactly: md files: N",
            r"md files:\s*4\b",
        ),
        # ---- multi-step / precision ---------------------------------------------
        EvalCase(
            id="multi_step_chain",
            task=("Three dependent steps, all in the current directory: (1) create seed.txt containing the number 6; "
                  "(2) create square.txt containing 6 squared; (3) create cube.txt containing 6 cubed. "
                  "Derive each from the previous file, don't just hardcode."),
            verifier=composite(
                fs("seed.txt", ("6",)),
                fs("square.txt", ("36",)),
                fs("cube.txt", ("216",)),
            ),
        ),
        _answer_case(
            "format_template",
            ("Report on this workspace in exactly this four-line template with no extra lines:\n"
             "workspace-report\nfiles: <how many .md files are under docs/>\nquiet: yes\nend"),
            r"workspace-report\s*\nfiles:\s*4\s*\nquiet:\s*yes\s*\nend",
        ),
        _answer_case(
            "no_tool_precision",
            "Without using any tools, answer: what is 17 * 24? Final line must be exactly: result: <number>",
            r"result:\s*408\b",
        ),
    ]


def _answer_case(case_id: str, task: str, pattern: str) -> EvalCase:
    return EvalCase(id=case_id, task=task, verifier=regex_matches(pattern))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--out", default="eval_runs/agent-bench")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--max-run-tokens", type=int, default=120_000)
    ap.add_argument("--only", default="", help="comma-separated case ids to run a subset")
    ap.add_argument("--extra-instruction", default="", help="appended to every task (e.g. /no_think for qwen3)")
    ap.add_argument("--tag", default="", help="label recorded into summary.json")
    args = ap.parse_args()

    from saturday.config import AgentConfig
    from saturday.agent.core import Agent

    # the outer build only supplies the id/task list (its verifiers are
    # throwaway — real root-bound verifiers are rebuilt per case below)
    cases = build_cases(Path(tempfile.mkdtemp(prefix="bench-ids-")))
    if args.only:
        want = {c.strip() for c in args.only.split(",") if c.strip()}
        cases = [c for c in cases if c.id in want]

    cfg_over = {
        "provider": args.provider,
        "safety_mode": "autonomous",
        "max_steps": args.max_steps,
        "max_run_tokens": args.max_run_tokens,
    }
    if args.model:
        cfg_over["model"] = args.model

    base = Path(tempfile.mkdtemp(prefix="saturday-bench-"))
    rows = []
    t0 = time.time()
    for spec in cases:
        ws = base / spec.id
        ws.mkdir(parents=True, exist_ok=True)
        # verifiers bind their workspace at construction, so each case is
        # rebuilt against its own ws (id/task are identical across builds)
        case = next(c for c in build_cases(ws) if c.id == spec.id)
        if args.extra_instruction:
            case = EvalCase(id=case.id, task=case.task + chr(10) * 2 + args.extra_instruction, verifier=case.verifier)
        runner = EvalRunner(
            lambda: Agent(cfg=AgentConfig.load({**cfg_over, "workspace_root": str(ws)})),
            out_dir=str(Path(args.out) / "trajectories"),
            root=str(ws),
            provenance={"provider": args.provider, "model": args.model or "(provider default)"},
        )
        tc0 = time.time()
        try:
            (res,) = runner.run([case])
        except Exception as exc:
            # one flaky provider response must not kill the suite: record and move on
            err = f"{type(exc).__name__}: {exc}"[:200]
            rows.append({
                "id": case.id, "reward": 0.0, "stop": "error",
                "steps": 0, "tokens": 0, "secs": round(time.time() - tc0, 1), "error": err,
            })
            print(f"[ERROR] {case.id:22s} {err}", flush=True)
            continue
        rows.append({
            "id": case.id,
            "reward": res.reward,
            "stop": res.stop_reason,
            "steps": res.steps,
            "tokens": res.total_tokens,
            "secs": round(time.time() - tc0, 1),
        })
        mark = "PASS" if res.reward >= 0.999 else "FAIL"
        print(f"[{mark}] {case.id:22s} reward={res.reward:.2f} steps={res.steps:3d} "
              f"tokens={res.total_tokens:7d} {res.stop_reason} ({rows[-1]['secs']}s)", flush=True)

    n = len(rows) or 1
    summary = {
        "suite": "saturday-pinned-agentic-v1",
        "tag": args.tag or f"{args.provider}/{args.model or 'default'}",
        "provider": args.provider,
        "model": args.model or "(provider default)",
        "cases": len(rows),
        "pass_rate": sum(1 for r in rows if r["reward"] >= 0.999) / n,
        "mean_reward": sum(r["reward"] for r in rows) / n,
        "total_tokens": sum(r["tokens"] for r in rows),
        "wall_secs": round(time.time() - t0, 1),
        "python": sys.version.split()[0],
        "rows": rows,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    print(f"workspaces: {base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
