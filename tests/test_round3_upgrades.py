"""Round-3 coding-harness upgrades: budgets, fuzzy edit matching, symbol-aware
repo index, binary-safe grep, structured compaction fallback."""
from __future__ import annotations

from pathlib import Path


# --------------------------------------------------------------------- budgets

def test_config_defaults_raised_for_long_horizon_tasks():
    from saturday.config import AgentConfig
    from saturday.agent.loop import MAX_TOOL_CALLS_PER_STEP, TOOL_RESULT_MAX_CHARS

    cfg = AgentConfig()
    assert cfg.max_steps >= 200, "long refactors need far more than 40 turns"
    assert cfg.tool_timeout >= 120.0, "builds/compilers exceed the old 60s watchdog"
    assert MAX_TOOL_CALLS_PER_STEP >= 16
    assert TOOL_RESULT_MAX_CHARS >= 48_000


# ------------------------------------------------------------- fuzzy edit_file

def test_edit_file_exact_match_survives_crlf_files(tmp_path: Path):
    from saturday.tools.files import EditFile

    p = tmp_path / "win.txt"
    p.write_bytes(b"def main():\r\n    return 1\r\n")
    tool = EditFile(root=str(tmp_path))
    # universal newlines: read_text normalizes CRLF, so a \n old_string matches
    ok, msg = tool.run({"path": "win.txt", "old_string": "def main():\n    return 1", "new_string": "def main():\n    return 2"})
    assert ok, msg
    assert b"return 2" in p.read_bytes()


def test_edit_file_fuzzy_fallback_indentation(tmp_path: Path):
    from saturday.tools.files import EditFile

    p = tmp_path / "ind.py"
    p.write_text("if True:\n        deep_call()\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    # model emitted 4-space indent; file has 8 — flexible match still lands
    ok, msg = tool.run({"path": "ind.py", "old_string": "if True:\n    deep_call()", "new_string": "if True:\n    shallow_call()"})
    assert ok, msg
    assert "shallow_call()" in p.read_text(encoding="utf-8")


def test_edit_file_still_fails_clean_when_unfindable(tmp_path: Path):
    from saturday.tools.files import EditFile

    (tmp_path / "x.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": "x.txt", "old_string": "omega", "new_string": "??"})
    assert not ok and "not found" in msg


def test_edit_file_ambiguous_exact_still_rejected(tmp_path: Path):
    from saturday.tools.files import EditFile

    (tmp_path / "y.txt").write_text("dup dup\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": "y.txt", "old_string": "dup", "new_string": "?"})
    assert not ok and "2 times" in msg


def test_render_file_diff_works_with_fuzzy_match(tmp_path: Path):
    from saturday.repl import render_file_diff

    p = tmp_path / "f.txt"
    p.write_text("keep\r\nchange me\r\nend\r\n", encoding="utf-8")
    diff = render_file_diff(
        "edit_file",
        {"path": str(p), "old_string": "change me\n", "new_string": "changed\n"},
    )
    assert diff and "+changed" in diff and "-change me" in diff


# --------------------------------------------- AST-symbol-aware repo retrieval

def test_repo_index_boosts_defining_file_over_mentioning_file(tmp_path):
    from saturday.tools.repo_index import build_index, search_index

    (tmp_path / ".saturday").mkdir(exist_ok=True)
    # definer: tiny file that DEFINES the function
    (tmp_path / "definer.py").write_text("def frobnicate_widget(w):\n    return w\n")
    # mentioner: big file that only references the name many times
    (tmp_path / "mentioner.py").write_text(
        "x = 1\nfrobnicate_widget(x)\nfrobnicate_widget(2)\nfrobnicate_widget(3)\nfrobnicate_widget(4)\n"
        + "\n".join(f"filler_{i} = {i}" for i in range(50))
        + "\n"
    )
    idx = build_index(tmp_path, force=True)
    assert idx["files"]["definer.py"].get("symbols") == ["frobnicate_widget"]
    hits = search_index(tmp_path, "frobnicate widget", index=idx)
    assert hits[0]["path"] == "definer.py"


def test_repo_index_symbols_survive_incremental_rebuild(tmp_path):
    from saturday.tools.repo_index import build_index, search_index

    f = tmp_path / "a.py"
    f.write_text("class Widget:\n    pass\n")
    build_index(tmp_path, force=True)
    # second build takes the cached path — symbols must persist there too
    idx2 = build_index(tmp_path)
    hits = search_index(tmp_path, "Widget", index=idx2)
    assert any(h["path"] == "a.py" for h in hits)


# ------------------------------------------------------------------- grep

def test_grep_skips_binary_files(tmp_path):
    from saturday.tools.files import GrepTool

    (tmp_path / "text.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"needle\x00binary\n")
    gp = GrepTool(root=str(tmp_path))
    ok, out = gp.run({"pattern": "needle", "include": "**/*"})
    assert ok and "text.txt:1" in out and "blob.bin" not in out


def test_grep_ignore_case_flag(tmp_path):
    from saturday.tools.files import GrepTool

    (tmp_path / "c.txt").write_text("MIXEDcase value\n", encoding="utf-8")
    gp = GrepTool(root=str(tmp_path))
    ok_sensitive, out_sensitive = gp.run({"pattern": "mixedcase"})
    assert out_sensitive == "(no matches)"
    ok, out = gp.run({"pattern": "mixedcase", "ignore_case": True})
    assert ok and "c.txt:1" in out


# --------------------------------------------------- structured compaction

def test_compaction_fallback_emits_structured_sections():
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
        {"role": "user", "content": "# Goal\ndo the refactor"},
        {
            "role": "assistant",
            "content": "I chose approach B instead of A.",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "edit_file", "arguments": json.dumps({"path": "src/a.py"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "edit_file", "content": "edited src/a.py"},
        {"role": "assistant", "content": "step two"},
        {
            "role": "assistant",
            "content": "reading tests next",
            "tool_calls": [
                {
                    "id": "t2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "tests/a_test.py"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t2", "name": "read_file", "content": "contents"},
        {"role": "assistant", "content": "wrapping up soon"},
        {"role": "assistant", "content": "last words"},
    ]
    loop = AgentLoop(OneShotModel(), ToolRegistry())
    loop._compact(list(history), force=True)
    pinned = loop.memory.render()
    assert "## Progress" in pinned
    assert "## Decisions" in pinned
    assert "approach B" in pinned
    assert "## Files modified" in pinned
    assert "src/a.py" in pinned
    assert "tests/a_test.py" not in pinned  # reads are not "modified"
