"""Cross-chat full-text search and the first-run onboarding endpoint."""
from __future__ import annotations

import json
import os
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
    saved: list[dict] = []
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: saved.append(dict(partial)))


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
    fake = make_scripted_model([{"content": "ok"}])
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


def test_search_finds_and_ranks(tmp_path):
    app = _make_app(tmp_path)
    s1 = app.store.create({"task": "kubernetes work", "surface": "app"})
    app.store.append(s1, {"type": "messages", "messages": [
        {"role": "user", "content": "how do kubernetes pods schedule"},
        {"role": "assistant", "content": "kubernetes schedules pods via the scheduler component"},
    ]})
    s2 = app.store.create({"task": "unrelated", "surface": "app"})
    app.store.append(s2, {"type": "messages", "messages": [{"role": "user", "content": "hello world"}]})

    results = __import__("saturday.webui", fromlist=["search_sessions"]).search_sessions(app.store, "kubernetes")
    assert results, "must find matches"
    assert results[0]["sid"] == s1
    assert results[0]["hits"] >= 2
    assert "kubernetes" in results[0]["snippet"].lower()
    assert all(r["sid"] != s2 for r in results)

    assert __import__("saturday.webui", fromlist=["search_sessions"]).search_sessions(app.store, "") == []


def test_search_endpoint(tmp_path):
    app = _make_app(tmp_path)
    s1 = app.store.create({"task": "needle session", "surface": "app"})
    app.store.append(s1, {"type": "messages", "messages": [{"role": "user", "content": "the zanzibar quorum meets at dawn"}]})
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/search?q=zanzibar")
        assert status == 200 and data["query"] == "zanzibar"
        assert data["results"] and data["results"][0]["sid"] == s1
        status, data = _req(srv.base, "/api/search?q=")
        assert status == 200 and data["results"] == []


# ----------------------------------------------------------------- onboard

def test_onboard_writes_env_switches_provider(tmp_path, monkeypatch):

    monkeypatch.setattr(
        "saturday.llm.probe.probe_connection",
        lambda prof, key="", timeout=8.0: (True, "reachable \u2014 2 models found", ["m1", "m2"]),
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openrouter", "api_key": "sk-test-123"})
        assert status == 200
        assert data["provider"] == "openrouter"
        assert data["has_key"] is True
        env_file = tmp_path / ".env"
        content = env_file.read_text(encoding="utf-8")
        assert "OPENROUTER_API_KEY=sk-test-123" in content
        assert os.environ["OPENROUTER_API_KEY"] == "sk-test-123"

    # second run replaces instead of duplicating
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openrouter", "api_key": "sk-two"})
        assert status == 200
        content = (tmp_path / ".env").read_text(encoding="utf-8")
        assert content.count("OPENROUTER_API_KEY") == 1
        assert "sk-two" in content


def test_onboard_validation_errors(tmp_path):
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "not-a-provider", "api_key": "x"})
        assert status == 400
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openai"})
        assert status == 400, "missing key refused"
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openai", "api_key": ""})
        assert status == 400


def test_onboard_model_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "saturday.llm.probe.probe_connection",
        lambda prof, key="", timeout=8.0: (True, "reachable \u2014 2 models found", ["m1", "m2"]),
    )
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "deepseek", "api_key": "k", "model": "deepseek-chat"})
        assert status == 200
        assert data["model"] == "deepseek-chat"
