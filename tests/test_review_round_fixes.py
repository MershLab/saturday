"""Regression tests for the round-4 code-review fixes."""
from __future__ import annotations

import ast
from pathlib import Path


def _src(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# ------------------------------------------------------------- journal creates

def test_write_file_journals_creations_so_revert_can_undo_them(tmp_path):
    from saturday.tools.files import WriteFile
    from saturday.tools.journal import load_entries, restore_entry

    w = WriteFile(root=str(tmp_path))
    ok, msg = w.run({"path": "new.txt", "content": "created by agent"})
    assert ok
    entries = load_entries(tmp_path, limit=5)
    assert entries and entries[0]["existed"] is False
    # revert of a creation deletes the file (tombstone contract)
    ok_r, msg_r = restore_entry(tmp_path, 0)
    assert ok_r, msg_r
    assert not (tmp_path / "new.txt").exists()


# ------------------------------------------------------------ flexible matching

def test_flexible_match_preserves_line_boundaries():
    from saturday.tools.files import flexible_match

    text = "def f():\n    if x: return y\n"
    # old_string spans two lines in the model's mind; text has them on ONE line
    assert flexible_match(text, "if x:\n    return y") is None
    ok_text = "def f():\n    if x:\n        return y\n"
    span = flexible_match(ok_text, "if x:\n return y")
    assert span is not None
    start, end = span
    assert ok_text[start:end].startswith("if x:")


def test_flexible_match_blank_lines_and_uniqueness():
    from saturday.tools.files import flexible_match

    text = "a = 1\n\n\nb = 2\n"
    assert flexible_match(text, "a = 1\n\nb = 2") is not None
    dup = "x = 1\nfoo()\nx = 1\nfoo()\n"
    assert flexible_match(dup, "x = 1\nfoo()") is None  # ambiguous -> None


def test_edit_file_rejects_empty_old_string_cleanly(tmp_path):
    from saturday.tools.files import EditFile

    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": "f.txt", "old_string": "", "new_string": "?"})
    assert not ok and "empty" in msg
    ok, msg = tool.run({"path": "f.txt", "old_string": "   ", "new_string": "?"})
    assert not ok and "empty" in msg


# ------------------------------------------------------------------ diff preview

def test_render_file_diff_mirrors_edit_file_rules(tmp_path):
    from saturday.repl import render_file_diff

    p = tmp_path / "multi.txt"
    p.write_text("dup\nmid\ndup\n", encoding="utf-8")
    diff = render_file_diff(
        "edit_file",
        {"path": "multi.txt", "old_string": "dup", "new_string": "?"},
        root=str(tmp_path),
    )
    assert diff and "matches 2 times" in diff

    missing = render_file_diff(
        "edit_file",
        {"path": "multi.txt", "old_string": "", "new_string": "?"},
        root=str(tmp_path),
    )
    assert missing and "old_string" in missing

    # relative paths resolve against the workspace root, not CWD
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "rel.txt").write_text("target here\n", encoding="utf-8")
    ok_diff = render_file_diff(
        "edit_file",
        {"path": "sub/rel.txt", "old_string": "target", "new_string": "bullseye"},
        root=str(tmp_path),
    )
    assert ok_diff and "+bullseye" in ok_diff


def test_gates_preview_against_workspace_root_not_cwd(tmp_path, monkeypatch):
    """Behavioral: both gates must resolve relative edit paths against the
    agent's workspace root — even when the process CWD is elsewhere."""

    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.session_runtime import SessionRuntime
    from saturday.sessions import SessionStore

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "rel.txt").write_text("rooted content\n", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)  # CWD deliberately differs from the workspace

    store = SessionStore(root=tmp_path / "sess")
    agent = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(ws)),
                  safety=False, session_store=store)

    # REPL surface: gate carries the workspace root
    repl = Repl(agent, store=store, output_fn=lambda *a, **k: None)
    assert Path(repl.file_gate.root) == ws

    # web surface: same wiring
    rt = SessionRuntime("s1", agent)
    assert Path(rt.file_gate.root) == ws


# ------------------------------------------------------------------- compaction

def test_compaction_files_section_lists_only_mutations():
    import json

    from saturday.agent.loop import AgentLoop
    from saturday.tools.base import ToolRegistry

    class Echo:
        usage = None
        tool_calls = []
        content = ""

    class OneShotModel:
        def chat(self, messages, **kwargs):
            return type("R", (), {"message": Echo(), "usage": None})()

    history = [
        {"role": "user", "content": "# Goal\ngo"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "r1", "type": "function",
                         "function": {"name": "read_file", "arguments": json.dumps({"path": "read_only.py"})}}]},
        {"role": "tool", "tool_call_id": "r1", "name": "read_file", "content": "..."},
        {"role": "assistant", "content": "writing now",
         "tool_calls": [{"id": "w1", "type": "function",
                         "function": {"name": "write_file", "arguments": json.dumps({"path": "mutated.py"})}}]},
        {"role": "tool", "tool_call_id": "w1", "name": "write_file", "content": "ok"},
        {"role": "assistant", "content": "tail one"},
        {"role": "assistant", "content": "tail two"},
        {"role": "assistant", "content": "tail three"},
    ]
    loop = AgentLoop(OneShotModel(), ToolRegistry())
    loop._compact(list(history), force=True)
    pinned = loop.memory.render()
    assert "mutated.py" in pinned
    assert "read_only.py" not in pinned


