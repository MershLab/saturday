"""Local usage accounting: record/aggregate round-trip and state exposure."""
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


def test_record_and_summary(tmp_path):
    from saturday.usage import load_entries, record_usage, usage_summary

    assert load_entries() == []
    record_usage(provider="openai", model="m1", session="s", steps=3, total_tokens=100, stop_reason="done")
    record_usage(provider="openrouter", model="m2", session="s2", steps=1, total_tokens=50, stop_reason="done")
    entries = load_entries()
    assert len(entries) == 2 and entries[0]["model"] == "m1"
    summary = usage_summary()
    assert summary["turns"] == 2
    assert summary["total_tokens"] == 150
    models = {m["model"]: m["tokens"] for m in summary["models"]}
    assert models["openai/m1"] == 100
    assert len(summary["days"]) == 1


def test_old_entries_ignored_not_deleted(tmp_path):
    from saturday.usage import DAYS_SHOWN, load_entries, record_usage, usage_summary

    record_usage(provider="p", model="m", total_tokens=10)
    # backdate a line beyond the window by rewriting the file
    p = tmp_path / "usage.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    import time as t

    old = json.loads(lines[0])
    old["ts"] = t.time() - (DAYS_SHOWN + 5) * 86_400
    old["day"] = "2000-01-01"
    p.write_text(json.dumps(old) + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    entries = load_entries()
    assert all(e["day"] != "2000-01-01" for e in entries)
    assert usage_summary()["turns"] == len(entries)


def test_corrupt_lines_skipped(tmp_path):
    from saturday.usage import load_entries

    p = tmp_path / "usage.jsonl"
    p.write_text("{not json}\n\n{\"ts\": 1}\n", encoding="utf-8")  # ts=1 -> ancient, dropped
    assert load_entries() == []


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


def test_chat_turn_records_usage_and_state_exposes(tmp_path):
    from fakes import make_scripted_model
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    fake = make_scripted_model([{"content": "answer!"}])
    orig = app._new_agent

    def patched(cfg):
        agent = orig(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    with _Server(app) as srv:
        payload = {"text": "hi", "session_id": ""}
        r = urllib.request.Request(srv.base + "/api/chat", data=json.dumps(payload).encode(), method="POST")
        r.add_header("X-Saturday-Token", TOKEN)
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=120) as resp:
            resp.read()
        status, state = _req(srv.base, "/api/state")
        assert status == 200
        assert state["usage"]["turns"] >= 1
        assert any(m["model"].endswith(app.base_cfg.model or "?") for m in state["usage"]["models"])
