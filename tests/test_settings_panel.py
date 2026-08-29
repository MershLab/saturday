"""Tests for the proper settings panel: new config knobs (background-only,
fallback models, max tokens), data endpoints (reveal/export-all/clear-all),
and client-rebuild semantics when request-affecting config changes."""
from __future__ import annotations

import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import saturday.webui as webui  # noqa: E402
from saturday.agent.core import Agent  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.projects import ProjectStore  # noqa: E402
from saturday.webui import AppState, AppServer  # noqa: E402

TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    saved: list[dict] = []

    def fake_save(partial):
        saved.append(dict(partial))

    monkeypatch.setattr(cfgmod, "save_config", fake_save)
    return saved


class _Server:
    def __init__(self, app: AppState):
        self.http = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.http.shutdown()
        self.http.server_close()


def make_app(tmp_path: Path, turns=None) -> AppState:
    from fakes import make_scripted_model

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


def req(base: str, path: str, method: str = "GET", payload: dict | None = None):
    import json

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


def test_state_payload_has_settings_fields(tmp_path: Path):
    app = make_app(tmp_path)
    with _Server(app) as srv:
        status, data = req(srv.base, "/api/state")
        assert status == 200
        for key in ("max_tokens", "fallback_models", "background_only", "config_dir", "sessions_dir", "workspace_root"):
            assert key in data, key
        assert Path(data["sessions_dir"]) == app.store.root


def test_background_only_roundtrip_and_persistence(tmp_path: Path):
    app = make_app(tmp_path)
    with _Server(app) as srv:
        status, data = req(srv.base, "/api/config", "POST", {"desktop_background_only": True})
        assert status == 200 and data["background_only"] is True
        assert app.base_cfg.desktop_background_only is True
        status, data = req(srv.base, "/api/config", "POST", {"desktop_background_only": False})
        assert status == 200 and data["background_only"] is False

        # project runtime clone receives the flag too
        _, d = req(srv.base, "/api/projects", "POST", {"name": "Bg"})
        pid = d["project"]["id"]
        import json as j

        payload = j.dumps({"text": "hi", "project_id": pid}).encode()
        r = urllib.request.Request(srv.base + "/api/chat", data=payload, method="POST")
        r.add_header("X-Saturday-Token", TOKEN)
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=60) as resp:
            sid = j.loads(resp.readline().decode())["sid"]
        req(srv.base, "/api/config", "POST", {"desktop_background_only": True})
        assert app.runtime_for(sid).agent.cfg.desktop_background_only is True


def test_fallback_models_forms_and_validation(tmp_path: Path):
    app = make_app(tmp_path)
    with _Server(app) as srv:
        status, data = req(srv.base, "/api/config", "POST", {"fallback_models": ["a", "b"]})
        assert status == 200 and data["fallback_models"] == ["a", "b"]

        status, data = req(srv.base, "/api/config", "POST", {"fallback_models": " x , ,y, x,"})
        assert status == 200 and data["fallback_models"] == ["x", "y"], "string form parsed + deduped"

        status, data = req(srv.base, "/api/config", "POST", {"fallback_models": "m1,m2,m3,m4,m5,m6,m7,m8,m9"})
        assert status == 200 and len(data["fallback_models"]) == 8, "capped at 8"

        status, _ = req(srv.base, "/api/config", "POST", {"fallback_models": 42})
        assert status == 400


def test_max_tokens_roundtrip_and_bounds(tmp_path: Path):
    app = make_app(tmp_path)
    with _Server(app) as srv:
        status, data = req(srv.base, "/api/config", "POST", {"max_tokens": 16384})
        assert status == 200 and data["max_tokens"] == 16384

        status, data = req(srv.base, "/api/config", "POST", {"max_tokens": 999999})
        assert status == 200 and data["max_tokens"] == 16384, "out-of-range ignored, previous kept"


def test_client_rebuilds_when_fallback_or_tokens_change():
    agent = Agent(cfg=AgentConfig(provider="openai", model="m1"))
    c1 = agent._ensure_client()
    agent.cfg.fallback_models = ["m2"]
    c2 = agent._ensure_client()
    assert c1 is not c2 and c2.fallback_models == ["m2"]
    c3 = agent._ensure_client()
    assert c3 is c2, "same signature must reuse the client"
    agent.cfg.max_tokens = 4096
    c4 = agent._ensure_client()
    assert c4 is not c2


def test_reveal_targets_and_validation(tmp_path: Path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webui, "_reveal_path", lambda p: opened.append(p))
    app = make_app(tmp_path)
    with _Server(app) as srv:
        for target, expected in (("config", None), ("sessions", str(app.store.root)), ("workspace", str(tmp_path / "ws"))):
            status, data = req(srv.base, "/api/reveal", "POST", {"target": target})
            assert status == 200 and data["ok"] is True
        assert len(opened) == 3
        assert Path(opened[1]) == app.store.root
        status, _ = req(srv.base, "/api/reveal", "POST", {"target": "C:/Windows"})
        assert status == 400, "arbitrary paths must be refused"


def test_clear_all_sessions_endpoint(tmp_path: Path):
    app = make_app(tmp_path)
    with _Server(app) as srv:
        sids = [app.store.create({"task": f"s{i}", "surface": "app"}) for i in range(2)]
        for sid in sids:
            app.store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "hi"}]})
        assert len(app.store.list_sessions()) == 2
        app.runtime_for(sids[0])

        status, data = req(srv.base, "/api/sessions/all", "DELETE")
        assert status == 200 and data["removed"] == 2
        assert app.store.list_sessions() == []
        assert app.runtimes == {}
        for sid in sids:
            assert not app.store._path(sid).exists()
            assert not app.store._path(sid).with_suffix(".checkpoint.json").exists()


def test_export_all_returns_full_records(tmp_path: Path):
    app = make_app(tmp_path)
    with _Server(app) as srv:
        sid = app.store.create({"task": "exportable", "surface": "app"})
        app.store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "hello export"}]})
        status, data = req(srv.base, "/api/export/all")
        assert status == 200 and data["exported"] == 1
        sess = data["sessions"][0]
        assert sess["meta"]["task"] == "exportable"
        msgs = [r for r in sess["records"] if r.get("type") == "messages"]
        assert msgs, "message records must be present in the export"
