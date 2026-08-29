"""Settings-menu parity audit: every control round-trips, nothing silently
drops, no hidden backend settings."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from saturday.webui import AppServer, AppState


def _make_server():
    app = AppState(cfg_overrides={"workspace_root": str(Path.cwd())})
    srv = AppServer(("127.0.0.1", 0), app, token="")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def _post(base, payload):
    req = urllib.request.Request(
        base + "/api/config",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_state_exposes_full_tool_universe_for_toggle_ui():
    """The settings checklist can only offer tools the state payload names;
    repo_search/memory/skills were previously invisible here."""
    srv, base = _make_server()
    try:
        with urllib.request.urlopen(base + "/api/state", timeout=15) as r:
            info = json.loads(r.read().decode())
        names = set(info["tool_names"])
        assert {"repo_search", "memory", "skill_save", "skills_index"} <= names
        assert info["keep_reasoning_in_history"] is False
        assert isinstance(info["lsp_servers"], dict)
    finally:
        # shutdown() first: closing the listening socket under a live
        # serve_forever thread raises WinError 10038 on Windows
        srv.shutdown()
        srv.server_close()


def test_keep_reasoning_and_lsp_roundtrip():
    srv, base = _make_server()
    try:
        status, out = _post(base, {"keep_reasoning_in_history": True,
                                   "lsp_servers": {"python": ["pylsp"]}})
        assert status == 200
        assert "keep_reasoning_in_history" in out["applied"]
        assert "lsp_servers" in out["applied"]
        assert out["keep_reasoning_in_history"] is True
        assert out["lsp_servers"] == {"python": ["pylsp"]}
        # invalid shape -> explicit 400, never a silent skip
        status, body = _post(base, {"lsp_servers": {"python": "pylsp"}})
        assert status == 400
        status, body = _post(base, {"lsp_servers": []})
        assert status == 400
    finally:
        # shutdown() first: closing the listening socket under a live
        # serve_forever thread raises WinError 10038 on Windows
        srv.shutdown()
        srv.server_close()


def test_frontend_has_no_stale_settings_patterns():
    root = Path(__file__).parents[1] / "src" / "saturday" / "webui_assets"
    js = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")

    # toggle map covers the previously hidden groups + dynamic other-tools list
    for needle in ("cfgToolMemory", "cfgToolSkills", "cfgToolRepoSearch",
                   "info.tool_names", 'id="cfgToolOther"', "data-tool"):
        assert needle in js or needle in html, needle
    # silent-rejection guard: saving must surface what the backend skipped
    assert "not applied:" in js
    # every JS-referenced cfg* element exists in the HTML (no dead controls)
    import re

    referenced = set(re.findall(r'\$\("#(cfg[A-Za-z]+)"\)', js))
    defined = set(re.findall(r'id="(cfg[A-Za-z]+)"', html))
    missing = {r_ for r_ in referenced if r_ not in defined}
    assert not missing, f"JS references settings elements that don't exist: {sorted(missing)}"
