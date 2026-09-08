"""Discord and Slack gateways: transport-level polling correctness (bootstrap
doesn't replay history, bot/self messages are filtered) and the shared
dispatch loop end to end, all against fake transports/urlopen - no real bot
token needed. Telegram's own gateway tests live in test_scheduling.py; this
file only covers what's new."""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


class FakeTransport:
    """Same shape TelegramGateway's tests use: pre-normalized {"chat_id",
    "text"} updates, since DiscordGateway/SlackGateway parse that shape by
    default (their real transports normalize on the way out)."""

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


def _wait_for(cond, message, timeout=5.0):
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if cond():
            return
        _t.sleep(0.01)
    raise AssertionError(message)


@pytest.mark.parametrize("gw_cls_name", ["DiscordGateway", "SlackGateway"])
def test_platform_gateway_end_to_end(gw_cls_name):
    from saturday import gateway as gwmod

    gw_cls = getattr(gwmod, gw_cls_name)
    updates = [
        {"chat_id": "c1", "text": "hello bot"},
        {"chat_id": "c2", "text": "intruder"},
    ]
    transport = FakeTransport(updates)
    gw = gw_cls("tok", scripted_agent_factory, channel_ids=["c1"], transport=transport)

    handled = gw.poll_once()
    assert handled == 1
    _wait_for(lambda: any(t[0] == "c1" for t in transport.sent), "no reply to the allowed channel")
    _wait_for(lambda: any(t[0] == "c2" for t in transport.sent), "no liveness reply to the stranger")
    sent = [t for t in transport.sent if t[0] == "c1"]
    blocked = [t for t in transport.sent if t[0] == "c2"]
    assert sent and sent[0][1] == "echo:hello bot"
    assert blocked and blocked[0][1] == "Not authorized for this bot."


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_discord_transport_bootstrap_does_not_replay_history(monkeypatch):
    from saturday.gateway import DiscordTransport

    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/messages?limit=1"):
            return _Resp(json.dumps([{"id": "100"}]).encode())
        raise AssertionError(f"unexpected call: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    t = DiscordTransport("tok", ["c1"])
    updates = t.get_updates()

    assert updates == []
    assert t._after["c1"] == "100"
    assert any(u.endswith("/messages?limit=1") for u in calls)


def test_discord_transport_filters_bot_and_self_then_returns_new(monkeypatch):
    from saturday.gateway import DiscordTransport

    t = DiscordTransport("tok", ["c1"])
    t._after["c1"] = "100"  # skip bootstrap

    def fake_urlopen(req, timeout=None):
        if req.full_url.endswith("/users/@me"):
            return _Resp(json.dumps({"id": "BOT1"}).encode())
        if "after=100" in req.full_url:
            return _Resp(
                json.dumps(
                    [
                        {"id": "103", "content": "hello", "author": {"id": "U1", "bot": False}},
                        {"id": "101", "content": "other bot", "author": {"id": "U2", "bot": True}},
                        {"id": "102", "content": "self echo", "author": {"id": "BOT1", "bot": True}},
                    ]
                ).encode()
            )
        raise AssertionError(f"unexpected call: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    updates = t.get_updates()

    assert updates == [{"chat_id": "c1", "text": "hello"}]
    assert t._after["c1"] == "103"  # cursor advances past the filtered messages too


def test_discord_transport_send_message_chunks_long_text(monkeypatch):
    from saturday.gateway import MAX_MESSAGE, DiscordTransport

    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode())["content"])
        return _Resp(b"")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    t = DiscordTransport("tok", ["c1"])
    t.send_message("c1", "x" * (MAX_MESSAGE + 10))

    assert len(sent) == 2
    assert len(sent[0]) == MAX_MESSAGE
    assert len(sent[1]) == 10


def test_slack_transport_bootstrap_does_not_replay_history(monkeypatch):
    from saturday.gateway import SlackTransport

    def fake_urlopen(req, timeout=None):
        raise AssertionError("bootstrap must not call the API at all")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    t = SlackTransport("tok", ["c1"])
    updates = t.get_updates()

    assert updates == []
    assert "c1" in t._oldest


def test_slack_transport_filters_bot_and_self_and_subtypes(monkeypatch):
    from saturday.gateway import SlackTransport

    t = SlackTransport("tok", ["c1"])
    t._oldest["c1"] = "100.0"  # skip bootstrap

    def fake_urlopen(req, timeout=None):
        if "auth.test" in req.full_url:
            return _Resp(json.dumps({"ok": True, "user_id": "BOTU1"}).encode())
        if "conversations.history" in req.full_url:
            return _Resp(
                json.dumps(
                    {
                        "ok": True,
                        "messages": [
                            {"ts": "103.0", "text": "hello", "user": "U1"},
                            {"ts": "101.0", "text": "other bot", "bot_id": "B1", "user": "U2"},
                            {"ts": "102.0", "text": "self echo", "user": "BOTU1"},
                            {"ts": "102.5", "text": "joined", "user": "U1", "subtype": "channel_join"},
                        ],
                    }
                ).encode()
            )
        raise AssertionError(f"unexpected call: {req.full_url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    updates = t.get_updates()

    assert updates == [{"chat_id": "c1", "text": "hello"}]
    assert t._oldest["c1"] == "103.0"


def test_slack_transport_raises_on_api_error(monkeypatch):
    from saturday.gateway import SlackTransport

    def fake_urlopen(req, timeout=None):
        return _Resp(json.dumps({"ok": False, "error": "channel_not_found"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    t = SlackTransport("tok", ["c1"])
    t._oldest["c1"] = "100.0"

    with pytest.raises(RuntimeError, match="channel_not_found"):
        t.get_updates()