# ------------------------------------------------------------------ watchdog

def test_watchdog_actually_bounds_a_hung_tool():
    """Behavioral: a tool that hangs past tool_call_timeout must not wedge the
    run — the loop returns with a timeout error well before the tool would."""
    import time as _t

    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from fakes import make_scripted_model

    from saturday.agent.loop import AgentLoop
    from saturday.tools.base import ToolRegistry, Tool

    class Sleeper(Tool):
        name = "sleep"
        description = "hangs"
        parameters = {"type": "object", "properties": {}}

        def run(self, args):
            _t.sleep(4.0)
            return True, "woke"

    reg = ToolRegistry()
    reg.register(Sleeper())
    model = make_scripted_model(
        [{"tool_calls": [{"name": "sleep", "arguments": {}}]}, {"content": "done"}]
    )
    loop = AgentLoop(model, reg, max_steps=2, tool_call_timeout=0.5)
    start = _t.monotonic()
    traj = loop.run("sys", "hang")
    elapsed = _t.monotonic() - start
    assert traj.stop_reason == "done"
    timeout_results = [r for s in traj.steps for r in (s.results or []) if not r.ok]
    assert timeout_results and "timed out after 0.5" in (timeout_results[0].error or "")
    assert elapsed < 3.0, f"watchdog did not bound the hang: {elapsed:.1f}s"


# --------------------------------------------------------------- truncation order

def test_truncated_tool_result_still_ends_with_protocol_tag():
    """Behavioral: oversized payloads are cut BEFORE wrapping, so even the
    biggest results keep their closing </tool_response> tag intact."""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from fakes import make_scripted_model

    from saturday.agent.loop import TOOL_RESULT_MAX_CHARS, AgentLoop
    from saturday.tools.base import Tool, ToolRegistry

    class Firehose(Tool):
        name = "firehose"
        description = "huge output"
        parameters = {"type": "object", "properties": {}}

        def run(self, args):
            return True, "x" * (TOOL_RESULT_MAX_CHARS * 2)

    reg = ToolRegistry()
    reg.register(Firehose())
    model = make_scripted_model(
        [{"tool_calls": [{"name": "firehose", "arguments": {}}]}, {"content": "ok"}]
    )
    traj = AgentLoop(model, reg, max_steps=2).run("sys", "flood")
    msg = traj.steps[0].tool_messages[0]
    assert len(msg["content"]) < TOOL_RESULT_MAX_CHARS + 200
    assert msg["content"].rstrip().endswith("</tool_response>")


# --------------------------------------------------------------- repo index perf

def test_symbol_terms_precomputed_at_index_time(tmp_path):
    from saturday.tools.repo_index import build_index, search_index

    (tmp_path / "s.py").write_text("def parse_hermes_tool_calls(t):\n    return t\n")
    idx = build_index(tmp_path, force=True)
    meta = idx["files"]["s.py"]
    assert "parse_hermes_tool_calls" in meta["symbol_terms"]
    hits = search_index(tmp_path, "hermes tool calls", index=idx)
    assert hits[0]["path"] == "s.py"


# ------------------------------------------------------------------ app --no-token

def test_cmd_app_no_token_maps_to_empty_not_none(monkeypatch):
    """Behavioral: `saturday app --no-token` must reach serve() as '' (auth
    disabled per AppServer contract), not None (which mints a fresh token)."""
    import argparse

    from saturday import cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(
        "saturday.webui.serve",
        lambda **kw: captured.update(kw) or 0,
    )
    ns = dict(host="127.0.0.1", port=8679, no_window=True, width=800, height=600, env=None)
    # --no-token -> empty string
    cli_mod.cmd_app(argparse.Namespace(**ns, no_token=True, token=None))
    assert captured["token"] == ""
    # default -> None (serve generates one)
    cli_mod.cmd_app(argparse.Namespace(**ns, no_token=False, token=None))
    assert captured["token"] is None


# ------------------------------------------------------------- swebench runner

def test_swebench_runner_hardening():
    src = _src("scripts/swebench_runner.py")
    ast.parse(src)  # stays syntactically valid standalone
    assert "--max-steps" in src and "SATURDAY_MAX_STEPS" in src, "--ci caps steps at 25"
    assert "docker\", \"rm\", \"-f\"" in src.replace("'", '"'), "orphan containers must be killed"
    assert "as_posix()" in src, "windows volume mounts need forward slashes"
    assert "_PRED_LOCK" in src, "preds.json writes need a real module-level lock"


def test_cred_passthrough_by_name_not_value():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "swebench_runner", Path(__file__).parents[1] / "scripts" / "swebench_runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    keys = mod._cred_env_keys({
        "DEEPSEEK_API_KEY": "sk-x",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "MY_SECRET_SAUCE": "1",
        "PATH": "/usr/bin",
        "BASE_COMMIT": "abc",
        "SATURDAY_PROVIDER": "deepseek",
    })
    assert set(keys) == {"ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY", "MY_SECRET_SAUCE"}
