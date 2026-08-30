"""Cron parity: 5-field matcher, ScheduleStore persistence, due/mark logic.
Also: Telegram gateway (session reuse, backoff), local usage accounting."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

from saturday.schedule import ScheduleStore, _valid_expr, cron_matches

TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


def test_cron_matches_basic_fields():
    dt = datetime(2026, 8, 27, 9, 30)
    assert cron_matches("30 9 * * *", dt) is True
    assert cron_matches("31 9 * * *", dt) is False
    assert cron_matches("30 8 * * *", dt) is False
    assert cron_matches("*/15 9 * * *", dt) is True  # minute 30 is a /15 match
    assert cron_matches("*/20 9 * * *", dt) is False  # 30 is not a /20 match
    assert cron_matches("0,30 9 * * *", dt) is True
    assert cron_matches("* * * * 4", dt) is True  # 2026-08-27 is a Thursday (isoweekday 4)
    assert cron_matches("* * * * 3", dt) is False


def test_cron_dom_dow_or_semantics():
    dt = datetime(2026, 8, 27, 9, 0)  # Thu, 27th
    # both restricted: EITHER match satisfies (standard cron contract)
    assert cron_matches("0 9 27 * 0", dt) is True  # dom matches, dow doesn't
    assert cron_matches("0 9 28 * 4", dt) is True  # dow matches, dom doesn't
    assert cron_matches("0 9 28 * 0", dt) is False
    # one side *: AND semantics
    assert cron_matches("0 9 * * 4", dt) is True
    assert cron_matches("0 9 27 * *", dt) is True


def test_invalid_expressions_rejected():
    assert _valid_expr("0 9 * * *") is True
    assert _valid_expr("60 9 * * *") is False
    assert _valid_expr("0 24 * * *") is False
    assert _valid_expr("0 9 * * 8") is False
    assert _valid_expr("0 9 * *") is False
    assert _valid_expr("junk") is False


def test_store_add_list_remove_and_due(tmp_path):
    store = ScheduleStore(tmp_path / "sched.json")
    s = store.add("morning", "0 9 * * 1-5", "standup notes")
    assert s.id == "morning" and store.list()[0].task == "standup notes"
    with pytest.raises(ValueError):
        store.add("bad", "99 9 * * *", "nope")

    dt = datetime(2026, 8, 27, 9, 0)  # Thu
    due = store.due(now=dt)
    assert [d.id for d in due] == ["morning"]
    store.mark_fired("morning", now=dt)
    assert store.due(now=dt) == [], "must not re-fire the same minute"
    assert [d.id for d in store.due(now=datetime(2026, 8, 28, 9, 0))] == ["morning"], "next weekday fires again"
    # 10:00 same morning: not due
    assert store.due(now=datetime(2026, 8, 27, 10, 0)) == []

    store2 = ScheduleStore(tmp_path / "sched.json")  # persistence round-trip
    assert store2.list()[0].last_fired_minute == "202608270900"
    assert store.remove("morning") is True
    assert store.remove("morning") is False


def test_cron_dow_7_matches_sunday():
    from saturday.schedule import _valid_expr, cron_matches

    sunday = datetime(2026, 8, 30, 9, 0)
    monday = datetime(2026, 8, 31, 9, 0)
    assert _valid_expr("0 9 * * 7")
    assert cron_matches("* * * * 7", sunday), "dow=7 must match Sunday"
    assert not cron_matches("* * * * 7", monday), "dow=7 must not match Monday"
    assert cron_matches("* * * * 0", sunday), "dow=0 still matches Sunday"
    assert cron_matches("* * * * 0,7", sunday), "combined 0,7 matches Sunday once"


def test_cron_dow_7_end_to_end(tmp_path):
    from saturday.schedule import ScheduleStore

    store = ScheduleStore(path=tmp_path / "schedules.json")
    store.add("sun", "0 9 * * 7", "weekly sunday task")
    due = store.due(now=datetime(2026, 8, 30, 9, 0))
    assert [s.id for s in due] == ["sun"]


# --- helpers pulled from tests/test_v05_platform.py ---
class FakeTransport:
    def __init__(self, updates: list[dict]):
        self.updates = list(updates)
        self.sent: list[tuple] = []

    def get_updates(self):
        out = self.updates
        self.updates = []
        return out

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def scripted_agent_factory():
    from saturday.types import Trajectory

    class A:
        memory = None
        cfg = None

        def run(self, task, **kw):
            return Trajectory(task=task, system_prompt="s", final_answer=f"echo:{task[:60]}", stop_reason="done")

    return A()


def transport_sent_last_text(gw):
    return gw.transport.sent[-1][1]


def test_telegram_gateway_end_to_end():
    from saturday.gateway import TelegramGateway

    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "hello bot"}},
        {"update_id": 2, "message": {"chat": {"id": 43}, "text": "intruder"}},
    ]
    transport = FakeTransport(updates)
    gw = TelegramGateway("tok", scripted_agent_factory, allowed_chat_ids={42}, transport=transport)

    handled = gw.poll_once()
    assert handled == 1
    sent = [t for t in transport.sent if t[0] == 42]
    blocked = [t for t in transport.sent if t[0] == 43]
    assert sent and sent[0][1] == "echo:hello bot"
    # r2: one liveness reply per stranger chat, then silent drop (probe oracle)
    assert blocked and blocked[0][1] == "Not authorized for this bot."

    transport.updates = [
        {"update_id": 3, "message": {"chat": {"id": 43}, "text": "intruder again"}},
    ]
    handled2 = gw.poll_once()
    assert handled2 == 0
    assert len([t for t in transport.sent if t[0] == 43]) == 1, "must not reply to repeat probes"


def test_gateway_session_reuse_and_error_path():
    from saturday.gateway import TelegramGateway

    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return scripted_agent_factory()

    class Boom:
        def run(self, task, **kw):
            raise RuntimeError("model exploded")

    transport = FakeTransport([{"update_id": 5, "message": {"chat": {"id": 7}, "text": "a"}}])
    gw = TelegramGateway("t", factory, transport=transport)
    s1 = gw.session_for(7)
    s2 = gw.session_for(7)
    assert s1 is s2

    boom_gw = TelegramGateway(
        "t",
        lambda: Boom(),
        transport=FakeTransport([{"update_id": 6, "message": {"chat": {"id": 8}, "text": "x"}}]),
    )
    boom_gw.poll_once()
    assert "agent error" in transport_sent_last_text(boom_gw)


def test_gateway_backoff_on_transport_failure():
    from saturday.gateway import TelegramGateway

    class FlakyTransport:
        def __init__(self):
            self.calls = 0

        def get_updates(self):
            self.calls += 1
            if self.calls <= 2:
                raise ConnectionError("telegram down")
            return []

        def send_message(self, chat_id, text):
            pass

    sleeps = []
    gw = TelegramGateway("t", lambda: None, transport=FlakyTransport())
    gw._tick(sleeps.append)
    gw._tick(sleeps.append)
    ok = gw._tick(sleeps.append)

    assert sleeps[:2] == [2.0, 4.0], f"backoff not exponential: {sleeps}"
    assert ok is True
    assert gw.consecutive_failures == 0


# --- helpers pulled from tests/test_usage.py ---
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
