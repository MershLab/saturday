"""Context breakdown: estimation math, agent facade, /api/context endpoint and
the /context slash command."""
from __future__ import annotations

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
from saturday.context import analyze_context, render_text  # noqa: E402

TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import os

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    for k in [k for k in os.environ if k.startswith("SATURDAY_")]:
        monkeypatch.delenv(k)


def test_sections_sum_to_total_and_roles_counted():
    history = [
        {"role": "user", "content": "x" * 400},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "content": "y" * 200},
    ]
    bd = analyze_context(system_prompt="s" * 100, history=history, max_context_tokens=10_000, compact_above_tokens=5_000)
    assert bd["total"] == sum(s["tokens"] for s in bd["sections"])
    keys = {s["key"]: s["tokens"] for s in bd["sections"]}
    assert keys["system"] == 25
    assert keys["user"] == 100
    assert bd["messages"]["assistant"] == 1
    assert bd["messages"]["tool"] == 1
    assert bd["user_turns"] == 1
    assert not bd["will_compact"]
    assert bd["prompt_tokens"] < bd["total"], "reply headroom excluded from prompt estimate"
    assert bd["usage_pct"] == pytest.approx(bd["total"] / 10_000 * 100, abs=0.2)
    assert bd["prompt_pct"] == pytest.approx(bd["prompt_tokens"] / 5_000 * 100, abs=0.3)


def test_images_billed_and_separated():
    img_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}
    history = [{"role": "user", "content": [{"type": "text", "text": "see pic"}, img_part]}]
    bd = analyze_context(history=history, tool_specs=None, include_tool_schemas=False)
    keys = {s["key"]: s["tokens"] for s in bd["sections"]}
    assert bd["images"] == 1
    assert keys["images"] > 0
    assert keys["user"] > 0


def test_tool_schemas_only_when_included():
    spec = {"name": "shell", "description": "d" * 80, "parameters": {"type": "object"}}
    with_tools = analyze_context(tool_specs=[spec], include_tool_schemas=True)
    without = analyze_context(tool_specs=[spec], include_tool_schemas=False)
    tk = lambda b: next(s["tokens"] for s in b["sections"] if s["key"] == "tools")  # noqa: E731
    assert tk(with_tools) > 0
    assert tk(without) == 0
    assert with_tools["total"] > without["total"]


def test_compaction_flags():
    big = [{"role": "user", "content": "z" * (60_000 * 4 + 8)}]
    bd = analyze_context(history=big, compact_above_tokens=60_000, include_tool_schemas=False)
    assert bd["will_compact"] is True
    txt = render_text(bd)
    assert "compaction" in txt.lower()


def test_render_text_lists_sections():
    bd = analyze_context(
        system_prompt="hello world",
        history=[{"role": "user", "content": "hi"}],
        tool_specs=[{"name": "t", "description": "x" * 40, "parameters": {}}],
        include_tool_schemas=True,
    )
    txt = render_text(bd)
    for label in ("system prompt", "tool schemas", "user messages"):
        assert label in txt


def test_agent_facade_counts_registry(tmp_path):
    cfg = AgentConfig(provider="openai", model="gpt-4o-mini", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False)
    bd = agent.context_breakdown([])
    tools_row = next(s for s in bd["sections"] if s["key"] == "tools")
    assert tools_row["tokens"] > 0, "native mode bills tool schemas"
    detail = tools_row.get("detail") or {}
    assert detail.get("count", 0) >= 5
    sys_row = next(s for s in bd["sections"] if s["key"] == "system")
    assert sys_row["detail"]["stable"] > 0


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

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(r, timeout=120) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        try:
            return e.code, {}
        except Exception:
            return e.code, {}


def test_api_context_endpoint_grows_with_history(tmp_path):
    app = _make_app(tmp_path)
    sid = app.store.create({"task": "ctx", "surface": "app"})
    app.store.save_checkpoint(sid, [{"role": "user", "content": "w" * 800}])
    with _Server(app) as srv:
        status, body = _req(srv.base, "/api/context?sid=" + sid)
        assert status == 200
        bd = json.loads(body)
        assert bd["sid"] == sid
        empty = {s["key"]: s["tokens"] for s in analyze_context()["sections"]}
        keys = {s["key"]: s["tokens"] for s in bd["sections"]}
        assert keys["user"] >= empty["user"] + len("w" * 800) // 4 - 10
        # unknown sid must NOT mint a cached runtime: refused with 404
        status, body = _req(srv.base, "/api/context?sid=nope")
        assert status == 404
        assert "nope" not in app.runtimes, "unknown sessions must not create runtimes"


def test_slash_context_returns_notice(tmp_path):
    app = _make_app(tmp_path)
    sid = app.store.create({"task": "slashctx", "surface": "app"})
    rt = app.runtime_for(sid)
    events = rt.__class__ and __import__("saturday.webui", fromlist=["handle_slash"]).handle_slash(rt, "/context")
    assert events and events[0]["t"] == "notice"
    assert "context:" in events[0]["s"]


def test_live_ctx_events_published_per_step(tmp_path):
    app = _make_app(tmp_path, turns=[{"content": "done answer"}])
    sid = app.store.create({"task": "live-ctx", "surface": "app"})
    rt = app.runtime_for(sid)  # installs the web surface incl. ctx checkpoint hook
    traj = rt.agent.run("hello", session_id=sid)
    assert traj.final_answer == "done answer"
    ctx_events = [e for e in list(rt.bus.buf) if e.get("t") == "ctx"]
    assert ctx_events, "checkpoint hook must publish ctx estimates"
    last = ctx_events[-1]
    assert last["prompt"] > 0
    assert last["compact"] == app.base_cfg.compact_above_tokens
    assert "budget" in last
