"""Merged from: tests/test_tools.py, tests/test_tool_toggles.py."""


from __future__ import annotations
from pathlib import Path
from saturday.tools.files import EditFile, GlobTool, GrepTool, ListDir, ReadFile, WriteFile
from saturday.tools.python_repl import PythonREPL
from saturday.tools.shell import ShellTool
import json
import sys
import urllib.error
import urllib.request
from fakes import make_scripted_model
from saturday.agent.core import Agent
from saturday.config import AgentConfig
from saturday.tools.base import ToolRegistry



# --- from tests/test_tools.py ---

def test_write_read_edit(tmp_path: Path):
    root = str(tmp_path)
    w = WriteFile(root=root)
    ok, out = w.run({"path": "sub/a.txt", "content": "hello world"})
    assert ok
    r = ReadFile(root=root)
    ok, text = r.run({"path": "sub/a.txt"})
    assert ok and "1: hello world" in text

    e = EditFile(root=root)
    ok, out = e.run({"path": "sub/a.txt", "old_string": "world", "new_string": "forge"})
    assert ok
    ok, text = r.run({"path": "sub/a.txt"})
    assert "hello forge" in text


def test_edit_rejects_ambiguous_match(tmp_path: Path):
    root = str(tmp_path)
    WriteFile(root=root).run({"path": "b.txt", "content": "x x x"})
    ok, err = EditFile(root=root).run({"path": "b.txt", "old_string": "x", "new_string": "y"})
    assert not ok and "3 times" in err


def test_path_escape_blocked(tmp_path: Path):
    r = ReadFile(root=str(tmp_path))
    ok, err = r.run({"path": "../../etc/passwd"})
    assert not ok and "escapes" in err


def test_glob_and_grep(tmp_path: Path):
    root = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("VALUE = 42\n")
    (tmp_path / "notes.md").write_text("the VALUE here\n")
    g = GlobTool(root=root)
    ok, out = g.run({"pattern": "**/*.py"})
    assert ok and "src/m.py" in out
    gp = GrepTool(root=root)
    ok, out = gp.run({"pattern": r"VALUE\s*=\s*42", "include": "**/*"})
    assert ok and "src/m.py:1" in out


def test_listdir_and_shell(tmp_path: Path):
    ld = ListDir(root=str(tmp_path))
    ok, out = ld.run({})
    assert ok
    sh = ShellTool(root=str(tmp_path))
    ok, out = sh.run({"command": "echo saturday"})
    assert ok and "saturday" in out


def test_python_repl_persistence():
    repl = PythonREPL()
    try:
        ok, _ = repl.run({"code": "z = 6 * 7"})
        assert ok
        ok, out = repl.run({"code": "print(z)"})
        assert ok and "42" in out
        ok, err = repl.run({"code": "1/0"})
        assert not ok and "ZeroDivisionError" in err
    finally:
        repl.close()



# --- from tests/test_tool_toggles.py ---

sys.path.insert(0, str(Path(__file__).parent))


def test_family_expansion():
    assert ToolRegistry.expand_tool_names(["web"]) == {"web_search", "web_fetch"}
    assert ToolRegistry.expand_tool_names(["web", "python", "read_file"]) == {
        "web_search", "web_fetch", "python", "read_file"
    }
    assert ToolRegistry.expand_tool_names([]) == set()


def test_excluding_view():
    reg = ToolRegistry()

    class T:
        def __init__(self, name):
            self.name = name

        def run(self, args):
            return True, "ok"

    for n in ("shell", "web_search", "read_file"):
        reg.register(T(n))
    view = reg.excluding({"web_search"})
    assert view.names() == ["read_file", "shell"]
    assert view.get("web_search") is None
    assert reg.get("web_search") is not None  # original untouched


def _agent_with_tools(cfg=None):
    agent = Agent(
        cfg=cfg or AgentConfig(provider="openai", model="m"),
        client=make_scripted_model([{"content": "ok"}]),
        enable_subagents=False,
    )
    agent._ensure_client = lambda: agent.client
    return agent


def test_cfg_disabled_tools_hidden_from_run():
    cfg = AgentConfig(provider="openai", model="m", disabled_tools=["web"])
    agent = _agent_with_tools(cfg)
    names = set(agent.effective_registry().names())
    assert "web_search" not in names and "web_fetch" not in names  # family expanded
    assert "read_file" in names and "shell" in names               # rest intact
    # single-name disable leaves siblings alone
    agent2 = _agent_with_tools(AgentConfig(provider="openai", model="m", disabled_tools=["shell"]))
    n2 = agent2.effective_registry().names()
    assert "shell" not in n2 and "python" in n2


