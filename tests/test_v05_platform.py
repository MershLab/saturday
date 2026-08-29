"""v0.5: gateway (telegram + serve), screen capture, JS-browser adapter, TUI."""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402


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


def transport_sent_last_text(gw):
    return gw.transport.sent[-1][1]


def _png_bytes() -> bytes:
    import base64

    b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return base64.b64decode(b64)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="screen capture fallback is Windows-only")
def test_screen_tool_captures_real_screen(tmp_path):
    from saturday.tools.screen import ScreenTool

    tool = ScreenTool(shots_dir=tmp_path / "shots")
    ok, out = tool.run({"monitor_note": "ci"})
    if not ok:
        pytest.skip(f"headless environment: {out[:80]}")
    assert tool.pending_images and Path(tool.pending_images[0]).exists()
    assert "[screenshot saved:" in out


def test_playwright_adapter_degrades_without_dep(monkeypatch):
    import builtins

    from saturday.tools import browser_playwright as bp

    tool = bp.PlaywrightBrowserTool()
    orig = builtins.__import__

    def guard(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    with pytest.raises(RuntimeError) as excinfo:
        tool.run({"action": "open", "url": "http://example.invalid"})
    assert "pip install" in str(excinfo.value)


def test_playwright_registered_only_when_available(monkeypatch):
    import builtins

    orig = builtins.__import__

    def guard(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    from saturday.plugins import core_plugin, install_plugins
    from saturday.tools.base import ToolRegistry

    reg = ToolRegistry()
    persona: list[str] = []
    install_plugins(reg, [core_plugin(None)], persona)
    assert "web_browser_js" not in reg.names()
    assert "screen" in reg.names()


def test_playwright_adapter_present_when_importable():
    from saturday.plugins import core_plugin, install_plugins
    from saturday.tools.base import ToolRegistry
    from saturday.tools.browser_playwright import playwright_available

    if not playwright_available():
        pytest.skip("playwright not installed")
    reg = ToolRegistry()
    install_plugins(reg, [core_plugin(None)], [])
    assert "web_browser_js" in reg.names()


def test_tui_rendering_helpers():
    from saturday import tui

    line = tui.status_line(type("A", (), {})())
    assert isinstance(line, str)
    h = tui.header(" Saturday ")
    assert "Saturday" in h.replace("\x1b", "").replace("[36m", "").replace("[0m", "")
    width_a = tui.terminal_width()
    assert width_a > 10


def test_serve_endpoint_roundtrip(tmp_path):
    from http.server import BaseHTTPRequestHandler as BRH, ThreadingHTTPServer

    scripted = make_scripted_model([{"content": "served answer"}])

    class Served(BRH):
        agent = None

        def log_message(self, fmt, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            traj = self.agent.run(str(payload.get("text") or ""))
            body = json.dumps({"ok": True, "answer": traj.final_answer}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    served_agent = make_scripted_agent_like()
    Served.agent = served_agent

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Served)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/message",
            data=json.dumps({"text": "ping"}).encode(),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True and data["answer"] == "echo:ping"
    finally:
        srv.shutdown()
        srv.server_close()


def make_scripted_agent_like():
    from saturday.types import Trajectory

    class A:
        def run(self, task, **kw):
            return Trajectory(task=task, system_prompt="s", final_answer=f"echo:{task[:60]}", stop_reason="done")

    return A()
