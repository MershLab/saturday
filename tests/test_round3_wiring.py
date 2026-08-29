"""Round-3 wiring regressions: the R1/R2 backend features must be reachable
from the desktop-app frontend (controls present, save path sends them,
metrics render target exists), and served assets must parse."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

ASSETS = Path(__file__).parent.parent / "src" / "saturday" / "webui_assets"


def _server(app):
    import threading

    from saturday.webui import AppServer

    http = AppServer(("127.0.0.1", 0), app, token="tok")
    base = f"http://127.0.0.1:{http.server_address[1]}"
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return base


def _req(base, path, method="GET", payload=None, token="tok"):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"X-Saturday-Token": token, "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def test_settings_controls_exist_in_index_html():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    for frag_id in ("cfgProvenance", "cfgVerifyCmd", "usageMetrics", "btnExportAll"):
        assert f'id="{frag_id}"' in html, f"missing #{frag_id} in index.html"
    assert 'data-sec="data"' in html and 'data-sec="about"' in html


def test_app_js_wires_the_new_controls():
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    # fill path
    assert 'info.provenance_marking' in js and '$("#cfgProvenance")' in js
    assert 'info.verify_command' in js and '$("#cfgVerifyCmd")' in js
    # save path sends both keys to /api/config
    assert 'provenance_marking: $("#cfgProvenance")' in js
    assert 'verify_command: $("#cfgVerifyCmd")' in js
    # metrics render path consumes the v2 fields
    assert "success_rate" in js and "avg_tokens_per_turn" in js and "stop_reasons" in js


def test_served_assets_match_disk_and_carry_controls(tmp_path):
    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "s")
    base = _server(app)
    status, html = _req(base, "/")
    assert status == 200 and 'id="cfgProvenance"' in html
    status, css = _req(base, "/app.css")
    assert status == 200 and len(css) > 1000
    status, js = _req(base, "/app.js")
    assert status == 200 and "cfgVerifyCmd" in js


def test_metrics_endpoint_served_with_auth(tmp_path):
    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "s")
    base = _server(app)
    status, body = _req(base, "/api/metrics?days=30")
    assert status == 200
    data = json.loads(body)
    assert data["window_days"] == 30
    for key in ("turns", "total_tokens", "success_rate", "stop_reasons", "providers", "days", "models"):
        assert key in data
    # auth enforced
    req = urllib.request.Request(base + "/api/metrics")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("metrics must require token")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_provenance_footer_reaches_webui_done_event(tmp_path, monkeypatch):
    """visible marking -> the streamed done event's final text carries the footer."""

    from saturday.session_runtime import SessionRuntime
    from saturday.types import Trajectory
    from saturday.webui import _run_chat

    class FakeAgent:
        def __init__(self):
            from saturday.config import AgentConfig

            self.cfg = AgentConfig(provider="deepseek", provenance_marking="visible")
            self.session_store = _NoStore()

        def run(self, task, **kw):
            return Trajectory(task=task, system_prompt="s", final_answer="the answer", stop_reason="done")

    class _NoStore:
        def load_checkpoint(self, sid):
            return None

        def save_checkpoint(self, sid, msgs):
            pass

        def append(self, sid, rec):
            pass

        def create(self, meta):
            return "rt-x"

    rt = SessionRuntime("rt-x", FakeAgent())
    rt.try_begin_run()
    _run_chat.__globals__  # touch to fail loudly if symbol moved
    _run_chat(None, rt, "hello", [])
    events = list(rt.bus.buf)
    done = [e for e in events if e.get("t") == "done"]
    assert done and "the answer" in done[0]["final"] and "AI-assisted" in done[0]["final"]

    # metadata mode leaves the answer untouched
    class MetaAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.cfg.provenance_marking = "metadata"

    rt2 = SessionRuntime("rt-y", MetaAgent())
    rt2.try_begin_run()
    _run_chat(None, rt2, "hello", [])
    done2 = [e for e in rt2.bus.buf if e.get("t") == "done"]
    assert done2 and done2[0]["final"] == "the answer"