def test_session_scoped_toggle_does_not_touch_config():
    cfg = AgentConfig(provider="openai", model="m")
    agent = _agent_with_tools(cfg)
    ok, msg, now_off = agent.toggle_tool("computer_use")
    assert ok and now_off
    assert "pointer" in agent.disabled_tools and "ui_invoke" in agent.disabled_tools
    assert cfg.disabled_tools == []  # global config untouched by session toggle
    ok2, msg2, now_off2 = agent.toggle_tool("computer_use")
    assert ok2 and not now_off2
    assert agent.disabled_tools == set()
    # unknown name rejected with families hint
    ok3, msg3, _ = agent.toggle_tool("nope")
    assert not ok3 and "families" in msg3
    # single tool toggle
    ok4, _, off4 = agent.toggle_tool("shell")
    assert ok4 and off4 and "shell" in agent.disabled_tools


def test_plan_mode_and_toggles_compose():
    cfg = AgentConfig(provider="openai", model="m")
    agent = _agent_with_tools(cfg)
    agent.toggle_tool("memory")
    agent.plan_mode = True
    names = set(agent.effective_registry().names())
    assert "memory" not in names          # session toggle
    assert "write_file" not in names      # plan mode filter
    assert "grep" in names                # read-only survivor


def test_config_load_accepts_comma_string(tmp_path, monkeypatch):
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    (tmp_path / "config.json").write_text('{"disabled_tools": "web, shell"}', encoding="utf-8")
    loaded = cfgmod.AgentConfig.load()
    assert loaded.disabled_tools == ["web", "shell"]


def _server(app):
    import threading

    from saturday.webui import AppServer

    http = AppServer(("127.0.0.1", 0), app, token="tok")
    base = f"http://127.0.0.1:{http.server_address[1]}"
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return base, "tok"


def _post_json(base, path, payload, token):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"X-Saturday-Token": token, "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_apply_config_disabled_tools_validation_and_state(monkeypatch, tmp_path):

    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "s")
    monkeypatch.setattr("saturday.config.save_config", lambda partial: None)
    base, tok = _server(app)
    status, body = _post_json(base, "/api/config", {"disabled_tools": ["web", "nope"]}, tok)
    assert status == 400 and "unknown tool or family 'nope'" in body["error"]

    status, body = _post_json(base, "/api/config", {"disabled_tools": "web"}, tok)
    assert status == 200 and "disabled_tools" in body["applied"]
    assert sorted(body["disabled_tools"]) == ["web_fetch", "web_search"]

    status, body = _post_json(base, "/api/config", {"disabled_tools": []}, tok)
    assert status == 200 and body["disabled_tools"] == []


def test_glob_skips_matches_outside_workspace(tmp_path):
    from saturday.tools.files import GlobTool

    (tmp_path / "outside.py").write_text("x = 1", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inner.py").write_text("x = 2", encoding="utf-8")

    tool = GlobTool(root=str(root))
    # '../*.py' from the workspace root points at the PARENT dir: every match
    # resolves outside the workspace and must be dropped
    ok, out = tool.run({"pattern": "../*.py"})
    assert ok and out == "(no matches)", out
    # the workspace itself stays fully visible
    ok, out = tool.run({"pattern": "*.py"})
    assert ok and out == "inner.py", out


def test_grep_skips_matches_outside_workspace(tmp_path):
    from saturday.tools.files import GrepTool

    (tmp_path / "outside.py").write_text("SECRET_TOKEN = 'leak'", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inner.py").write_text("SECRET_TOKEN = 'fine'", encoding="utf-8")

    tool = GrepTool(root=str(root))
    ok, out = tool.run({"pattern": "SECRET_TOKEN", "include": "../*.py"})
    assert ok and out == "(no matches)", "content from outside the workspace must not leak"
    ok, out = tool.run({"pattern": "SECRET_TOKEN", "include": "*.py"})
    assert ok and "inner.py" in out and "fine" in out


def test_glob_still_finds_nested_files(tmp_path):
    from saturday.tools.files import GlobTool

    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("pass", encoding="utf-8")
    ok, out = GlobTool(root=str(root)).run({"pattern": "**/*.py"})
    assert ok and "src/app.py" in out
