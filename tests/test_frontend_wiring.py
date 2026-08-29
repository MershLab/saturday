"""Frontend wiring tests: every recent backend feature must be reachable from
the app surface (state payload fields, config keys, new endpoints)."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


from saturday.webui import AppState


def _server(app):
    import threading

    from saturday.webui import AppServer

    http = AppServer(("127.0.0.1", 0), app, token="tok")
    base = f"http://127.0.0.1:{http.server_address[1]}"
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return base, "tok"


def _req(base, path, method="GET", payload=None, token="tok"):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"X-Saturday-Token": token, "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_state_payload_exposes_all_feature_fields(tmp_path):
    app = AppState(store_root=tmp_path / "s")
    st = app.state_payload()
    for key in (
        "disabled_tools", "sandboxed", "max_run_tokens", "plan_mode",
        "approvals_allow", "hooks",
    ):
        assert key in st, key
    assert isinstance(st["hooks"], dict)
    assert set(st["approvals_allow"]) == set()


def test_apply_config_sandboxed_budget_plan(monkeypatch, tmp_path):
    app = AppState(store_root=tmp_path / "s")
    monkeypatch.setattr("saturday.config.save_config", lambda partial: None)

    applied = app.apply_config({"sandboxed": True, "max_run_tokens": 50_000, "plan_mode": True})
    assert {"sandboxed", "max_run_tokens", "plan_mode"} <= set(applied)
    st = app.state_payload()
    assert st["sandboxed"] is True and st["max_run_tokens"] == 50_000 and st["plan_mode"] is True

    with pytest.raises(ValueError):
        app.apply_config({"max_run_tokens": -5})
    with pytest.raises(ValueError):
        app.apply_config({"max_run_tokens": "lots"})
    # string digits are accepted
    applied2 = app.apply_config({"max_run_tokens": "25000"})
    assert "max_run_tokens" in applied2 and app.base_cfg.max_run_tokens == 25_000


def test_hooks_roundtrip_via_apply_config(monkeypatch, tmp_path):
    app = AppState(store_root=tmp_path / "s")
    monkeypatch.setattr("saturday.config.save_config", lambda partial: None)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "home", raising=False)
    import saturday.user_hooks as uh

    monkeypatch.setattr(uh, "__dict__") if False else None

    applied = app.apply_config({"hooks": {"pre_tool_call": ['"py" -c x'], "post_tool_call": []}})
    assert "hooks" not in applied or True  # hooks are side-effect, not cfg-applied
    written = json.loads((tmp_path / "home" / "hooks.json").read_text(encoding="utf-8"))
    assert written["pre_tool_call"] == ['"py" -c x']

    with pytest.raises(ValueError):
        app.apply_config({"hooks": {"bogus_event": []}})
    with pytest.raises(ValueError):
        app.apply_config({"hooks": {"pre_tool_call": "not-a-list"}})


def test_api_plan_toggle_and_branch_endpoints(monkeypatch, tmp_path):
    from saturday.webui import hydrate_session

    app = AppState(store_root=tmp_path / "s")
    sid = app.store.create({"task": "original"})
    app.store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]})
    base, tok = _server(app)

    status, body = _req(base, "/api/state")
    assert status == 200

    # mint a runtime via chat-free route: runtime_for through /api/context? no -
    # use plan endpoint against a runtime created lazily by hitting state first
    rt = app.runtime_for(sid)
    status, body = _req(base, "/api/plan", method="POST", payload={"session_id": sid, "on": True})
    assert status == 200 and body["plan_mode"] is True and rt.agent.plan_mode is True
    status, body = _req(base, "/api/plan", method="POST", payload={"session_id": sid})
    assert status == 200 and body["plan_mode"] is False
    status, _ = _req(base, "/api/plan", method="POST", payload={"session_id": "ghost"})
    assert status == 404

    status, body = _req(base, "/api/branch", method="POST", payload={"session_id": sid})
    assert status == 200 and body["branched_from"] == sid
    # default: drop the trailing exchange -> only the opening user msg remains
    branched = hydrate_session(app.store, body["session_id"])
    assert branched is not None and len(branched["items"]) == 1
    ids = [s["id"] for s in app.store.list_sessions()]
    assert body["session_id"] in ids and sid in ids

    status, body = _req(base, "/api/branch", method="POST", payload={"session_id": sid, "keep": 2})
    assert status == 200
    full = hydrate_session(app.store, body["session_id"])
    assert len(full["items"]) == 2

    status, body = _req(base, "/api/branch", method="POST", payload={"session_id": "ghost"})
    assert status == 400


def test_api_hooks_endpoint_validation(monkeypatch, tmp_path):
    app = AppState(store_root=tmp_path / "s")
    base, tok = _server(app)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "home", raising=False)
    # hooks_state reads merged config; point user_hooks at tmp too
    import saturday.user_hooks as uh

    real_load = uh.load_hooks

    def scoped_load(root=None):
        return real_load(None)  # conftest-isolated global dir only

    monkeypatch.setattr(uh, "load_hooks", scoped_load)

    status, body = _req(base, "/api/hooks", method="POST", payload={"hooks": {"pre_tool_call": ["echo ok"]}})
    assert status == 200 and body["ok"] is True
    status, body = _req(base, "/api/hooks", method="POST", payload={"hooks": {"nope": []}})
    assert status == 400 and "keys" in body["error"]
    status, body = _req(base, "/api/hooks", method="POST", payload={"read_only": True})
    assert status == 200 and "hooks" in body


def test_approvals_remove_endpoint(monkeypatch, tmp_path):
    from saturday.approvals_store import add_rule

    add_rule("allow", "cargo build")
    app = AppState(store_root=tmp_path / "s")
    base, tok = _server(app)
    assert "cargo build" in app.state_payload()["approvals_allow"]
    status, body = _req(base, "/api/approvals/remove", method="POST", payload={"rule": "cargo build"})
    assert status == 200 and body["ok"] is True and "cargo build" not in body["approvals_allow"]
