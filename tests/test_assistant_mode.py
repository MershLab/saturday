"""Personal assistant mode v2: FULL capability, hidden plumbing.

The registry is identical in both modes; the differences are the persona
(outcome-reporting, non-intrusive), background-first computer use by default,
and UI-level hiding. Tests cover prompt, config semantics, runtime behavior
and CLI plumbing."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from saturday.agent.core import Agent  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.prompts.system import build_system_prompt  # noqa: E402

TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    import os

    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    for k in [k for k in os.environ if k.startswith("SATURDAY_")]:
        monkeypatch.delenv(k)


def _agent(**kw) -> Agent:
    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(Path.cwd()), **kw)
    return Agent(cfg=cfg, safety=False)


def _names(agent: Agent) -> set:
    return set(agent._build_registry().names())


def test_registry_identical_across_modes():
    """Assistant mode HIDES plumbing in UX, never removes capability."""
    agent_names = _names(_agent())
    assistant_names = _names(_agent(persona_mode="assistant"))
    assert agent_names == assistant_names
    # the world-acting tools must all be present in assistant mode
    for must in ("shell", "python", "pointer", "keyboard", "ui_invoke", "app_open",
                 "screen", "web_search", "browser", "write_file", "memory", "todo"):
        assert must in assistant_names, must


def test_assistant_prompt_outcome_focused_and_non_intrusive():
    reg = _agent()._build_registry()
    assistant = build_system_prompt(reg, persona_mode="assistant", workspace_root=".")
    default = build_system_prompt(reg, persona_mode="agent", workspace_root=".")
    assert "hands-free operator" in assistant
    assert "NON-INTRUSIVE" in assistant and "window=<title>" in assistant
    assert "never describe commands" in assistant.lower() or "report outcomes" in assistant.lower()
    assert "personal assistant mode" not in default
    # light planning only: the heavy dev reasoning protocol stays out
    assert "Reasoning protocol" not in assistant
    assert "background-first" in assistant


def test_default_agent_prompt_unchanged():
    reg = _agent()._build_registry()
    default = build_system_prompt(reg, workspace_root=".")
    assert "state-of-the-art autonomous software engineering" in default
    assert "Reasoning protocol" in default


class _Server:
    def __init__(self, app):
        from saturday.webui import AppServer

        self.http = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.http.shutdown()
        self.http.server_close()


def _make_app(tmp_path, turns=None):
    from fakes import make_scripted_model
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    fake = make_scripted_model(turns or [{"content": "ok"}])
    orig = app._new_agent

    def patched(cfg):
        agent = orig(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    return app


def _req(base, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("X-Saturday-Token", TOKEN)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def test_enabling_assistant_defaults_background_first(tmp_path: Path):
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/config", "POST", {"persona_mode": "assistant"})
        assert status == 200
        assert data["persona_mode"] == "assistant"
        assert data["background_only"] is True, "assistant works while you work"

    # explicit override wins over the default
    app2 = _make_app(tmp_path / "b")
    with _Server(app2) as srv:
        status, data = _req(srv.base, "/api/config", "POST",
                            {"persona_mode": "assistant", "desktop_background_only": False})
        assert status == 200 and data["persona_mode"] == "assistant" and data["background_only"] is False

    # switching back to agent leaves the bg flag where the user put it
    with _Server(app2) as srv:
        status, data = _req(srv.base, "/api/config", "POST", {"persona_mode": "agent"})
        assert status == 200 and data["persona_mode"] == "agent" and data["background_only"] is False


def test_persona_toggle_keeps_tools_and_updates_prompt_live(tmp_path: Path):
    app = _make_app(tmp_path)
    sid = app.store.create({"task": "am", "surface": "app"})
    rt = app.runtime_for(sid)
    before = _names(rt.agent)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/config", "POST", {"persona_mode": "assistant"})
        assert status == 200
    assert _names(rt.agent) == before, "no capability may disappear in assistant mode"
    sysp = rt.agent.system_prompt(rt.agent.registry)
    assert "hands-free operator" in sysp
    with _Server(app) as srv:
        _req(srv.base, "/api/config", "POST", {"persona_mode": "agent"})
    assert "hands-free operator" not in rt.agent.system_prompt(rt.agent.registry)


def test_invalid_persona_mode_ignored(tmp_path: Path):
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/config", "POST", {"persona_mode": "bogus"})
        assert status == 200 and data["persona_mode"] == "agent"


def test_cli_assistant_flag_sets_override():
    from saturday.cli import _overrides

    args = argparse.Namespace(provider=None, model=None, temperature=None, max_steps=None, assistant=True)
    assert _overrides(args)["persona_mode"] == "assistant"
    args2 = argparse.Namespace(provider=None, model=None, temperature=None, max_steps=None, assistant=False)
    assert _overrides(args2)["persona_mode"] is None


# ------------------------------------------------------------- identity layer

def test_identity_injected_into_prompt():
    reg = _agent()._build_registry()
    plain = build_system_prompt(reg, persona_mode="assistant", workspace_root=".")
    assert 'go by "Jarvis"' not in plain
    named = build_system_prompt(
        reg, persona_mode="assistant", workspace_root=".",
        assistant_name="Jarvis", assistant_user_title="sir",
    )
    assert 'go by "Jarvis"' in named
    assert 'Address the user as "sir"' in named
    assert "mission debrief" in named
    # agent mode must stay clean of the identity block
    assert "Identity & voice" not in build_system_prompt(reg, persona_mode="agent", workspace_root=".")


def test_identity_config_roundtrip_validation_and_clone_sync(tmp_path: Path):
    app = _make_app(tmp_path)
    sid = app.store.create({"task": "ident", "surface": "app"})
    rt = app.runtime_for(sid)  # project-less runtime shares base cfg object
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/config", "POST", {"persona_mode": "assistant"})
        assert status == 200
        status, data = _req(srv.base, "/api/config", "POST",
                            {"assistant_name": "Jarvis", "assistant_user_title": "sir"})
        assert status == 200
        assert data["assistant_name"] == "Jarvis" and data["assistant_user_title"] == "sir"
        assert rt.agent.cfg.assistant_name == "Jarvis"

        status, data = _req(srv.base, "/api/config", "POST", {"assistant_name": "x" * 41})
        assert status == 400
        status, data = _req(srv.base, "/api/config", "POST", {"assistant_name": "two\nlines"})
        assert status == 400

        status, data = _req(srv.base, "/api/config", "POST", {"assistant_name": ""})
        assert status == 200 and data["assistant_name"] == "", "empty clears the name"

    sysp = rt.agent.system_prompt(rt.agent.registry)
    assert 'go by "Jarvis"' not in sysp or rt.agent.cfg.assistant_name == "Jarvis"


def test_project_clone_receives_identity(tmp_path: Path):
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions2",
        projects_store=ProjectStore(tmp_path / "p2.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    app.base_cfg.assistant_name = "Friday"
    app.base_cfg.persona_mode = "assistant"
    sid = app.store.create({"task": "clone-id", "surface": "app"})
    proj_ws = tmp_path / "pws"
    proj_ws.mkdir()
    _, d = None, None

    with _Server(app) as srv:
        _, d = _req(srv.base, "/api/projects", "POST", {"name": "P", "workspace": str(proj_ws)})
        pid = d["project"]["id"]
        payload = {"text": "hello", "project_id": pid}
        r = urllib.request.Request(srv.base + "/api/chat", data=json.dumps(payload).encode(), method="POST")
        r.add_header("X-Saturday-Token", TOKEN)
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=120) as resp:
            resp.read()
    proj_rt = next(rt for rt in app.runtimes.values() if rt.project_id == pid)
    rcfg = proj_rt.agent.cfg
    assert rcfg is not app.base_cfg, "project runtime holds a clone"
    assert rcfg.assistant_name == "Friday"
