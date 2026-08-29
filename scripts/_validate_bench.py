"""One-shot validation of the pinned benchmark suite: verifiers must fail on
untouched fixtures and pass on hand-solved states. Not part of the benchmark
output — a pre-run sanity gate for suite edits."""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from agent_bench import build_cases

tmp = Path(tempfile.mkdtemp(prefix="bench-validate-"))
traj = SimpleNamespace(final_answer="", steps=[], usage=SimpleNamespace(total_tokens=0))

# 1) no verifier may pass on untouched fixtures
ws = tmp / "clean"
ws.mkdir()
cases = {c.id: c for c in build_cases(ws)}
bad = [(c.id, r) for c in cases.values() if (r := c.verifier(traj)) > 0]
print("clean-workspace false passes:", bad or "NONE (correct)")

# 2) hand-solved states must pass — build_cases(ws2) seeds + binds verifiers to
#    ws2 FIRST (its seed_fixtures would overwrite hand-solved files), then we
#    overwrite fixtures with solved states
ws2 = tmp / "solved"
ws2.mkdir()
cases_solved = {c.id: c for c in build_cases(ws2)}
(ws2 / "launch.txt").write_text("Saturday online\n")
for name, text in [("a.txt", "alpha"), ("b.txt", "bravo"), ("c.txt", "charlie")]:
    (ws2 / name).write_text(text)
(ws2 / "invoice_summary.json").write_text(json.dumps({"id": "A-102", "total_eur": 129.5}))
(ws2 / "config.txt").write_text("app=demo\nenvironment=prod\nretries=3\n", newline="\n")
(ws2 / "src" / "greet.py").write_text(
    'def greet(name):\n    return f"Hello, {name}!"\n\n\ndef goodbye(name):\n    return f"Bye, {name}."\n', newline="\n")
(ws2 / "src" / "main.py").write_text(
    "from greet import greet, goodbye\n\nprint(greet('Saturday'))\nprint(goodbye('agent'))\n", newline="\n")
(ws2 / "broken" / "job.py").write_text(
    "def total(items):\n    s = 0\n    for i in items:\n        s += i\n    return s\n\n\n"
    'if __name__ == "__main__":\n    print(total([1, 2, 3, 4]))\n', newline="\n")
(ws2 / "total_units.txt").write_text("51")
(ws2 / "count_files.py").write_text("print(4)\n", newline="\n")

solved = {
    "file_write_exact": traj,
    "file_multi_create": traj,
    "file_json_transform": traj,
    "edit_fix_typo": traj,
    "edit_rename_symbol": traj,
    "edit_recover_crash": traj,
    "shell_aggregate_csv": traj,
    "script_create_run": traj,
}
bad2 = []
for cid, t in solved.items():
    r = cases_solved[cid].verifier(t)
    if r < 0.999:
        bad2.append((cid, r))
    print(f"  solved {cid:22s} -> reward={r}")
print("solved-state false fails:", bad2 or "NONE (correct)")

# 3) answer verifiers need the right final answer text
answers = {
    "file_read_extract": "code: QUASAR-9",
    "answer_csv_sum": "revenue_total: 10890",
    "search_find_string": "file: notes/meeting.txt",
    "search_count_md": "md files: 4",
    "format_template": "workspace-report\nfiles: 4\nquiet: yes\nend",
    "no_tool_precision": "result: 408",
}
bad3 = []
for cid, text in answers.items():
    t = SimpleNamespace(final_answer=text, steps=[], usage=SimpleNamespace(total_tokens=0))
    r = cases[cid].verifier(t)
    if r < 0.999:
        bad3.append((cid, r))
print("answer-verifier false fails:", bad3 or "NONE (correct)")

# 4) run the two command-embedded verifiers live (edit_fix_function, edit_recover_crash)
ok = 0
for cid in ("edit_fix_function", "edit_recover_crash"):
    r = cases_solved[cid].verifier(traj)
    print(f"  live {cid:22s} -> reward={r}")
    ok += r >= 0.999
print("live command checks:", "ALL PASS" if ok == 2 else f"only {ok}/2")
