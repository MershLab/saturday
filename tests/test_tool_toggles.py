"""Tool-toggle regressions: blocklist semantics, family expansion, per-session
/toggle override, webui config API validation + persistence, CLI flag."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model

from saturday.agent.core import Agent
from saturday.config import AgentConfig
from saturday.tools.base import ToolRegistry


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


# ------------------------------------------------------- config load + api


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
