"""Parity tests for the shared slash-command registry (saturday.slash) and
the webui route tables: the three command inventories (terminal HELP_TEXT,
the served autocomplete menu, and the dispatch registry itself) must stay in
lockstep now that they drive both surfaces, and unknown routes must 404
uniformly across every verb."""
from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.webui import AppState, AppServer  # noqa: E402

TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


def test_registry_help_and_menu_are_lockstep():
    """HELP_TEXT, the /api/state menu and the registry dispatch exactly the
    same command set — a command added to one surface without the others is
    the drift this extraction exists to prevent."""
    import saturday.slash as slash
    from saturday.repl import HELP_TEXT

    help_cmds = set(re.findall(r"^  (/\w+)", HELP_TEXT, re.M))
    menu_cmds = {name for name, _ in slash.SLASH_COMMAND_LIST}
    registry_cmds = set(slash.COMMANDS)

    assert help_cmds == menu_cmds == registry_cmds, {
        "help_only": sorted(help_cmds - registry_cmds),
        "menu_only": sorted(menu_cmds - registry_cmds),
        "registry_only": sorted(registry_cmds - help_cmds),
    }
    # descriptions on the registry entries are the served menu verbatim
    for sc in slash.COMMANDS.values():
        assert [sc.name, sc.desc] in slash.SLASH_COMMAND_LIST


def test_webui_reexports_shared_registry_objects():
    """webui must re-export the shared objects (identity, not copies) so
    embedders and tests observe registry edits immediately."""
    import saturday.slash as slash
    import saturday.webui as webui

    assert webui.SLASH_COMMAND_LIST is slash.SLASH_COMMAND_LIST
    assert webui.SLASH_ALIASES is slash.SLASH_ALIASES


class _Server:
    def __init__(self, app: AppState):
        self.app = app
        self.http = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.http.shutdown()
        self.http.server_close()


def _make_app(tmp_path: Path) -> AppState:
    app = AppState(
        store_root=tmp_path / "sessions",
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path)},
    )
    fake = make_scripted_model([{"content": "ok"}])
    orig_new = app._new_agent

    def patched(cfg):
        agent = orig_new(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    return app


def _req(base: str, path: str, method: str, payload: dict | None = None):
    data = json.dumps(payload or {}).encode() if method in ("POST", "PATCH") else None
    r = urllib.request.Request(f"{base}{path}", data=data, method=method)
    # r2 review: the URL query is no longer an auth channel — use the header
    r.add_header("X-Saturday-Token", TOKEN)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_unknown_routes_404_across_verbs(tmp_path):
    """Route tables must fall through to the same 404 JSON for unmatched
    paths on every verb (parameterized families too, e.g. /api/session/...
    with a bogus suffix shape is simply not matched)."""
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        for method in ("GET", "POST", "PATCH", "DELETE"):
            status, body = _req(srv.base, "/api/definitely-not-a-route", method)
            assert status == 404, (method, status)
            assert body == {"error": "not found"}, (method, body)
        # unparseable parameterized family member falls through as well
        status, body = _req(srv.base, "/api/session/%2e%2e/escape", "GET")
        assert status == 404
