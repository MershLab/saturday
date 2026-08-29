"""v0.6.0 feature layer tests not covered elsewhere: write-verification notes
and REPL /context parity."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from saturday.tools.files import EditFile, WriteFile  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})


def test_write_valid_python_has_no_warning(tmp_path):
    tool = WriteFile(root=str(tmp_path))
    ok, out = tool.run({"path": "good.py", "content": "def f():\n    return 1\n"})
    assert ok and "[verify]" not in out


def test_write_broken_python_warns_with_line(tmp_path):
    tool = WriteFile(root=str(tmp_path))
    ok, out = tool.run({"path": "bad.py", "content": "def f():\n    return =\n"})
    assert ok, "write itself must succeed"
    assert "[verify] WARNING" in out
    assert "line 2" in out
    assert (tmp_path / "bad.py").exists(), "file still written"


def test_non_python_files_not_checked(tmp_path):
    tool = WriteFile(root=str(tmp_path))
    ok, out = tool.run({"path": "notes.md", "content": "# def broken( :\n"})
    assert ok and "[verify]" not in out


def test_edit_introducing_syntax_error_warns(tmp_path):
    p = tmp_path / "app.py"
    p.write_text("x = 1\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, out = tool.run({"path": "app.py", "old_string": "x = 1", "new_string": "x = ("})
    assert ok
    assert "[verify] WARNING" in out


def test_edit_fixing_syntax_clears_warning(tmp_path):
    p = tmp_path / "app.py"
    p.write_text("x = (\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, out = tool.run({"path": "app.py", "old_string": "x = (", "new_string": "x = 1"})
    assert ok and "[verify]" not in out


# ------------------------------------------------------------------ repl /ctx

def test_repl_context_command_renders_breakdown(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.sessions import SessionStore

    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False, session_store=SessionStore(root=tmp_path / "sess"))
    collected: list[str] = []
    repl = Repl(agent, store=agent.session_store, output_fn=lambda *a, **k: collected.append(" ".join(str(x) for x in a)))
    repl._sid = agent.session_store.create({"task": "t"})
    handled = repl.dispatch("/context")
    assert handled is True
    text = "\n".join(collected)
    assert "context:" in text and "system prompt" in text


def test_version_bumped():
    import saturday

    assert saturday.__version__ >= "0.6.0"


def test_title_from_text_strips_noise():
    from saturday.webui import _title_from_text

    assert _title_from_text("```python\nprint('hi')\n```\nfix this") == "code fix this"
    assert _title_from_text("### **Bold** heading\nsecond line") == "Bold heading second line"
    assert _title_from_text("") == "(interactive)"
    assert len(_title_from_text("x" * 500)) == 60
