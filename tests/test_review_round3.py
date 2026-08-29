"""Regression tests for the session-2 subagent review round findings."""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


def test_approval_ids_are_session_namespaced():
    from saturday.webui import WebApprover

    events_a: list[dict] = []
    events_b: list[dict] = []
    a = WebApprover(events_a.append, ttl=5, scope="sess-a")
    b = WebApprover(events_b.append, ttl=5, scope="sess-b")

    results: dict[str, bool] = {}

    def ask(approver, key):
        results[key] = approver("cmd-x", "reason")

    ta = threading.Thread(target=ask, args=(a, "a"))
    tb = threading.Thread(target=ask, args=(b, "b"))
    ta.start(); tb.start()
    for _ in range(100):
        if events_a and events_b:
            break
        threading.Event().wait(0.02)
    assert events_a and events_b
    aid_a, aid_b = events_a[-1]["id"], events_b[-1]["id"]
    assert aid_a != aid_b, f"ids must be namespaced: {aid_a} vs {aid_b}"
    # resolving b's id must NOT satisfy a's pending ask
    assert a.resolve(aid_b, "allow") is False
    assert b.resolve(aid_b, "allow") is True
    ta.join(timeout=5); tb.join(timeout=5)


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


def _make_app(tmp_path):
    from fakes import make_scripted_model
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    fake = make_scripted_model([{"content": "ok"}] * 4)
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


def test_project_patch_resyncs_runtime_scopes_and_persona(tmp_path):
    app = _make_app(tmp_path)
    proj_ws = tmp_path / "pws"
    proj_ws.mkdir()
    with _Server(app) as srv:
        _, d = _req(srv.base, "/api/projects", "POST", {"name": "P1"})
        pid = d["project"]["id"]
        payload = {"text": "hello project", "project_id": pid}
        r = urllib.request.Request(srv.base + "/api/chat", data=json.dumps(payload).encode(), method="POST")
        r.add_header("X-Saturday-Token", TOKEN)
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=120) as resp:
            sid = json.loads(resp.read().decode().split("\n", 1)[0])["sid"]
        rt = app.runtime_for(sid)
        assert rt.agent.cfg.auth_scopes == {}

        # tighten the project's reserved scopes; the live runtime must follow
        status, d2 = _req(srv.base, "/api/project/" + pid, "PATCH", {"scopes": {"reserved": ["shell"]}})
        assert status == 200
        assert rt.agent.cfg.auth_scopes == {"reserved": ["shell"]}, "stale scopes are security-relevant"

        # instructions change re-merges persona too
        status, d3 = _req(srv.base, "/api/project/" + pid, "PATCH", {"instructions": "always answer in rhyme"})
        assert status == 200
        assert "answer in rhyme" in (rt.agent.persona_extra or "")


def test_hydrate_parses_error_bodies_with_retry_hint(tmp_path):
    from saturday.prompts.templates import render_tool_response
    from saturday.webui import hydrate_session

    app = _make_app(tmp_path)
    sid = app.store.create({"task": "hydrate-err", "surface": "app"})
    body = render_tool_response("shell", False, "explosion happened")
    app.store.append(
        sid,
        {
            "type": "messages",
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "name": "shell", "content": body},
            ],
        },
    )
    data = hydrate_session(app.store, sid)
    items = [it for it in data["items"] if it["kind"] == "assistant"]
    res = items[0]["results"]["c1"]
    assert res["ok"] is False
    assert "explosion happened" in res["body"]
    assert "{" not in res["body"], "raw JSON must not leak into the rendered result"
