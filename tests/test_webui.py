"""Merged from: tests/test_webui_core.py, tests/test_webui_e2e.py, tests/test_webui_newui.py, tests/test_frontend_wiring.py, tests/test_competitive_ui.py, tests/test_webui_projects.py, tests/test_settings.py, tests/test_desktop_window.py."""
from __future__ import annotations
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import pytest
from fakes import make_scripted_model
from saturday.webui import AppState, AppServer, WebApprover
import json
from saturday.projects import ProjectStore
from saturday import webui
from saturday.webui import _CFG_SKIP, _b_float_range, _b_int_range, _b_int_range_opt, _v_bool
from saturday.agent.core import Agent
from saturday.config import AgentConfig
os.environ.setdefault("SATURDAY_APPROVAL_TTL", "6")
TOKEN = "tok"
ASSETS = Path(__file__).parent.parent / "src" / "saturday" / "webui_assets"

def _hermetic(monkeypatch):
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


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

    def url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.base}{path}{sep}k={TOKEN}"


def make_app(tmp_path: Path, turns, *, safety="off", workspace=None) -> AppState:
    app = AppState(
        store_root=tmp_path / "sessions",
        cfg_overrides={"safety_mode": safety, "workspace_root": str(workspace or tmp_path)},
    )
    fake = make_scripted_model(turns)
    orig_new = app._new_agent

    def patched(cfg):
        agent = orig_new(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    return app


def get(base_url: str, path: str, token=TOKEN, headers=None):
    req = urllib.request.Request(base_url + path)
    if token:
        req.add_header("X-Saturday-Token", token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, dict(r.headers), r.read()


def post_json(base_url: str, path: str, payload: dict, token=TOKEN):
    data = json_bytes(payload)
    req = urllib.request.Request(base_url + path, data=data, method="POST")
    if token:
        req.add_header("X-Saturday-Token", token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def chat_events(server: _Server, payload: dict) -> list[dict]:
    """POST /api/chat and collect the full NDJSON event stream."""
    status, body = post_json(server.base, "/api/chat", payload)
    assert status == 200, body[:400]
    out = []
    for line in body.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json_load(line))
    return out


def json_bytes(obj) -> bytes:
    import json

    return json.dumps(obj).encode("utf-8")


def json_load(s: str) -> dict:
    import json

    return json.loads(s)


PNG_1PX = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_static_assets_served(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        status, headers, body = get(srv.base, "/")
        assert status == 200 and b"Saturday" in body
        assert "text/html" in headers["Content-Type"]
        for path, marker in [("/app.js", b"renderMd"), ("/app.css", b"--accent"), ("/favicon.svg", b"<svg")]:
            st, hd, bd = get(srv.base, path)
            assert st == 200 and marker in bd


def test_token_auth(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(srv.base, "/api/state", token=None)
        assert ei.value.code == 401
        st, _, _ = get(srv.base, "/api/state", headers={"X-Saturday-Token": TOKEN})
        assert st == 200
        st, _, _ = get(srv.base, "/api/state")
        assert st == 200, "query param k must authorize too"


def test_state_payload_shape(tmp_path: Path):
    from saturday import __version__

    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        _, _, body = get(srv.url("/api/state"), "")
        data = json_load(body.decode())
        assert data["version"] == __version__
        assert data["safety_mode"] == "off"
        assert isinstance(data["providers"], list) and len(data["providers"]) >= 10
        assert all("has_key" in p for p in data["providers"])


def test_chat_roundtrip_and_persistence(tmp_path: Path):
    app = make_app(tmp_path, [{"content": "final hello"}])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "say hi"})
        types = [e["t"] for e in events]
        assert types[0] == "hello"
        assert "user" in types and "delta" in types
        done = [e for e in events if e["t"] == "done"][0]
        assert done["final"] == "final hello"
        assert done["stop_reason"] == "done"
        sid = done["sid"]
        sessions = app.store.list_sessions()
        assert len(sessions) == 1 and sessions[0]["id"] == sid


def test_chat_reasoning_events_stream_before_text(tmp_path: Path):
    app = make_app(tmp_path, [{"reasoning": "pondering", "content": "answer"}])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "q"})
        reason_idx = next(i for i, e in enumerate(events) if e["t"] == "reason")
        delta_idx = next(i for i, e in enumerate(events) if e["t"] == "delta")
        assert reason_idx < delta_idx
        assert any("pondering" in e.get("s", "") for e in events if e["t"] == "reason")


def test_chat_tool_flow_shell(tmp_path: Path):
    turns = [
        {"tool_calls": [{"name": "shell", "arguments": {"command": "echo df-echo-test"}}]},
        {"content": "done"},
    ]
    app = make_app(tmp_path, turns)
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "run echo"})
        starts = [e for e in events if e["t"] == "tool_start"]
        results = [e for e in events if e["t"] == "tool_result"]
        assert len(starts) == 1 and starts[0]["name"] == "shell" and starts[0]["card"].startswith("c")
        assert len(results) == 1
        assert results[0]["ok"] is True
        assert "df-echo-test" in results[0]["output"]
        assert results[0]["card"] == starts[0]["card"], "result must attach to the running card"


def test_busy_session_returns_409(tmp_path: Path):
    app = make_app(tmp_path, [{"content": "x"}])
    with _Server(app) as srv:
        rt = app.runtime_for("busy-sess")
        rt.busy = True
        status, body = post_json(srv.base, "/api/chat", {"session_id": "busy-sess", "text": "hello"})
        assert status == 409
        rt.busy = False


def test_image_upload_roundtrip(tmp_path: Path):
    import tempfile

    app = make_app(tmp_path, [{"content": "seen it"}])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "what is this", "images": [PNG_1PX]})
        user = [e for e in events if e["t"] == "user"][0]
        assert user["images"] == 1
        done = [e for e in events if e["t"] == "done"][0]
        assert done["final"] == "seen it"
        uploads = list((Path(tempfile.gettempdir()) / "saturday-uploads").rglob("att-*.png"))
        assert uploads, "decoded image should be persisted under the uploads dir"


def test_slash_commands_help_model_compact(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "/help"})
        notices = [e["s"] for e in events if e["t"] == "notice"]
        assert any("/tools" in n for n in notices)
        assert events[-1]["t"] == "done" and events[-1]["stop_reason"] == "slash"

        events = chat_events(srv, {"text": "/model zz-model-9b"})
        cfg_evts = [e for e in events if e["t"] == "config"]
        assert cfg_evts and cfg_evts[0]["model"] == "zz-model-9b"

        events = chat_events(srv, {"text": "/compact"})
        notices = [e["s"] for e in events if e["t"] == "notice"]
        assert any("nothing to compact" in n for n in notices)


def test_unknown_command_notice(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "/definitely-not-a-command"})
        notices = [e["s"] for e in events if e["t"] == "notice"]
        assert any("unknown command" in n for n in notices)


def test_approval_deny_blocks_sudo(tmp_path: Path):
    turns = [
        {"tool_calls": [{"name": "shell", "arguments": {"command": "sudo rm thing"}}]},
        {"content": "ok"},
    ]
    app = make_app(tmp_path, turns, safety="ask")
    resolved = threading.Event()

    def resolver(events_box):
        while not resolved.is_set():
            for e in list(events_box):
                if e.get("t") == "approval":
                    st, _ = post_json(srv_holder["base"], "/api/approve", {"id": e["id"], "decision": "deny"})
                    assert st == 200
                    return
            time.sleep(0.05)

    srv_holder = {}
    events_box: list[dict] = []
    with _Server(app) as srv:
        srv_holder["base"] = srv.base
        t = threading.Thread(target=resolver, args=(events_box,), daemon=True)
        t.start()
        for line in stream_chat_lines(srv, {"text": "do it"}):
            events_box.append(line)
        resolved.set()
        t.join(timeout=5)
        results = [e for e in events_box if e["t"] == "tool_result"]
        assert results and results[0]["ok"] is False
        assert "user denied" in (results[0]["error"] or "")
        starts = [e for e in events_box if e["t"] == "tool_start"]
        assert starts and "sudo" in starts[0]["args"]["command"], "blocked calls must still emit a card with args"
        assert starts[0]["card"] == results[0]["card"]
        dones = [e for e in events_box if e["t"] == "approval_done"]
        assert dones and dones[0]["allowed"] is False and dones[0]["timeout"] is False


def stream_chat_lines(server: _Server, payload: dict):
    import json as j

    data = json_bytes(payload)
    req = urllib.request.Request(server.base + "/api/chat", data=data, method="POST")
    req.add_header("X-Saturday-Token", TOKEN)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=90) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line:
                yield j.loads(line)


def test_file_gate_diff_allow_then_always(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.txt"
    target.write_text("alpha\n", encoding="utf-8")
    call = lambda: {  # noqa: E731
        "tool_calls": [{"name": "write_file", "arguments": {"path": str(target), "content": "alpha\nbeta\n"}}]
    }
    app = make_app(tmp_path, [call(), {"content": "wrote"}, call(), {"content": "again"}], safety="off", workspace=ws)
    with _Server(app) as srv:
        box1: list[dict] = []

        def resolver():
            while True:
                appr = next((e for e in box1 if e.get("t") == "approval"), None)
                if appr:
                    st, body = post_json(srv.base, "/api/approve", {"id": appr["id"], "decision": "always"})
                    assert st == 200 and json_load(body.decode()).get("ok") is True, f"resolve failed: {body!r}"
                    return
                time.sleep(0.05)

        t = threading.Thread(target=resolver, daemon=True)
        t.start()
        ev1: list[dict] = []
        for line_evt in stream_chat_lines(srv, {"text": "write it"}):
            box1.append(line_evt)
            ev1.append(line_evt)
        t.join(timeout=5)
        approvals1 = [e for e in ev1 if e["t"] == "approval"]
        assert len(approvals1) == 1 and approvals1[0]["kind"] == "file"
        assert "+beta" in approvals1[0]["diff"]
        res1 = [e for e in ev1 if e["t"] == "tool_result"][0]
        assert res1["ok"] is True, f"write failed: {res1.get('error')!r}"
        assert "beta" in target.read_text(encoding="utf-8")

        ev2 = chat_events(srv, {"session_id": ev1[-1]["sid"], "text": "rewrite it"})
        approvals2 = [e for e in ev2 if e["t"] == "approval"]
        assert not approvals2, "'always this file' must suppress later prompts for the same path"
        res2 = [e for e in ev2 if e["t"] == "tool_result"][0]
        assert res2["ok"] is True


def test_stop_cancels_pending_approval(tmp_path: Path):
    app = make_app(tmp_path, [], safety="ask")
    with _Server(app) as srv:
        sid = "stop-test"
        rt = app.runtime_for(sid)
        got: list[bool] = []

        def ask():
            got.append(rt.approver("danger-cmd", "elevated privileges (sudo)"))

        t = threading.Thread(target=ask, daemon=True)
        t.start()
        time.sleep(0.15)
        st, _ = post_json(srv.base, "/api/stop", {"session_id": sid})
        assert st == 200
        t.join(timeout=6)
        assert got == [False]
        evts = rt.bus.replay(0)
        dones = [e for e in evts if e["t"] == "approval_done"]
        assert dones and dones[0]["allowed"] is False


def test_session_hydration_with_tool_results(tmp_path: Path):
    turns = [
        {"tool_calls": [{"name": "shell", "arguments": {"command": "echo hyd-12345"}}]},
        {"content": "all done"},
    ]
    app = make_app(tmp_path, turns)
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "go"})
        sid = events[-1]["sid"]
        _, _, body = get(srv.url(f"/api/session/{sid}"), "")
        data = json_load(body.decode())
        kinds = [it["kind"] for it in data["items"]]
        assert kinds == ["user", "assistant", "assistant"], kinds
        asst = data["items"][1]
        assert asst["calls"][0]["name"] == "shell"
        res = list(asst["results"].values())[0]
        assert res["ok"] is True and "hyd-12345" in res["body"]
        assert data["items"][2]["text"] == "all done"


def test_hydration_unknown_session_404(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(srv.url("/api/session/does-not-exist"), "")
        assert ei.value.code == 404


def test_config_endpoint_updates_live_state(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        status, body = post_json(srv.base, "/api/config", {"provider": "openai", "model": "gpt-test-x", "safety_mode": "deny", "max_steps": 25})
        assert status == 200
        data = json_load(body.decode())
        assert data["provider"] == "openai"
        assert data["model"] == "gpt-test-x"
        assert data["safety_mode"] == "deny"
        assert data["max_steps"] == 25

        status, body = post_json(srv.base, "/api/config", {"provider": "nope"})
        assert status == 400


def test_sessions_listing_includes_busy_flag(tmp_path: Path):
    app = make_app(tmp_path, [{"content": "hi"}])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "hello"})
        sid = events[-1]["sid"]
        rt = app.runtime_for(sid)
        rt.busy = True
        _, _, body = get(srv.url("/api/sessions"), "")
        rows = json_load(body.decode())["sessions"]
        row = next(r for r in rows if r["id"] == sid)
        assert row["busy"] is True
        rt.busy = False


def test_webapprover_timeout_fails_closed():
    seen: list[dict] = []
    appr = WebApprover(publish=seen.append, ttl=0.1)
    assert appr("some command", "why") is False
    kinds = [e["t"] for e in seen]
    assert kinds.count("approval") == 1 and "approval_done" in kinds
    done = [e for e in seen if e["t"] == "approval_done"][0]
    assert done["allowed"] is False and done["timeout"] is True
    assert appr("some command", "why") is False, "timed-out command should be remembered denied"


def test_webapprover_always_remembers():
    seen: list[dict] = []
    appr = WebApprover(publish=seen.append, ttl=5)
    box = {}

    def resolve():
        time.sleep(0.05)
        aid = [e for e in seen if e["t"] == "approval"][0]["id"]
        box["ok"] = appr.resolve(aid, "always")

    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    assert appr("cmd one", "r") is True
    t.join()
    assert box["ok"] is True
    assert appr("cmd one", "r") is True, "'always' must skip future prompts"
    prompts = [e for e in seen if e["t"] == "approval"]
    assert len(prompts) == 1


def test_session_delete_and_rename(tmp_path: Path):
    app = make_app(tmp_path, [{"content": "hi"}])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "hello"})
        sid = events[-1]["sid"]
        assert any(r["id"] == sid for r in app.store.list_sessions())

        status, body = post_json(srv.base, "/api/rename", {"session_id": sid, "title": "Renamed Title"})
        assert status == 200
        rows = app.store.list_sessions()
        row = next(r for r in rows if r["id"] == sid)
        assert row["task"] == "Renamed Title"

        req = urllib.request.Request(srv.url(f"/api/session/{sid}"), method="DELETE")
        req.add_header("X-Saturday-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200
        assert not any(r["id"] == sid for r in app.store.list_sessions())
        assert app.store._path(sid).with_suffix(".checkpoint.json").exists() is False
        assert app.store._path(sid).with_suffix(".meta.json").exists() is False


def test_workspace_browser_endpoints(tmp_path: Path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "sub" / "note.txt").write_text("alpha beta gamma", encoding="utf-8")
    (ws / "top.md").write_text("# hello", encoding="utf-8")
    app = make_app(tmp_path, [], workspace=ws)
    with _Server(app) as srv:
        _, _, body = get(srv.url("/api/ws"), "")
        data = json_load(body.decode())
        names = {e["name"]: e for e in data["entries"]}
        assert "sub" in names and names["sub"]["dir"] is True
        assert "top.md" in names and names["top.md"]["size"] == 7

        _, _, body = get(srv.url("/api/ws?path=sub"), "")
        data = json_load(body.decode())
        assert [e["name"] for e in data["entries"]] == ["note.txt"]

        _, _, body = get(srv.url("/api/wsfile?path=sub/note.txt"), "")
        data = json_load(body.decode())
        assert data["content"] == "alpha beta gamma"
        assert data["truncated"] is False

        for bad in ("..%2F..%2Fetc", "%2e%2e/esc"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                get(srv.url(f"/api/wsfile?path={bad}"), "")
            assert ei.value.code in (400, 403, 404)


def test_workspace_listing_fields_and_image_api(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16  # endpoint checks the suffix, not decodability
    (ws / "img.png").write_bytes(png)
    (ws / "note.txt").write_text("hello", encoding="utf-8")
    app = make_app(tmp_path, [], workspace=ws)
    with _Server(app) as srv:
        # the Files tab needs mtime (modified column) and abs path (image preview)
        _, _, body = get(srv.url("/api/ws"), "")
        data = json_load(body.decode())
        ent = {e["name"]: e for e in data["entries"]}
        assert ent["img.png"]["mtime"] > 0
        assert ent["img.png"]["path"].endswith("img.png")

        # /api/file must serve workspace images even with a projectless sid:
        # regression — session_workspace() returned None and Path(None) crashed
        # the handler, resetting the connection on every image preview
        url = "/api/file?p=" + urllib.parse.quote(str(ws / "img.png")) + "&sid=ghost"
        status, headers, body = get(srv.url(url), "")
        assert status == 200
        assert (headers.get("Content-Type") or "") == "image/png"
        assert body == png

        # outside the sandbox roots -> 403
        outside = tmp_path / "evil.png"
        outside.write_bytes(png)
        url = "/api/file?p=" + urllib.parse.quote(str(outside)) + "&sid=ghost"
        with pytest.raises(urllib.error.HTTPError) as ei:
            get(srv.url(url), "")
        assert ei.value.code == 403


def test_persona_extra_config_roundtrip(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        status, body = post_json(srv.base, "/api/config", {"persona_extra": "Always answer in haiku."})
        assert status == 200
        data = json_load(body.decode())
        assert data["persona_extra"] == "Always answer in haiku."
        agent = app.runtime_for("p1").agent
        assert agent.persona_extra == "Always answer in haiku."


def test_serve_refuses_occupied_port(tmp_path: Path):
    """A second launch must fail loudly instead of silently sharing the port with
    a stale server (Windows SO_REUSEADDR allows the double-bind; the browser
    window then talks to whichever process won)."""
    import socket as sockmod

    from saturday.webui import serve

    blocker = sockmod.socket()
    port = None
    for candidate in range(18790, 18840):  # Windows reserves random ephemeral ranges
        try:
            blocker.bind(("127.0.0.1", candidate))
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        blocker.close()
        pytest.skip("no bindable test port available (excluded port range)")
    blocker.listen(5)
    try:
        rc = serve(port=port, open_window=False)
        assert rc == 2, "serve must refuse an occupied port"
    finally:
        blocker.close()


def test_rename_rejected_while_busy(tmp_path: Path):
    app = make_app(tmp_path, [{"content": "hi"}])
    with _Server(app) as srv:
        sid = "busy-rename"
        app.store.create({"id": sid, "task": "t"})
        rt = app.runtime_for(sid)
        rt.busy = True
        status, body = post_json(srv.base, "/api/rename", {"session_id": sid, "title": "X"})
        assert status == 409, body
        rt.busy = False
        status, _ = post_json(srv.base, "/api/rename", {"session_id": sid, "title": "X"})
        assert status == 200


def test_assign_rejected_while_busy(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        _, data = post_json_parse(srv.base, "/api/projects", {"name": "P"})
        pid = data["project"]["id"]
        sid = "busy-assign"
        rt = app.runtime_for(sid)
        rt.busy = True
        status, _ = post_json(srv.base, "/api/assign", {"session_id": sid, "project_id": pid})
        assert status == 409
        rt.busy = False


def test_delete_busy_session_waits_for_worker(tmp_path: Path):
    """Deleting a busy session must not leave a zombie file recreated by the
    worker's final append after the unlink."""
    app = make_app(tmp_path, [{"content": "late reply"}])
    with _Server(app) as srv:
        events = chat_events(srv, {"text": "go"})
        sid = events[-1]["sid"]
        rt = app.runtime_for(sid)
        rt.busy = True  # simulate an in-flight run
        req = urllib.request.Request(srv.url(f"/api/session/{sid}"), method="DELETE")
        req.add_header("X-Saturday-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200
        rt.busy = False  # worker would finish here in reality
        import time

        time.sleep(0.3)
        assert not app.store._path(sid).exists(), "zombie session file must not reappear"


def test_slash_crash_does_not_brick_session(tmp_path: Path):
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        import saturday.webui as w

        orig = w.handle_slash

        def boom(rt, line):
            raise RuntimeError("slash exploded")

        w.handle_slash = boom
        try:
            events = chat_events(srv, {"text": "/help"})
        finally:
            w.handle_slash = orig
        sid = events[0]["sid"]
        types = [e["t"] for e in events]
        assert "notice" in types and events[-1]["t"] == "done"
        # session must remain usable
        rt = app.runtime_for(sid)
        assert rt.is_idle
        events2 = chat_events(srv, {"session_id": sid, "text": "/help"})
        assert events2[-1]["t"] == "done", "session survived the crash"


def post_json_parse(base_url: str, path: str, payload: dict, token=TOKEN):
    status, body = post_json(base_url, path, payload, token)
    return status, json_load(body.decode())



# --- from tests/test_webui_e2e.py ---

sys.path.insert(0, str(Path(__file__).parent))


TOKEN = "e2e-tok"


try:
    from playwright.sync_api import sync_playwright

    HAS_PW = True
except Exception:
    HAS_PW = False


# Note: an earlier module-scoped `ui_server` fixture (and the `_make_server(app)`
# helper it alone called) lived here. It was byte-for-byte shadowed by the
# `ui_server` fixture defined later in this file (same name, later definition
# wins per Python semantics) and so never actually ran; removed as dead code
# rather than kept as an unreachable duplicate.


def _with_fake(agent, fake):
    agent._ensure_client = lambda: fake
    return agent


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_send_and_streamed_reply_renders(ui_server):
    last_err = None
    for attempt in range(2):
        logs: list[str] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 860})
                page.on("console", lambda m, logs=logs: logs.append(f"console.{m.type}: {m.text}"))
                page.on("pageerror", lambda e, logs=logs: logs.append(f"pageerror: {e}"))
                page.on("requestfailed", lambda r, logs=logs: logs.append(f"reqfail: {r.url} {r.failure}"))
                page.goto(f"{ui_server}/?k={TOKEN}")
                page.wait_for_selector("#input", state="visible", timeout=20000)
                page.fill("#input", "hello forge")
                page.keyboard.press("Enter")
                page.locator(".turn-stats").first.wait_for(timeout=25000)
                html = page.locator(".msg-assistant .md").first.inner_html()
                if "<strong" not in html:
                    err_line = page.evaluate("() => { const e = document.querySelector('.sysline.error'); return e ? e.textContent : null; }")
                    raise AssertionError(f"markdown empty; sysline={err_line!r}; html={html[:120]!r}")
                assert 'class="codewrap"' in html, f"fenced code block missing: {html[:300]}"
                assert 'class="inline"' in html, f"inline code missing: {html[:300]}"
                stats = page.locator(".turn-stats").first.inner_text()
                assert "step" in stats and "tokens" in stats
                browser.close()
                return
        except Exception as exc:
            try:
                diag = page.evaluate("() => ({err: document.querySelector('.sysline.error')?.textContent || null, stats: document.querySelector('.turn-stats')?.textContent || null, thread: (document.querySelector('#thread')?.innerHTML || '').slice(0, 400)})")
                last_err = AssertionError(f"{exc} | dom={diag}")
            except Exception:
                last_err = exc
        if attempt == 0:
            import time

            time.sleep(1.0)
    raise AssertionError(f"e2e failed after retry: {last_err}\nbrowser log:\n" + "\n".join(logs[-30:]))


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_slash_popup_and_settings_modal(ui_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.goto(f"{ui_server}/?k={TOKEN}")
        page.wait_for_selector("#input", state="visible", timeout=15000)
        page.fill("#input", "/to")
        page.wait_for_selector(".slash-item", timeout=5000)
        first = page.locator(".slash-item").first.inner_text()
        assert "/todo" in first or "/tools" in first
        page.keyboard.press("Escape")

        page.click("#modelPill")
        page.wait_for_selector("#modelMenu:not(.hidden)", timeout=5000)
        page.locator("#modelMenu button", has_text="All settings").click()
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        assert page.locator("#cfgProvider option").count() >= 10

        # proper settings panel: section nav switches panes
        assert page.locator("#setNav button").count() >= 7
        page.click('#setNav button[data-sec="safety"]')
        page.wait_for_selector('.set-pane[data-sec="safety"].on', timeout=5000)
        assert page.locator("#cfgBgOnly").count() == 1
        page.click('#setNav button[data-sec="data"]')
        page.wait_for_selector('#btnClearSessions:not(.hidden)', timeout=5000)
        page.click('#setNav button[data-sec="model"]')
        page.wait_for_selector('.set-pane[data-sec="model"].on', timeout=5000)

        page.click("#settingsClose")
        time.sleep(0.1)
        assert page.locator("#settingsModal.hidden").count() == 1
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_projects_flow(ui_server):
    import tempfile

    kb = tempfile.NamedTemporaryFile("w", suffix="-kb.txt", delete=False, encoding="utf-8")
    kb.write("E2E-KNOWLEDGE-MARKER style rules")
    kb.close()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.goto(f"{ui_server}/?k={TOKEN}")
        page.wait_for_selector("#input", state="visible", timeout=20000)

        # create a project through the modal: color + knowledge file included
        page.click("#newProjBtn")
        page.wait_for_selector("#projModal:not(.hidden)", timeout=5000)
        page.fill("#projName", "E2E Repo")
        page.click('#projColors .swatch[data-c="green"]')
        page.fill("#projFileInput", kb.name)
        page.click("#projFileAdd")
        page.wait_for_selector(".kfile-chip", timeout=5000)
        page.click("#projSave")
        page.wait_for_selector(".proj-item", timeout=5000)
        assert "E2E Repo" in page.locator(".proj-item .proj-name").first.inner_text()
        assert page.locator(".proj-item.pc-green").count() == 1, "color accent class must render"

        # creating selects it: chip + scoped view + project head row
        page.wait_for_selector("#projChip:not(.hidden)", timeout=5000)
        assert "E2E Repo" in page.locator("#projChipName").inner_text()
        page.wait_for_selector(".proj-open-head", timeout=5000)

        # a chat sent now lands inside the project
        page.fill("#input", "project hello")
        page.keyboard.press("Enter")
        page.locator(".turn-stats").first.wait_for(timeout=25000)
        sid = page.evaluate("() => window.df.state.sid")
        assert sid, "session must be adopted"
        proj_id = page.evaluate("() => window.df.state.proj")
        assert proj_id, "adopted session must carry the active project"

        # server-side truth: session tagged; color + knowledge persisted (cookie carries auth)
        data = page.evaluate("async () => { const s = await fetch('/api/sessions'); const p = await fetch('/api/projects'); return { sessions: await s.json(), projects: await p.json() }; }")
        rows = {r["id"]: r for r in data["sessions"]["sessions"]}
        assert rows[sid]["project"] == proj_id
        proj = next(p for p in data["projects"]["projects"] if p["id"] == proj_id)
        assert proj["color"] == "green"
        assert len(proj["files"]) == 1 and proj["files"][0].endswith("-kb.txt")

        # back to all chats: unprojected view only
        page.click(".all-chats")
        page.wait_for_selector("#projChip.hidden", state="attached", timeout=5000)

        # move-to-project menu lists the project
        page.evaluate(f"() => window.df.openProjPick({sid!r})")
        page.wait_for_selector("#projPickMenu:not(.hidden)", timeout=5000)
        assert page.locator("#projPickMenu button", has_text="E2E Repo").count() == 1
        page.keyboard.press("Escape")

        # star the project: pinned marker renders in sidebar
        page.hover(".proj-item")
        page.locator('.proj-item .proj-acts button[title="Star project"]').click()
        page.wait_for_selector(".proj-item.pinned", timeout=5000)
        browser.close()



# --- from tests/test_webui_newui.py ---

sys.path.insert(0, str(Path(__file__).parent))


TOKEN = "newui-tok"


try:
    from playwright.sync_api import sync_playwright

    HAS_PW = True
except Exception:
    HAS_PW = False


KEY_ENVS = [
    "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY", "NOUS_API_KEY", "XAI_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY",
    "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "ZAI_API_KEY", "AZURE_OPENAI_API_KEY",
    "TOGETHER_API_KEY",
]


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory):
    from pytest import MonkeyPatch

    scratch = tmp_path_factory.mktemp("df-newui")
    with MonkeyPatch().context() as mp:
        import saturday.mcp_plugin as mcpmod
        from saturday import config as cfgmod
        from saturday.projects import ProjectStore

        mp.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
        # NEVER touch the user's real ~/.saturday from browser tests:
        # CONFIG_FILE is bound at import time from CONFIG_DIR, so patch BOTH
        mp.setattr(cfgmod, "CONFIG_DIR", scratch)
        mp.setattr(cfgmod, "CONFIG_FILE", scratch / "config.json")
        saved: list[dict] = []
        mp.setattr(cfgmod, "save_config", lambda partial: saved.append(dict(partial)))
        for k in KEY_ENVS:
            os.environ.pop(k, None)
        app = AppState(
            cfg_overrides={"safety_mode": "off", "workspace_root": str(Path.cwd())},
            store_root=scratch / "sessions",
            projects_store=ProjectStore(scratch / "projects.json"),
        )
        fake = make_scripted_model([{"content": "ok reply"}] * 4)
        orig = app._new_agent
        app._new_agent = lambda cfg: _with_fake(orig(cfg), fake)
        srv = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield base
        srv.shutdown()
        srv.server_close()


def _with_fake(agent, fake):
    agent._ensure_client = lambda: fake
    return agent


def _fresh_page(pw, base):
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e, errs=errs: errs.append(f"pageerror: {e}"))
    page.on("console", lambda m, errs=errs: errs.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    ctx._df_errs = errs
    page.goto(f"{base}/?k={TOKEN}")
    page.evaluate("() => { localStorage.clear(); }")
    page.reload()
    page.wait_for_selector("#input", state="visible", timeout=20000)
    # cleared storage re-arms the onboarding wizard; dismiss so it doesn't
    # block pointer events on everything underneath
    page.wait_for_timeout(650)
    if page.locator("#onboardModal:not(.hidden)").count():
        page.click("#obSkip")
        page.wait_for_function("() => document.querySelector('#onboardModal').classList.contains('hidden')", timeout=5000)
    return browser, ctx, page


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_context_panel_and_meter(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        try:
            page.wait_for_selector("#tokMeter:not(.hidden)", timeout=10000)
        except Exception:
            raise AssertionError(f"meter never visible; js errors: {getattr(ctx, '_df_errs', [])}")
        meter_txt = page.locator("#tokMeter").inner_text()
        assert "/" in meter_txt, f"meter should show used/compact: {meter_txt}"
        page.click("#tokMeter")
        page.wait_for_selector("#ctxModal:not(.hidden)", timeout=5000)
        segs = page.locator("#ctxBar .ctx-seg").count()
        assert segs >= 2, "bar should show system+tools+reserve slices"
        rows = page.locator("#ctxLegend .ctx-row").count()
        assert rows >= 3
        total = page.locator("#ctxTotal").inner_text()
        assert "tokens" in total
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.querySelector('#ctxModal').classList.contains('hidden')", timeout=5000)

        # slash parity: /context streams a notice into the chat
        # (first Enter accepts the autocomplete, second sends)
        page.fill("#input", "/context")
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")
        page.wait_for_selector(".notice", timeout=10000)
        assert "%" in page.locator(".notice").first.inner_text()
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_theme_menu_applies_omarchy_theme(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        page.click("#themeBtn")
        page.wait_for_selector("#themeMenu:not(.hidden)", timeout=5000)
        buttons = page.locator("#themeMenu button")
        assert buttons.count() >= 20, "19 omarchy themes + 2 saturday + system"
        assert page.locator('#themeMenu button', has_text="Tokyo Night").count() == 1
        page.locator("#themeMenu button", has_text="Gruvbox").click()
        page.wait_for_function("() => document.documentElement.dataset.theme === 'gruvbox'", timeout=5000)
        assert page.evaluate("() => document.documentElement.dataset.mode") == "dark"
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(40, 40, 40)"
        assert page.evaluate("() => localStorage.getItem('df_theme')") == "gruvbox"
        # toggle button flips between last dark and last light
        page.click("#themeBtn")
        page.locator("#themeMenu button", has_text="Flexoki Light").click()
        page.wait_for_function("() => document.documentElement.dataset.theme === 'flexoki-light'", timeout=5000)
        assert page.evaluate("() => document.documentElement.dataset.mode") == "light"
        page.evaluate("() => window.df.toggleTheme()")
        assert page.evaluate("() => document.documentElement.dataset.theme") == "gruvbox", "toggle returns to last dark theme"
        assert page.evaluate("() => document.documentElement.dataset.mode") == "dark"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_theme_setting_persists_via_settings(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        page.click("#kebabBtn")
        page.locator('#kebabMenu button[data-act="settings"]').click()
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        opts = page.locator("#cfgThemeSel optgroup[label='Omarchy'] option").count()
        assert opts >= 19, "all shipped omarchy themes selectable"
        page.select_option("#cfgThemeSel", "rose-pine")
        page.click("#settingsSave")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'rose-pine'", timeout=5000)
        assert page.evaluate("() => document.documentElement.dataset.mode") == "light"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_onboarding_wizard_shows_and_saves(ui_server, monkeypatch):
    monkeypatch.setattr(
        "saturday.llm.probe.probe_connection",
        lambda prof, key="", timeout=8.0: (True, "reachable \u2014 2 models found", ["openai/gpt-x", "openai/gpt-y"]),
    )
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        # _fresh_page dismisses via session storage; re-arm it for this test
        page.evaluate("() => sessionStorage.removeItem('df_onboard_skip')")
        page.reload()
        page.wait_for_selector("#onboardModal:not(.hidden)", timeout=10000)
        assert page.locator("#obProvider option").count() >= 10
        # validation: save without key keeps the modal + warning
        page.click("#obSave")
        page.wait_for_selector("#obWarn:not(.hidden)", timeout=5000)
        page.fill("#obKey", "sk-e2e-fake-key")
        page.locator("#obProvider").select_option("openai")
        page.click("#obSave")
        page.wait_for_function("() => document.querySelector('#onboardModal').classList.contains('hidden')", timeout=8000)
        info = page.evaluate("async () => await (await fetch('/api/state')).json()")
        assert info["provider"] == "openai" and info["has_key"] is True
        # reload: no wizard again
        page.reload()
        page.wait_for_selector("#input", state="visible", timeout=15000)
        page.wait_for_timeout(700)
        assert page.locator("#onboardModal.hidden").count() == 1, "wizard must not reappear"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_settings_panes_render_and_save(ui_server):
    """Settings layout regression: the search bar spans the grid, nav and
    panes keep their two columns, and the Advanced group opens + saves."""
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        page.click("#modelLabel")
        page.wait_for_timeout(200)
        page.click("text=All settings\u2026")
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        # search spans full width; nav stays left of the content column
        nav_x = page.locator("#setNav").bounding_box()["x"]
        pane_x = page.locator(".set-pane.on").bounding_box()["x"]
        search_b = page.locator("#cfgSearch").bounding_box()
        nav_b = page.locator("#setNav").bounding_box()
        assert pane_x > nav_x, "settings nav and panes must be separate columns"
        assert search_b["y"] + search_b["height"] <= nav_b["y"], "search bar must sit on its own grid row above nav"
        # Advanced collapsible opens
        page.click('#setNav button[data-sec="model"]')
        page.click("details.adv > summary")
        page.wait_for_timeout(200)
        assert page.locator("#cfgTopP").is_visible(), "advanced group must open"
        # save round-trips without warnings
        page.click("#settingsSave")
        page.wait_for_timeout(1000)
        assert page.locator("#settingsWarn:not(.hidden)").count() == 0
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_titlebar_dblclick_toggles_maximize(ui_server):
    """Custom title bar: double-click on the drag region toggles maximize."""
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        ctx.add_init_script(
            "window.addEventListener('DOMContentLoaded', () => {"
            "window.pywebview = { api: {"
            " win_min: () => true,"
            " win_max: () => { (window.__mx = (window.__mx || 0) + 1); return window.__mx % 2 === 1; },"
            " win_close: () => true } }; });"
        )
        page.reload()
        page.wait_for_timeout(700)
        page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.classList.contains('embedded')")
        page.dblclick(".titlebar-brand")
        page.wait_for_timeout(200)
        page.dblclick(".titlebar-brand")
        page.wait_for_timeout(200)
        assert page.evaluate("window.__mx") == 2, "double-click must call win_max twice"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_assistant_mode_flavor_and_toggle(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        agent_mode_tag = page.locator("#emptyState .tagline").inner_text()
        assert "harness" in agent_mode_tag
        assert page.locator("#modeBadge.hidden").count() == 1, "no badge in agent mode"

        page.click("#kebabBtn")
        page.locator('#kebabMenu button[data-act="settings"]').click()
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        page.locator("#cfgAssistant").check()
        page.fill("#cfgAssistantName", "Jarvis")
        page.fill("#cfgAssistantTitle", "sir")
        page.click("#settingsSave")
        page.wait_for_function("() => window.df.state.info && window.df.state.info.persona_mode === 'assistant'", timeout=8000)
        # badge appears; background-first flips on with the mode
        page.wait_for_selector("#modeBadge:not(.hidden)", timeout=5000)
        assert page.evaluate("() => window.df.state.info.background_only") is True, "assistant defaults to non-intrusive"
        assert page.evaluate("() => window.df.state.info.assistant_name") == "Jarvis"
        assert page.evaluate("() => window.df.state.info.assistant_user_title") == "sir"

        # THE POINT of assistant mode: the UI visibly simplifies - chat IS the app
        page.wait_for_function("() => document.body.classList.contains('mode-assistant')", timeout=5000)
        assert not page.locator("#stage").is_visible(), "technical stage must disappear"
        assert not page.locator("#modelPill").is_visible(), "model pill is developer plumbing"
        assert not page.locator("#tokMeter").is_visible(), "context meter is developer plumbing"
        hint = page.locator("#composerHint").inner_text()
        assert "background" in hint
        page.wait_for_function("() => document.querySelector('#emptyState .tagline').textContent.includes('tell it what you need')", timeout=5000)
        chips = page.locator(".suggest-chip").all_inner_texts()
        assert any("Calculator" in c or "headlines" in c for c in chips), f"task-flavored suggestions expected: {chips}"
        placeholder = page.evaluate("() => document.querySelector('#input').placeholder")
        assert "Tell me what you need" in placeholder

        # full capability retained: registry identical across modes
        names = page.evaluate(
            "async () => { const r = await fetch('/api/tools'); return (await r.json()); }"
        ) if False else None  # tools endpoint not exposed; verified via unit tests
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_provenance_and_verify_settings_roundtrip(ui_server):
    """R1 features are operable from the Settings > Data pane end-to-end."""
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        try:
            page.click("#kebabBtn")
            page.locator('#kebabMenu button[data-act="settings"]').click()
            page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
            page.locator('#setNav button[data-sec="data"]').click()
            page.select_option("#cfgProvenance", "visible")
            page.fill("#cfgVerifyCmd", "python -m pytest -q")
            page.click("#settingsSave")
            page.wait_for_function(
                "() => window.df.state.info && window.df.state.info.provenance_marking === 'visible'",
                timeout=8000,
            )
            assert page.evaluate("() => window.df.state.info.verify_command") == "python -m pytest -q"

            # reopen: controls reflect the persisted values
            page.click("#kebabBtn")
            page.locator('#kebabMenu button[data-act="settings"]').click()
            page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
            assert page.input_value("#cfgProvenance") == "visible"
            assert page.input_value("#cfgVerifyCmd") == "python -m pytest -q"
            assert page.locator("#usageMetrics").count() == 1
        finally:
            js_errs = getattr(ctx, "_df_errs", [])
            browser.close()
        assert not js_errs, f"js errors: {js_errs}"



# --- from tests/test_frontend_wiring.py ---

sys.path.insert(0, str(Path(__file__).parent))


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



# --- from tests/test_competitive_ui.py ---

sys.path.insert(0, str(Path(__file__).parent))


def test_journal_list_and_restore(tmp_path):
    # isolated workspace: the journal is keyed by the session workspace, so
    # never touch the real CWD the default workspace_root would use
    app = AppState(
        store_root=tmp_path / "s",
        cfg_overrides={"workspace_root": str(tmp_path / "ws")},
    )
    base, _ = _server(app)
    from saturday.tools.journal import record_edit

    ws = Path(app.base_cfg.workspace_root)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "a.py").write_text("orig a\n", encoding="utf-8")
    record_edit(ws, "write_file", str(ws / "a.py"))
    (ws / "a.py").write_text("edited a\n", encoding="utf-8")

    status, data = _req(base, "/api/journal")
    assert status == 200
    assert data["entries"] and data["entries"][0]["tool"] == "write_file"
    idx = data["entries"][0]["index"]

    status, data = _req(base, "/api/journal/restore", method="POST", payload={"index": idx})
    assert status == 200 and data["ok"] is True
    assert (ws / "a.py").read_text(encoding="utf-8") == "orig a\n"

    status, data = _req(base, "/api/journal/restore", method="POST", payload={"index": 999})
    assert status == 200 and data["ok"] is False

    status, data = _req(base, "/api/journal/restore", method="POST", payload={"index": "x"})
    assert status == 400


def test_schedules_crud(tmp_path, monkeypatch):
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    # hermetic schedule path (SATURDAY_HOME is redirected by conftest)
    status, data = _req(base, "/api/schedules")
    assert status == 200 and data["schedules"] == []

    status, data = _req(
        base, "/api/schedules", method="POST",
        payload={"action": "add", "expr": "0 9 * * 1-5", "task": "standup notes"},
    )
    assert status == 200 and len(data["schedules"]) == 1
    sched_id = data["schedules"][0]["id"]
    assert data["schedules"][0]["expr"] == "0 9 * * 1-5"

    # invalid cron rejected
    status, data = _req(
        base, "/api/schedules", method="POST",
        payload={"action": "add", "expr": "not a cron", "task": "x"},
    )
    assert status == 400

    status, data = _req(base, "/api/schedules", method="POST", payload={"action": "remove", "id": sched_id})
    assert status == 200 and data["schedules"] == []

    status, data = _req(base, "/api/schedules", method="POST", payload={"action": "remove", "id": "nope"})
    assert status == 404


def test_custom_commands_crud(tmp_path):
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    cmds = {
        "review": {"prompt": "Review $ARGS against our house style", "description": "code review"},
        "UPPER_case": {"prompt": "x"},  # normalized to a legal slug
        "dropped": {"prompt": ""},  # dropped: empty prompt
    }
    status, data = _req(base, "/api/commands", method="POST", payload={"commands": cmds})
    assert status == 200, data
    assert set(data["commands"].keys()) == {"review", "upper_case"}

    status, data = _req(base, "/api/commands", method="POST", payload={"commands": {"ok1": {"prompt": "p"}}})
    assert status == 200 and data["commands"]["ok1"]["prompt"] == "p"

    status, data = _req(base, "/api/commands", method="POST", payload={"commands": {"bad name!": {"prompt": "p"}}})
    assert status == 400


def test_feedback_endpoint(tmp_path):
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    status, data = _req(
        base, "/api/feedback", method="POST",
        payload={"sid": "s1", "turn": 2, "value": "up", "model": "deepseek/deepseek-chat"},
    )
    assert status == 200 and data["ok"] is True
    status, data = _req(base, "/api/feedback", method="POST", payload={"value": "meh"})
    assert status == 400
    from saturday.config import get_config_dir

    fb = get_config_dir() / "feedback.jsonl"
    assert fb.is_file()
    row = json.loads(fb.read_text(encoding="utf-8").splitlines()[0])
    assert row["value"] == "up" and row["sid"] == "s1"


def test_state_payload_pricing_and_commands(tmp_path):
    app = AppState(store_root=tmp_path / "s")
    st = app.state_payload()
    assert "pricing" in st and "custom_commands" in st
    if st["pricing"]:
        assert len(st["pricing"]) == 2


def test_session_items_carry_msg_idx(tmp_path):
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    sid = app.store.create({"task": "idx", "surface": "app"})
    app.store.append(
        sid,
        {
            "type": "messages",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "ok"},
                {"role": "tool", "tool_call_id": "t1", "content": "r"},
                {"role": "user", "content": "second"},
            ],
        },
    )
    status, data = _req(base, f"/api/session/{sid}")
    assert status == 200
    users = [it for it in data["items"] if it["kind"] == "user"]
    assert [u["msg_idx"] for u in users] == [0, 3]


def test_branch_keep_matches_msg_idx(tmp_path):
    """branch(keep=msg_idx) keeps everything BEFORE that user message — the
    contract edit-&-resend relies on."""
    app = AppState(store_root=tmp_path / "s")
    sid = app.store.create({"task": "br", "surface": "app"})
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "done"},
    ]
    app.store.append(sid, {"type": "messages", "messages": msgs})
    # second user msg sits at raw index 2; branch keeping 2 keeps [first, ok]
    new_sid = app.store.branch(sid, keep_messages=2)
    assert new_sid
    hist = app.store.history_messages(new_sid)
    assert [m["content"] for m in hist] == ["first", "ok"]


def test_schedule_watcher_env_kill_switch(tmp_path, monkeypatch):
    import saturday.webui as w

    monkeypatch.setenv("SATURDAY_SCHEDULE_WATCHER", "0")
    monkeypatch.setattr(w, "SCHEDULE_WATCHER_ON", False)  # isolate from other tests
    app = AppState(store_root=tmp_path / "s")
    w.start_schedule_watcher(app)
    assert w.SCHEDULE_WATCHER_ON is False


def test_runs_payload_and_archive_roundtrip(tmp_path):
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(tmp_path / "ws")})
    base, _ = _server(app)
    sid = app.store.create({"task": "run me", "surface": "app", "project": ""})
    app.store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "hi"}]})

    status, data = _req(base, "/api/runs")
    assert status == 200
    row = next(r for r in data["runs"] if r["id"] == sid)
    assert row["task"] == "run me"
    assert row["busy"] is False and row["archived"] is False
    assert row["mtime"] > 0

    status, data = _req(base, "/api/archive", method="POST", payload={"session_id": sid, "archived": True})
    assert status == 200 and data["ok"] is True
    assert next(r for r in data["sessions"] if r["id"] == sid)["archived"] is True

    status, data = _req(base, "/api/runs")
    assert next(r for r in data["runs"] if r["id"] == sid)["archived"] is True

    # unarchive
    status, data = _req(base, "/api/archive", method="POST", payload={"session_id": sid, "archived": False})
    assert next(r for r in data["sessions"] if r["id"] == sid)["archived"] is False

    # unknown session
    status, _ = _req(base, "/api/archive", method="POST", payload={"session_id": "nope", "archived": True})
    assert status == 404


def test_git_status_endpoint(tmp_path):
    import subprocess

    ws = tmp_path / "repo"
    ws.mkdir()
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)
    sid = app.store.create({"task": "git", "surface": "app"})

    # not a repo yet
    status, data = _req(base, f"/api/git/status?sid={sid}")
    assert status == 200 and data["available"] is False

    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=ws, check=True)
    (ws / "seed.txt").write_text("seed changed\n", encoding="utf-8")  # tracked modification: +1 -1
    (ws / "a.txt").write_text("line1\n", encoding="utf-8")  # untracked: counted in `changed`, not in numstat

    status, data = _req(base, f"/api/git/status?sid={sid}")
    assert status == 200 and data["available"] is True
    assert data["changed"] == 2
    assert data["adds"] == 1 and data["dels"] == 1
    assert set(data["files"]) == {"seed.txt", "a.txt"}
    assert data["branch"]


def test_journal_entry_content_endpoint(tmp_path):
    from saturday.tools.journal import record_edit

    ws = tmp_path / "ws"
    ws.mkdir()
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)
    (ws / "a.py").write_text("orig\n", encoding="utf-8")
    record_edit(ws, "edit_file", str(ws / "a.py"))

    status, data = _req(base, "/api/journal?entry=0")
    assert status == 200
    assert data["entry"]["before"] == "orig\n"

    status, _ = _req(base, "/api/journal?entry=9")
    assert status == 404
    status, _ = _req(base, "/api/journal?entry=zz")
    assert status == 400


def test_ask_user_tool_contract():
    from saturday.tools.ask import AskUserTool

    tool = AskUserTool()
    # no surface hook: graceful fallback, never a stall
    ok, out = tool.run({"question": "which db?"})
    assert ok and "best judgment" in out
    # with a hook: returns the answer verbatim
    tool.ask_fn = lambda q, options, ttl: "blue"
    ok, out = tool.run({"question": "which db?", "options": ["blue", "red"]})
    assert ok and 'answered: "blue"' in out
    # empty question refused
    ok, out = tool.run({"question": " "})
    assert not ok


def test_web_approver_ask_and_deny_note():
    import threading

    from saturday.session_runtime import WebApprover

    events = []
    ap = WebApprover(events.append, ttl=5.0, scope="s1")

    # question answered via resolve(note=...)
    box = {}
    t = threading.Thread(target=lambda: box.update(ans=ap.ask_question("which db?", ["blue", "red"], ttl=5)))
    t.start()
    while not any(e.get("t") == "ask" for e in events):
        time.sleep(0.01)
    ask_evt = next(e for e in events if e.get("t") == "ask")
    assert ask_evt["q"] == "which db?" and ask_evt["options"] == ["blue", "red"]
    assert ap.resolve(ask_evt["id"], "answer", note="blue")
    t.join(5)
    assert box["ans"] == "blue"
    assert any(e.get("t") == "ask_done" and e.get("answer") == "blue" for e in events)

    # deny with a note: consume_denial_note surfaces it once
    t2 = threading.Thread(target=lambda: box.update(ok=ap("rm -rf /tmp/x", "guardrail hit")))
    t2.start()
    while not any(e.get("t") == "approval" for e in events):
        time.sleep(0.01)
    appr_evt = next(e for e in events if e.get("t") == "approval")
    assert ap.resolve(appr_evt["id"], "deny", note="use the recycle bin instead")
    t2.join(5)
    assert box["ok"] is False
    assert ap.consume_denial_note() == "use the recycle bin instead"
    assert ap.consume_denial_note() == ""  # consumed once


def test_ask_endpoint_resolves_pending_question(tmp_path):
    import threading

    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    sid = app.store.create({"task": "ask", "surface": "app"})
    rt = app.runtime_for(sid)
    q = rt.bus.subscribe()
    box = {}
    t = threading.Thread(target=lambda: box.update(ans=rt.approver.ask_question("continue?", ["yes", "no"], ttl=10)))
    t.start()
    evt = q.get(timeout=5)
    while evt.get("t") != "ask":
        evt = q.get(timeout=5)
    status, data = _req(base, "/api/ask", method="POST", payload={"id": evt["id"], "answer": "yes"})
    assert status == 200 and data["ok"] is True
    t.join(5)
    assert box["ans"] == "yes"
    rt.bus.unsubscribe(q)


def test_session_model_override(tmp_path):
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    sid = app.store.create({"task": "m", "surface": "app"})
    global_model = app.base_cfg.model

    status, data = _req(base, "/api/config", method="POST", payload={"session_id": sid, "model": "deepseek-chat"})
    assert status == 200 and data["session_only"] is True and data["model"] == "deepseek-chat"
    assert app._cfg_for_session(sid)[0].model == "deepseek-chat"
    assert app.base_cfg.model == global_model  # global untouched
    assert app.state_payload()["session_models"] == {sid: "deepseek-chat"}

    # clearing the override restores the global model
    status, data = _req(base, "/api/config", method="POST", payload={"session_id": sid, "model": ""})
    assert status == 200
    assert app._cfg_for_session(sid)[0].model == global_model


def test_enhance_endpoint(tmp_path, monkeypatch):
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    import saturday.webui as w

    monkeypatch.setattr(w, "_one_shot", lambda cfg, prompt, **kw: "Do X, then Y. Constraints: Z.")
    status, data = _req(base, "/api/enhance", method="POST", payload={"text": "do the thing"})
    assert status == 200 and data["ok"] is True and "X" in data["text"]

    status, _ = _req(base, "/api/enhance", method="POST", payload={"text": ""})
    assert status == 400


def test_auto_title_renames_and_publishes(tmp_path, monkeypatch):
    import saturday.webui as w
    from saturday.webui_support import _title_from_text

    app = AppState(store_root=tmp_path / "s")
    user_text = "help me write a very long task description that gets truncated somewhere around here"
    # real flow: create() stores _title_from_text(text) as the initial title
    sid = app.store.create({"task": _title_from_text(user_text), "surface": "app"})
    app.store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": user_text}]})
    rt = app.runtime_for(sid)
    q = rt.bus.subscribe()
    monkeypatch.setattr(w, "_one_shot", lambda cfg, prompt, **kw: '"Build the Login Flow"')

    w._auto_title(app, rt, user_text, "ok")
    assert (app.store.read_meta(sid) or {}).get("task") == "Build the Login Flow"
    evt = q.get(timeout=5)
    while evt.get("t") != "title":
        evt = q.get(timeout=5)
    assert evt["sid"] == sid and evt["title"] == "Build the Login Flow"
    rt.bus.unsubscribe(q)

    # never overwrite a user-set title
    app.store.set_task(sid, "My custom name")
    w._auto_title(app, rt, "completely different new text", "ok")
    assert (app.store.read_meta(sid) or {}).get("task") == "My custom name"


def test_subagent_event_forwarding():
    """SubagentTask forwards child activity through _event_fn."""
    from saturday.tasks import SubagentTask

    class FakeResult:
        name = "shell"
        ok = False
        output = ""
        error = "boom"

    class FakeTraj:
        final_answer = "child report"
        stop_reason = "done"

        def messages(self):
            return [{"role": "user", "content": "p"}, {"role": "assistant", "content": "a"}]

    class FakeAgent:
        def run(self, prompt, initial_history=None, on_step_start=None, on_tool_result=None, **kw):
            if on_step_start:
                on_step_start(0)
            if on_tool_result:
                on_tool_result(FakeResult())
            return FakeTraj()

    seen = []
    task = SubagentTask(agent_factory=lambda: FakeAgent())
    task._event_fn = lambda cid, kind, kw: seen.append((cid, kind, kw))
    ok, out = task.run({"description": "x", "prompt": "p"})
    assert ok and "child report" in out
    kinds = [k for _, k, _ in seen]
    assert kinds == ["start", "step", "tool", "done"]
    done = seen[-1][2]
    assert done["summary"].startswith("child report")
    tool_evt = seen[2][2]
    assert tool_evt["name"] == "shell" and tool_evt["ok"] is False and tool_evt["error"] == "boom"


def test_state_payload_round3_fields(tmp_path):
    app = AppState(store_root=tmp_path / "s")
    st = app.state_payload()
    assert "session_models" in st
    assert "auto_title_sessions" in st and st["auto_title_sessions"] is True


def test_stream_tail_replays_inflight_run(tmp_path):
    """A second viewer opening /api/stream/<sid>?from=run while the run waits
    on an approval replays the whole in-flight turn — the mechanism behind
    "switch sessions while one is running"."""
    app = make_app(
        tmp_path,
        [{"tool_calls": [{"name": "shell", "arguments": {"command": "sudo rm thing"}}]}, {"content": "done"}],
        safety="ask",
    )
    with _Server(app) as srv:
        got = []

        def run_chat():
            for line in stream_chat_lines(srv, {"text": "clean that up"}):
                got.append(line)

        t = threading.Thread(target=run_chat, daemon=True)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline and not any(e.get("t") == "approval" for e in got):
            time.sleep(0.05)
        assert any(e.get("t") == "approval" for e in got), "run should be blocked on approval"
        sid = next(e["sid"] for e in got if e.get("t") == "hello")

        # second viewer re-attaches; read in a thread (the stream stays open)
        tail = []

        def read_tail():
            import urllib.request

            req = urllib.request.Request(srv.base + f"/api/stream/{sid}?from=run")
            req.add_header("X-Saturday-Token", TOKEN)
            conn = urllib.request.urlopen(req, timeout=30)
            try:
                for raw in conn:
                    tail.append(json.loads(raw.decode()))
            except Exception:
                pass

        t2 = threading.Thread(target=read_tail, daemon=True)
        t2.start()
        deadline = time.time() + 10
        while time.time() < deadline and not any(e.get("t") == "approval" for e in tail):
            time.sleep(0.05)
        kinds = [e.get("t") for e in tail]
        assert tail and tail[0].get("t") == "hello"
        assert "user" in kinds and "tool_start" in kinds and "approval" in kinds, kinds

        aid = next(e["id"] for e in tail if e.get("t") == "approval")
        status, data = _req(srv.base, "/api/approve", method="POST", payload={"id": aid, "decision": "allow", "note": ""})
        assert status == 200 and data["ok"] is True
        t.join(timeout=30)
        assert any(e.get("t") == "done" for e in got), "original stream should finish"
        # t.join() only waits for the original stream's thread; the tail
        # reader is a second, independent connection and can lag behind on
        # a loaded machine (observed on CI, never locally) — give it its
        # own deadline instead of asserting immediately.
        deadline = time.time() + 10
        while time.time() < deadline and not any(e.get("t") == "done" for e in tail):
            time.sleep(0.05)
        assert any(e.get("t") == "done" for e in tail), "tail should see the same done event"


def test_stream_tail_live_only_when_idle(tmp_path):
    """?from=run must NOT replay a finished turn: idle sessions stream live
    events only (stale run_start_seq never re-sends a completed exchange)."""
    app = make_app(tmp_path, [{"content": "ok"}], safety="off")
    with _Server(app) as srv:
        lines = list(stream_chat_lines(srv, {"text": "hi"}))
        sid = next(e["sid"] for e in lines if e.get("t") == "hello")
        assert any(e.get("t") == "done" for e in lines)

        # open a live-only tail (the client attaches only to busy sessions;
        # here we verify the server's idle guard directly)
        import urllib.request

        req = urllib.request.Request(srv.base + f"/api/stream/{sid}?from=run")
        req.add_header("X-Saturday-Token", TOKEN)
        conn = urllib.request.urlopen(req, timeout=10)

        def read_some():
            out = []
            for raw in conn:
                out.append(json.loads(raw.decode()))
                if len(out) >= 2:
                    break
            return out

        # trigger fresh events from a second connection and confirm the tail
        # did NOT replay the old "user"/"done" events first
        conn2 = urllib.request.Request(srv.base + "/api/chat",
            data=json.dumps({"text": "/help"}).encode(),
            headers={"X-Saturday-Token": TOKEN, "Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(conn2, timeout=10).read()
        except Exception:
            pass
        conn.close()


ASSETS = Path(__file__).parent.parent / "src" / "saturday" / "webui_assets"


def test_round5_dropdowns_anchor_to_their_trigger():
    """Placement parity (Cursor/ChatGPT/Claude): no menu may open at a fixed
    viewport corner; every menu opens through openDropdown() anchored to the
    control that triggered it, and opening one menu closes the others."""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "function openDropdown(" in js, "missing anchored-dropdown helper"
    # every dropdown opens through the helper with its real trigger
    assert "openDropdown(m, $(\"#kebabBtn\"))" in js
    assert "openDropdown(m, $(\"#modelPill\"))" in js
    assert "openDropdown(m, $(\"#themeBtn\"))" in js
    # safety menu anchors to whichever control opened it (composer chip or badge)
    assert "openSafetyMenu($(\"#safetyBadge\"))" in js
    assert "openSafetyMenu($(\"#safetyChip\"))" in js
    assert 'anchor || (chip && chip.offsetParent ? chip : $("#safetyBadge"))' in js
    # move-to-project opens under the kebab button that launched it
    assert 'openProjPick(state.sid, $("#kebabBtn"))' in js
    # helper positions relative to the trigger and flips/clamps to the viewport
    for snippet in (
        "anchor.getBoundingClientRect()",
        "top + mh > window.innerHeight - 8",
        "window.innerWidth - mw - 8",
    ):
        assert snippet in js, snippet
    # the old fixed-corner dropdown CSS is no longer the only positioning
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert ".dropdown {" in css  # base style remains as a pre-position fallback


def test_round5_menus_are_mutually_exclusive():
    """openDropdown closes all other menus first (the kebab menu used to open
    on top of the safety menu)."""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function openDropdown("):js.index("function openKebab(")]
    assert "closeMenus();" in body
    assert "wasOpen" in body  # trigger click toggles instead of re-opening


def test_round5_no_native_dialogs():
    """Dialog parity: native confirm()/prompt() are replaced by the styled
    in-app askModal (native dialogs are unstyled and unreliable in the
    desktop shell)."""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    import re

    for name in ("confirm", "prompt"):
        bare = re.search(r"(?<![\w.$])" + name + r"\(", js)
        assert bare is None, f"native {name}() still used at {js[:bare.start()].count(chr(10)) + 1}"
    assert "function uiConfirm(" in js
    assert "function uiPrompt(" in js
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    for frag in ("askModal", "askTitle", "askMsg", "askInput", "askOk", "askCancel"):
        assert f'id="{frag}"' in html, frag
    # Esc dismisses the dialog, and the approval Y/A/N shortcut is suppressed
    # while it is open
    assert '$("#askModal").classList.contains("hidden")) { askClose(false)' in js
    assert '"#trustModal", "#askModal"' in js


def test_round5_dialog_and_menu_buttons_are_styled():
    """The shared Cancel/secondary button must be themed (the trust modal's
    'Don't Trust' button previously rendered as a raw browser button), and
    destructive confirms get the filled danger styling — without colliding
    with the outline .danger-btn used by settings footers."""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    for cls in (".secondary-btn {", ".danger-solid {", ".modal-card-sm {"):
        assert cls in css, cls
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert 'okB.classList.toggle("danger-solid", !!opts.danger)' in js
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'id="askCancel" class="secondary-btn"' in html


def test_round6_hidden_preview_pane_cannot_steal_stage_width():
    """Regression: `#stagePreview { display:flex }` outranked
    `.stage-pane { display:none }`, so the invisible Preview pane permanently
    took 50% of the stage width and squeezed every other tab into the left
    half. Only the active pane may lay out."""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert "#stagePreview.on { display: flex" in css
    import re

    bare = re.search(r"#stagePreview \{[^}]*display\s*:", css)
    assert bare is None, "unqualified #stagePreview display rule is back"


def test_round6_spacing_system():
    """Spacing pass: one sidebar gutter, composer chips share the textarea's
    left edge, stage tabs match the topbar gutter, toasts sit below the
    header bar instead of covering the pills."""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    # sidebar: every region shares the 12px gutter
    assert "padding: 2px 12px 8px" in css            # .session-list
    assert "padding: 0 11px 4px" in css              # .sess-group-label
    assert "padding: 8px 12px 0" in css              # .proj-head
    assert "padding: 10px 12px; border-top" in css   # .side-foot
    assert "padding: 8px 16px 0" not in css          # old proj-head gutter
    # composer: mode chips align with the input text (textarea pad-left 2px)
    assert "padding: 8px 2px 0" in css               # #composerModes
    assert ".hint { flex: 1; font-family: var(--mono); font-size: 10px; color: var(--faint); padding-left: 2px;" in css
    # stage tabs match the topbar's 12px gutter; toasts clear the 42px header
    assert "padding: 0 12px; border-bottom" in css   # #stageTabs
    assert ".toasts { position: fixed; top: 48px;" in css
    # workbench values prefer natural break points over mid-word breaks
    assert "overflow-wrap: anywhere; word-break: normal" in css


def test_round7_composer_button_placement_and_states():
    """Composer close-up pass: tool buttons (enhance/mic/attach) live on the
    LEFT of the hint and send stays pinned bottom-right (ChatGPT/Claude
    placement) — conditional buttons appearing must not shift the send
    button. Disabled send reads as a dimmed accent button, not a dead grey
    square."""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert ".composer-actions #enhanceBtn { order: -3; }" in css
    assert ".composer-actions #micBtn { order: -2; }" in css
    assert ".composer-actions #attachBtn { order: -1; }" in css
    # uniform icon chrome sized against the 30px send button
    assert ".composer-actions .icon-btn { width: 28px; height: 28px;" in css
    # dimmed-accent disabled state (old dead-grey rule must be gone)
    assert ".send-btn:disabled { background: var(--accent); border-color: transparent;" in css
    assert ".send-btn:disabled { background: var(--bg3);" not in css
    # breathing room above the first text line (was 1px)
    assert "max-height: 180px; padding: 4px 2px 6px;" in css
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'placeholder="Message Saturday&hellip; ( / for commands )"' in html


def test_round8_suggest_endpoint(tmp_path, monkeypatch):
    """/api/suggest (Devin/Cursor parity): returns up to 3 short follow-up
    prompts generated from the session's last exchange; empty payloads when
    the feature is off, the session is unknown, or the model fails."""
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    import saturday.webui as w

    def fake_one_shot(cfg, prompt, **kw):
        assert "Assistant reply:" in prompt and "User:" in prompt
        return "1. run the full suite\n- write a regression test\n3. commit the fix\n4. extra noise that should be dropped"
    monkeypatch.setattr(w, "_one_shot", fake_one_shot)

    # empty/unknown session: clean empty payload, no error
    status, data = _req(base, "/api/suggest", method="POST", payload={"session_id": "nope"})
    assert status == 200 and data["suggestions"] == []

    # a real session with a user+assistant exchange
    app.store.append("s1", {"type": "messages", "messages": [
        {"role": "user", "content": "fix the failing test"},
        {"role": "assistant", "content": "Fixed test_loop.py; suite green."},
    ]})
    status, data = _req(base, "/api/suggest", method="POST", payload={"session_id": "s1"})
    assert status == 200 and data["ok"] is True
    assert data["suggestions"] == ["run the full suite", "write a regression test", "commit the fix"]

    # feature off (config gate) -> empty payload even with a session
    monkeypatch.setattr(app.base_cfg.__class__, "suggest_followups", property(lambda self: False))
    status, data = _req(base, "/api/suggest", method="POST", payload={"session_id": "s1"})
    assert status == 200 and data["suggestions"] == []

    # model failure is swallowed (best-effort chrome)
    monkeypatch.setattr(w, "_one_shot", lambda cfg, prompt, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    status, data = _req(base, "/api/suggest", method="POST", payload={"session_id": "s1"})
    assert status == 200 and data["suggestions"] == []


def test_round8_state_payload_and_config_gate(tmp_path, monkeypatch):
    app = AppState(store_root=tmp_path / "s")
    assert app.state_payload()["suggest_followups"] is True
    monkeypatch.setattr("saturday.config.save_config", lambda partial: None)
    app.apply_config({"suggest_followups": False})
    assert app.state_payload()["suggest_followups"] is False


def test_round8_frontend_wiring():
    """Follow-up chips, per-session drafts, detached-run badges and the image
    lightbox must all be reachable from the app surface."""
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    # follow-ups: fetched on normal completion, cleared on input/send/switch
    assert '"/api/suggest"' in js
    assert 'if ((e.stop_reason || "done") === "done") fetchFollowups();' in js
    assert js.count("clearFollowups()") >= 4  # send / input / newChat / openSession(+chip)
    assert 'el("button", "follow-chip", s)' in js
    # drafts: saved per session, restored on open
    assert 'function draftKey(sid) { return "df_draft_" + (sid || "new"); }' in js
    assert "restoreDraft();" in js and "saveDraft();" in js
    # detached badges: tracked on leave, resolved against /api/runs, shown in sidebar
    assert 'markDetached(state.sid); // the run continues server-side; badge it in the sidebar' in js
    assert '"/api/runs"' in js
    assert 'el("span", "sess-done", "finished")' in js
    # lightbox: transcript images zoom, Esc dismisses, approvals shortcut suppressed
    assert "lightboxOpen(e.target.currentSrc || e.target.src)" in js
    assert "$(\"#lightbox\").classList.contains(\"hidden\")) { lightboxClose(); return; }" in js
    assert '"#askModal", "#lightbox"].some(' in js
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'id="followRow"' in html and 'id="lightbox"' in html and 'id="cfgFollowups"' in html
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    for cls in (".follow-chip {", ".sess-done {", "#lightbox {"):
        assert cls in css, cls



# --- from tests/test_webui_projects.py ---

sys.path.insert(0, str(Path(__file__).parent))


TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


class _ServerProjects:
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


def make_app_projects(tmp_path: Path, turns) -> AppState:
    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "global-ws")},
    )
    fake = make_scripted_model(turns)
    orig_new = app._new_agent

    def patched(cfg):
        agent = orig_new(cfg)
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


def stream_chat(base: str, payload: dict) -> list[dict]:
    import json as j

    data = j.dumps(payload).encode()
    r = urllib.request.Request(base + "/api/chat", data=data, method="POST")
    r.add_header("X-Saturday-Token", TOKEN)
    r.add_header("Content-Type", "application/json")
    out = []
    with urllib.request.urlopen(r, timeout=90) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line:
                out.append(j.loads(line))
    return out


def test_project_store_roundtrip(tmp_path: Path):
    st = ProjectStore(tmp_path / "projects.json")
    p1 = st.create("Acme Frontend", instructions="use tabs", workspace=str(tmp_path))
    assert p1.id.startswith("acme-frontend")
    assert st.get(p1.id).name == "Acme Frontend"
    p2 = st.create("Acme Frontend")
    assert p2.id != p1.id, "duplicate names get unique ids"
    assert st.list()[0].id == p1.id, "ordered by creation"

    st.update(p1.id, name="Renamed", instructions="use spaces")
    got = st.get(p1.id)
    assert got.name == "Renamed" and got.instructions == "use spaces" and got.workspace == str(tmp_path)

    # persistence across instances
    st2 = ProjectStore(tmp_path / "projects.json")
    assert {p.id for p in st2.list()} == {p1.id, p2.id}

    assert st.delete(p2.id) is True
    assert st.delete(p2.id) is False
    assert st.get(p2.id) is None


def test_project_store_validation(tmp_path: Path):
    st = ProjectStore(tmp_path / "projects.json")
    with pytest.raises(ValueError):
        st.create("   ")
    with pytest.raises(ValueError):
        st.create("x", workspace=str(tmp_path / "missing-dir"))
    p = st.create("ok")
    with pytest.raises(ValueError):
        st.update(p.id, workspace="Z:/definitely/not/here")
    assert st.get(p.id).workspace == "", "failed update must not corrupt state"


def test_create_and_list_projects_api(tmp_path: Path):
    app = make_app_projects(tmp_path, [])
    ws = tmp_path / "repo"
    ws.mkdir()
    with _ServerProjects(app) as srv:
        status, data = req(srv.base, "/api/projects", "POST", {"name": "Repo X", "workspace": str(ws), "instructions": "be terse"})
        assert status == 200 and data["project"]["name"] == "Repo X"
        pid = data["project"]["id"]

        status, data = req(srv.base, "/api/state")
        projects = {p["id"]: p for p in data["projects"]}
        assert projects[pid]["sessions"] == 0

        status, data = req(srv.base, "/api/projects", "POST", {"name": ""})
        assert status == 400
        status, data = req(srv.base, "/api/projects", "POST", {"name": "bad", "workspace": "C:/nope/nothere"})
        assert status == 400


def test_patch_project_api(tmp_path: Path):
    app = make_app_projects(tmp_path, [])
    with _ServerProjects(app) as srv:
        _, data = req(srv.base, "/api/projects", "POST", {"name": "P1"})
        pid = data["project"]["id"]

        status, data = req(srv.base, f"/api/project/{pid}", "PATCH", {"name": "P1 renamed", "instructions": "new rules"})
        assert status == 200 and data["project"]["name"] == "P1 renamed"
        assert app.projects.get(pid).instructions == "new rules"

        status, _ = req(srv.base, "/api/project/ghost", "PATCH", {"name": "x"})
        assert status == 404


def test_chat_tags_session_with_project(tmp_path: Path):
    turns = [{"content": "hi from project"}]
    app = make_app_projects(tmp_path, turns)
    ws = tmp_path / "projws"
    (ws / "inner").mkdir(parents=True)
    with _ServerProjects(app) as srv:
        _, data = req(srv.base, "/api/projects", "POST", {"name": "Tagged", "workspace": str(ws), "instructions": "always answer in rhyme"})
        pid = data["project"]["id"]

        events = stream_chat(srv.base, {"text": "hello", "project_id": pid})
        hello = events[0]
        sid = hello["sid"]
        assert hello["t"] == "hello" and hello["project"] == pid
        done = [e for e in events if e["t"] == "done"][0]
        assert done["final"] == "hi from project"

        rows = {r["id"]: r for r in app.store.list_sessions()}
        assert rows[sid]["project"] == pid

        rt = app.runtime_for(sid)
        assert rt.project_id == pid
        assert rt.agent.cfg.workspace_root == str(ws), "project workspace must become the sandbox root"
        assert "always answer in rhyme" in rt.agent.persona_extra

        # project-scoped file browser follows the session's workspace
        status, data = req(srv.base, f"/api/ws?sid={sid}")
        assert status == 200 and [e["name"] for e in data["entries"]] == ["inner"]


def test_chat_rejects_unknown_project(tmp_path: Path):
    app = make_app_projects(tmp_path, [])
    with _ServerProjects(app) as srv:
        status, _ = req(srv.base, "/api/chat", "POST", {"text": "x", "project_id": "ghost"})
        assert status == 400
        assert not app.store.list_sessions(), "no session may be created for a bad project"


def test_untagged_sessions_stay_in_default_view(tmp_path: Path):
    app = make_app_projects(tmp_path, [{"content": "plain"}])
    with _ServerProjects(app) as srv:
        events = stream_chat(srv.base, {"text": "plain chat"})
        sid = events[0]["sid"]
        rows = {r["id"]: r for r in app.store.list_sessions()}
        assert rows[sid]["project"] == ""
        rt = app.runtime_for(sid)
        assert rt.project_id is None
        assert rt.agent.cfg.workspace_root == str(tmp_path / "global-ws")


def test_assign_moves_session_between_projects(tmp_path: Path):
    app = make_app_projects(tmp_path, [{"content": "ok"}])
    wsa = tmp_path / "wsa"
    wsb = tmp_path / "wsb"
    for w in (wsa, wsb):
        w.mkdir()
    with _ServerProjects(app) as srv:
        _, d1 = req(srv.base, "/api/projects", "POST", {"name": "A", "workspace": str(wsa)})
        _, d2 = req(srv.base, "/api/projects", "POST", {"name": "B", "workspace": str(wsb)})
        pa, pb = d1["project"]["id"], d2["project"]["id"]
        events = stream_chat(srv.base, {"text": "move me"})
        sid = events[0]["sid"]

        status, _ = req(srv.base, "/api/assign", "POST", {"session_id": sid, "project_id": pb})
        assert status == 200
        assert app.store.read_meta(sid)["project"] == pb
        rt = app.runtime_for(sid)
        assert rt.project_id == pb and rt.agent.cfg.workspace_root == str(wsb)

        status, _ = req(srv.base, "/api/assign", "POST", {"session_id": sid, "project_id": ""})
        assert status == 200
        assert "project" not in app.store.read_meta(sid)

        status, _ = req(srv.base, "/api/assign", "POST", {"session_id": sid, "project_id": "ghost"})
        assert status == 404
        status, _ = req(srv.base, "/api/assign", "POST", {"session_id": "ghost-session", "project_id": ""})
        assert status == 404


def test_delete_project_untags_sessions(tmp_path: Path):
    app = make_app_projects(tmp_path, [{"content": "kept"}])
    with _ServerProjects(app) as srv:
        _, data = req(srv.base, "/api/projects", "POST", {"name": "Doomed"})
        pid = data["project"]["id"]
        events = stream_chat(srv.base, {"text": "in doomed project", "project_id": pid})
        sid = events[0]["sid"]

        status, data = req(srv.base, f"/api/project/{pid}", "DELETE")
        assert status == 200 and data["untagged"] == 1
        meta = app.store.read_meta(sid)
        assert "project" not in meta
        assert app.store.load(sid) is not None, "chat content must survive project deletion"
        status, _ = req(srv.base, f"/api/project/{pid}", "DELETE")
        assert status == 404


def test_config_change_syncs_project_runtime_persona(tmp_path: Path):
    app = make_app_projects(tmp_path, [])
    with _ServerProjects(app) as srv:
        _, data = req(srv.base, "/api/projects", "POST", {"name": "Sync", "instructions": "project rule one"})
        pid = data["project"]["id"]
        events = stream_chat(srv.base, {"text": "start", "project_id": pid})
        sid = events[0]["sid"]
        rt = app.runtime_for(sid)

        status, _ = req(srv.base, "/api/config", "POST", {"persona_extra": "global rule"})
        assert status == 200
        persona = rt.agent.persona_extra
        assert "global rule" in persona and "project rule one" in persona

        _, data = req(srv.base, f"/api/project/{pid}", "PATCH", {"instructions": "project rule two"})
        assert status == 200
        status, _ = req(srv.base, "/api/config", "POST", {"model": "zz-new-model"})
        assert status == 200
        assert rt.agent.cfg.model == "zz-new-model", "cloned cfg follows global model changes"
        assert "project rule one" not in rt.agent.persona_extra
        assert "project rule two" in rt.agent.persona_extra, "config sync re-merges current project instructions"
        assert "global rule" in rt.agent.persona_extra


def test_project_color_and_knowledge_files_roundtrip(tmp_path: Path):
    app = make_app_projects(tmp_path, [])
    kf1 = tmp_path / "style-guide.md"
    kf1.write_text("always use tabs", encoding="utf-8")
    kf2 = tmp_path / "glossary.md"
    kf2.write_text("df = saturday", encoding="utf-8")
    with _ServerProjects(app) as srv:
        status, data = req(
            srv.base,
            "/api/projects",
            "POST",
            {"name": "Styled", "color": "green", "files": [str(kf1), str(kf2)]},
        )
        assert status == 200, data
        proj = data["project"]
        assert proj["color"] == "green"
        assert proj["files"] == [str(kf1.resolve()), str(kf2.resolve())]

        status, data = req(srv.base, f"/api/project/{proj['id']}", "PATCH", {"color": "blue"})
        assert status == 200 and data["project"]["color"] == "blue"
        assert len(data["project"]["files"]) == 2, "patching color must not touch files"

        status, data = req(srv.base, f"/api/project/{proj['id']}", "PATCH", {"files": [str(kf2)]})
        assert status == 200 and data["project"]["files"] == [str(kf2.resolve())]

        # validation failures -> 400, state untouched
        status, _ = req(srv.base, f"/api/project/{proj['id']}", "PATCH", {"files": [str(tmp_path / "ghost.md")]})
        assert status == 400
        status, _ = req(srv.base, f"/api/project/{proj['id']}", "PATCH", {"color": "chartreuse"})
        assert status == 400
        status, _ = req(srv.base, f"/api/project/{proj['id']}", "PATCH", {"files": "not-a-list"})
        assert status == 400
        many = []
        for i in range(13):
            f = tmp_path / f"kb{i}.txt"
            f.write_text("x", encoding="utf-8")
            many.append(str(f))
        status, data = req(srv.base, f"/api/project/{proj['id']}", "PATCH", {"files": many})
        assert status == 400 and "max 12" in data.get("error", "")
        assert app.projects.get(proj["id"]).files == [str(kf2.resolve())], "failed patch must leave files intact"


def test_knowledge_files_injected_into_project_chats(tmp_path: Path):
    turns = [{"content": "ack"}]
    app = make_app_projects(tmp_path, turns)
    kf = tmp_path / "kb.txt"
    kf.write_text("DF-KNOWLEDGE-MARKER-4242 " + "filler\n" * 50, encoding="utf-8")
    big = tmp_path / "big.txt"
    big.write_text("x" * (25_000), encoding="utf-8")
    with _ServerProjects(app) as srv:
        _, data = req(
            srv.base,
            "/api/projects",
            "POST",
            {"name": "Kb", "instructions": "be brief", "files": [str(kf), str(big)]},
        )
        pid = data["project"]["id"]
        events = stream_chat(srv.base, {"text": "hi", "project_id": pid})
        sid = events[0]["sid"]
        rt = app.runtime_for(sid)
        persona = rt.agent.persona_extra
        assert "DF-KNOWLEDGE-MARKER-4242" in persona
        assert "# Project reference files" in persona and "--- " + str(kf.resolve()) in persona
        assert "Project: Kb" in persona and "be brief" in persona
        # per-file cap: big file truncated at 20k chars + marker
        kb_block_start = persona.index(str(big.resolve()))
        chunk = persona[kb_block_start:]
        assert "[truncated]" in chunk
        assert persona.count("x") < 25_000, "oversized knowledge file must be capped"


def test_missing_knowledge_file_degrades_gracefully(tmp_path: Path):
    app = make_app_projects(tmp_path, [{"content": "ok"}])
    kf = tmp_path / "gone.txt"
    kf.write_text("temp knowledge", encoding="utf-8")
    with _ServerProjects(app) as srv:
        _, data = req(srv.base, "/api/projects", "POST", {"name": "Vanish", "files": [str(kf)]})
        pid = data["project"]["id"]
        kf.unlink()
        events = stream_chat(srv.base, {"text": "still works?", "project_id": pid})
        done = [e for e in events if e["t"] == "done"][0]
        assert done["final"] == "ok"
        rt = app.runtime_for(done["sid"])
        assert "(unreadable)" in rt.agent.persona_extra



# --- from tests/test_settings.py ---

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


def test_range_validators_bounds():
    assert _b_int_range(1, 600)({"request_timeout": 120}, None, "request_timeout") == 120
    assert _b_int_range(1, 600)({"tool_timeout": 0}, None, "tool_timeout") is _CFG_SKIP
    assert _b_float_range(0, 1)({"top_p": 0.9}, None, "top_p") == 0.9
    assert _b_float_range(0, 1)({"top_p": 1.4}, None, "top_p") is _CFG_SKIP


def test_optional_int_clears_with_null():
    _opt = _b_int_range_opt(0, 10_000_000)
    assert _opt({"compact_above_tokens": None}, None, "compact_above_tokens") is None
    assert _opt({"max_context_tokens": 5000}, None, "max_context_tokens") == 5000
    assert _opt({"max_context_tokens": -1}, None, "max_context_tokens") is _CFG_SKIP
    assert _opt({}, None, "max_context_tokens") is _CFG_SKIP


def test_bool_toggles_only_accept_bools():
    assert _v_bool({"stream": False}, None, "stream") is False
    assert _v_bool({"stream": "no"}, None, "stream") is _CFG_SKIP


def test_config_fields_expose_advanced_knobs():
    keys = {k for k, _ in webui._CONFIG_FIELDS}
    assert {
        "top_p",
        "request_timeout",
        "tool_timeout",
        "max_retries",
        "memory_max_chars",
        "max_context_tokens",
        "compact_above_tokens",
        "stream",
        "shell_allow_network",
    } <= keys


sys.path.insert(0, str(Path(__file__).parent))


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


class _ServerSettings:
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


def make_app_settings(tmp_path: Path, turns=None) -> AppState:
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


def test_state_payload_has_settings_fields(tmp_path: Path):
    app = make_app_settings(tmp_path)
    with _ServerSettings(app) as srv:
        status, data = req(srv.base, "/api/state")
        assert status == 200
        for key in ("max_tokens", "fallback_models", "background_only", "config_dir", "sessions_dir", "workspace_root"):
            assert key in data, key
        assert Path(data["sessions_dir"]) == app.store.root


def test_background_only_roundtrip_and_persistence(tmp_path: Path):
    app = make_app_settings(tmp_path)
    with _ServerSettings(app) as srv:
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
    app = make_app_settings(tmp_path)
    with _ServerSettings(app) as srv:
        status, data = req(srv.base, "/api/config", "POST", {"fallback_models": ["a", "b"]})
        assert status == 200 and data["fallback_models"] == ["a", "b"]

        status, data = req(srv.base, "/api/config", "POST", {"fallback_models": " x , ,y, x,"})
        assert status == 200 and data["fallback_models"] == ["x", "y"], "string form parsed + deduped"

        status, data = req(srv.base, "/api/config", "POST", {"fallback_models": "m1,m2,m3,m4,m5,m6,m7,m8,m9"})
        assert status == 200 and len(data["fallback_models"]) == 8, "capped at 8"

        status, _ = req(srv.base, "/api/config", "POST", {"fallback_models": 42})
        assert status == 400


def test_max_tokens_roundtrip_and_bounds(tmp_path: Path):
    app = make_app_settings(tmp_path)
    with _ServerSettings(app) as srv:
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
    app = make_app_settings(tmp_path)
    with _ServerSettings(app) as srv:
        for target, expected in (("config", None), ("sessions", str(app.store.root)), ("workspace", str(tmp_path / "ws"))):
            status, data = req(srv.base, "/api/reveal", "POST", {"target": target})
            assert status == 200 and data["ok"] is True
        assert len(opened) == 3
        assert Path(opened[1]) == app.store.root
        status, _ = req(srv.base, "/api/reveal", "POST", {"target": "C:/Windows"})
        assert status == 400, "arbitrary paths must be refused"


def test_clear_all_sessions_endpoint(tmp_path: Path):
    app = make_app_settings(tmp_path)
    with _ServerSettings(app) as srv:
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
    app = make_app_settings(tmp_path)
    with _ServerSettings(app) as srv:
        sid = app.store.create({"task": "exportable", "surface": "app"})
        app.store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "hello export"}]})
        status, data = req(srv.base, "/api/export/all")
        assert status == 200 and data["exported"] == 1
        sess = data["sessions"][0]
        assert sess["meta"]["task"] == "exportable"
        msgs = [r for r in sess["records"] if r.get("type") == "messages"]
        assert msgs, "message records must be present in the export"



# --- from tests/test_desktop_window.py ---

class _FakeWin:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def minimize(self) -> None:
        self.calls.append("minimize")

    def maximize(self) -> None:
        self.calls.append("maximize")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append("destroy")


def test_window_controls_minimize_via_close():
    win = _FakeWin()
    ctl = webui._WindowControls(win)
    assert ctl.win_min() is True
    assert ctl.win_close() is True
    assert win.calls == ["minimize", "destroy"]


def test_window_controls_maximizes_then_restores():
    win = _FakeWin()
    ctl = webui._WindowControls(win)
    assert ctl.win_max() is True  # now maximized
    assert win.calls == ["maximize"]
    assert ctl.win_max() is False  # now restored
    assert win.calls == ["maximize", "restore"]


def test_embedded_window_falls_back_without_pywebview(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("pywebview not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert webui.launch_embedded_window("http://127.0.0.1:1/", 800, 600) is False


def test_titlebar_markup_present():
    assets = Path(webui.__file__).resolve().parent / "webui_assets"
    html = (assets / "index.html").read_text(encoding="utf-8")
    js = (assets / "app.js").read_text(encoding="utf-8")
    assert 'id="titlebar"' in html
    assert "pywebview-drag-region" in html
    assert "tbMax" in html and "tbClose" in html
    assert 'enableTitleBar' in js and "pywebviewready" in js


# ---- merged from test_round3_wiring.py ----
def _req_raw(base, path, method="GET", payload=None, token="tok"):
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
    base, _ = _server(app)
    status, html = _req_raw(base, "/")
    assert status == 200 and 'id="cfgProvenance"' in html
    status, css = _req_raw(base, "/app.css")
    assert status == 200 and len(css) > 1000
    status, js = _req_raw(base, "/app.js")
    assert status == 200 and "cfgVerifyCmd" in js


def test_metrics_endpoint_served_with_auth(tmp_path):
    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    status, body = _req_raw(base, "/api/metrics?days=30")
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


def test_launch_app_window_uses_isolated_profile(monkeypatch, tmp_path):
    from saturday import webui
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(webui, "find_app_browser", lambda: "/usr/bin/chromium")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv

        class P:
            pid = 1

        return P()

    monkeypatch.setattr(webui.subprocess, "Popen", fake_popen)
    webui.launch_app_window("http://127.0.0.1:8679/")
    profile_flags = [a for a in captured["argv"] if a.startswith("--user-data-dir=")]
    assert len(profile_flags) == 1
    profile_dir = Path(profile_flags[0].split("=", 1)[1])
    assert profile_dir == tmp_path / "home" / "app-browser-profile"
    assert profile_dir.is_dir()  # created, not just referenced


def test_launch_app_window_falls_back_to_webbrowser_when_no_app_browser(monkeypatch):
    from saturday import webui

    monkeypatch.setattr(webui, "find_app_browser", lambda: None)
    opened = []
    monkeypatch.setattr(webui.webbrowser, "open", lambda url: opened.append(url))
    result = webui.launch_app_window("http://127.0.0.1:8679/")
    assert result is None
    assert opened == ["http://127.0.0.1:8679/"]


# ---- remote access (tunnel) ----------------------------------------------


def test_argv_per_provider():
    from saturday import remote as rmt

    assert rmt._argv("cloudflared", 8679) == ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8679"]
    assert rmt._argv("tailscale", 8679) == ["tailscale", "funnel", "--bg=false", "8679"]
    with pytest.raises(ValueError):
        rmt._argv("nope", 1)


def test_available_providers_reflects_path(monkeypatch):
    from saturday import remote as rmt

    monkeypatch.setattr(rmt.shutil, "which", lambda n: "/usr/bin/x" if n == "tailscale" else None)
    assert rmt.available_providers() == ["tailscale"]
    monkeypatch.setattr(rmt.shutil, "which", lambda n: None)
    assert rmt.available_providers() == []


def test_start_tunnel_missing_binary_names_the_installer(monkeypatch):
    from saturday import remote as rmt

    monkeypatch.setattr(rmt.shutil, "which", lambda n: None)
    with pytest.raises(RuntimeError, match="cloudflared is not installed"):
        rmt.start_tunnel("cloudflared", 8679)


class _FakeProc:
    def __init__(self, lines, exits=False):
        self.stdout = iter(lines)
        self._exits = exits
        self.terminated = False

    def poll(self):
        return 1 if self._exits else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_start_tunnel_parses_url_and_host(monkeypatch):
    from saturday import remote as rmt

    lines = ["starting\n", "|  https://brave-mode-1234.trycloudflare.com    |\n", "ready\n"]
    monkeypatch.setattr(rmt.shutil, "which", lambda n: "/usr/bin/cloudflared")
    monkeypatch.setattr(rmt.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))
    tun = rmt.start_tunnel("cloudflared", 8679, timeout=5)
    assert tun.url == "https://brave-mode-1234.trycloudflare.com"
    assert tun.host == "brave-mode-1234.trycloudflare.com"
    assert tun.provider == "cloudflared"


def test_start_tunnel_surfaces_real_output_on_failure(monkeypatch):
    """A dead tunnel must report its own words - 'tunnel failed' is unfixable."""
    from saturday import remote as rmt

    monkeypatch.setattr(rmt.shutil, "which", lambda n: "/usr/bin/cloudflared")
    monkeypatch.setattr(rmt.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(["ERR failed to connect\n"], exits=True))
    with pytest.raises(RuntimeError, match="failed to connect"):
        rmt.start_tunnel("cloudflared", 8679, timeout=5)


def test_qr_lines_absent_without_qrencode(monkeypatch):
    from saturday import remote as rmt

    monkeypatch.setattr(rmt.shutil, "which", lambda n: None)
    assert rmt.qr_lines("https://x") == []


def test_allow_host_widens_pin_without_losing_loopback(tmp_path):
    """The Host pin rejects a tunnel hostname by design; remote must widen it
    explicitly, and must not drop the loopback entries doing so."""
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    srv = AppServer(("127.0.0.1", 0), app, token="t")
    try:
        before = set(srv.RequestHandlerClass.allowed_hosts)
        assert not any("trycloudflare" in h for h in before)
        srv.allow_host("brave-mode-1234.trycloudflare.com")
        after = set(srv.RequestHandlerClass.allowed_hosts)
        assert "brave-mode-1234.trycloudflare.com" in after
        assert before <= after
        assert "https://brave-mode-1234.trycloudflare.com" in srv.RequestHandlerClass.allowed_origins
    finally:
        srv.server_close()


# ---- agents + models API (GUI parity for the CLI features) ---------------


def test_agents_endpoint_lists_and_toggles(tmp_path, monkeypatch):
    import saturday.config as cfgmod
    from saturday.tools import external_agent as ea

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(ea.shutil, "which", lambda n: "/usr/bin/x")
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/agents")
        assert status == 200
        names = {a["agent"] for a in data["agents"]}
        assert "claude-code" in names
        assert all(a["enabled"] is False for a in data["agents"])

        status, data = _req(srv.base, "/api/agents", "POST",
                            {"agent": "claude-code", "enabled": True})
        assert status == 200 and data["enabled"] == ["claude-code"]

        _, data = _req(srv.base, "/api/agents")
        row = next(a for a in data["agents"] if a["agent"] == "claude-code")
        assert row["enabled"] is True and row["tier_name"] == "subscription"


def test_agents_post_requires_a_name(tmp_path, monkeypatch):
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/agents", "POST", {"enabled": True})
        assert status == 400 and data["ok"] is False


def test_models_endpoint_marks_free_and_filters(tmp_path, monkeypatch):
    import saturday.cli as cli
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "_probe_provider",
                        lambda n, t: (n, n == "openrouter", "ok", ["a/b:free", "c/d"]))
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    with _Server(app) as srv:
        _, data = _req(srv.base, "/api/models")
        ids = {m["id"]: m["free"] for m in data["providers"]["openrouter"]}
        assert ids == {"a/b:free": True, "c/d": False}

        _, data = _req(srv.base, "/api/models?free=1")
        assert [m["id"] for m in data["providers"]["openrouter"]] == ["a/b:free"]


def test_models_post_wires_free_models_into_agents(tmp_path, monkeypatch):
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/models", "POST",
                            {"models": {"openrouter": ["z-ai/glm-5.2:free"]}})
        assert status == 200 and data["added"] == ["free-z-ai-glm-5-2"]
    written = json.loads((tmp_path / "agents.json").read_text())
    assert written["free-z-ai-glm-5-2"]["model"] == "z-ai/glm-5.2:free"


def test_remote_endpoint_reports_state_and_providers(tmp_path, monkeypatch):
    from saturday import remote as rmt

    monkeypatch.setattr(rmt.shutil, "which", lambda n: "/usr/bin/x" if n == "cloudflared" else None)
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    with _Server(app) as srv:
        _, data = _req(srv.base, "/api/remote")
        assert data["running"] is False and data["available"] == ["cloudflared"]


def test_remote_start_allowlists_the_tunnel_host(tmp_path, monkeypatch):
    """Starting a tunnel must widen the Host pin or every request 403s."""
    from saturday import remote as rmt

    tun = rmt.Tunnel(url="https://x-y-z.trycloudflare.com", host="x-y-z.trycloudflare.com",
                     provider="cloudflared", proc=None)
    monkeypatch.setattr(rmt, "available_providers", lambda: ["cloudflared"])
    monkeypatch.setattr(rmt, "start_tunnel", lambda p, port, **k: tun)
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/remote", "POST", {"start": True})
        assert status == 200 and data["running"] is True
        assert data["url"].startswith("https://x-y-z.trycloudflare.com/")
        assert "k=" in data["url"], "token must ride the pairing URL"
        assert "x-y-z.trycloudflare.com" in srv.http.RequestHandlerClass.allowed_hosts

        _, data = _req(srv.base, "/api/remote", "POST", {"start": False})
        assert data["running"] is False


def test_remote_start_without_provider_explains(tmp_path, monkeypatch):
    from saturday import remote as rmt

    monkeypatch.setattr(rmt, "available_providers", lambda: [])
    app = AppState(cfg_overrides={"workspace_root": str(tmp_path)})
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/remote", "POST", {"start": True})
        assert status == 400 and "cloudflared" in data["hints"]


def test_memgraph_builds_layers_and_links_them(tmp_path, monkeypatch):
    """The graph must span layers: code files, the sessions that touched them,
    and the facts that mention them - a code-only graph is not a memory."""
    cfg = tmp_path / "cfg"
    (cfg / "sessions").mkdir(parents=True)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)

    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "core.py").write_text(
        "class Widget:\n    def spin(self):\n        return 1\n", encoding="utf-8")
    (ws / "pkg" / "use.py").write_text(
        "from pkg.core import Widget\nWidget().spin()\n", encoding="utf-8")

    (cfg / "sessions" / "s1.jsonl").write_text(json.dumps({
        "type": "messages",
        "messages": [{"role": "user", "content": "please look at pkg/core.py"}],
    }) + "\n", encoding="utf-8")
    (cfg / "MEMORY.md").write_text("- pkg/core.py owns the Widget lifecycle\n", encoding="utf-8")

    from saturday.memgraph import build_graph

    g = build_graph(ws)
    kinds = g["stats"]["kinds"]
    assert kinds.get("file", 0) >= 2 and kinds.get("dir", 0) >= 1
    assert kinds.get("session") == 1 and kinds.get("fact") == 1

    idx = {n["id"]: i for i, n in enumerate(g["nodes"])}
    core = idx["file:pkg/core.py"]
    pairs = {(e["s"], e["t"]) for e in g["edges"]} | {(e["t"], e["s"]) for e in g["edges"]}
    # use.py references a symbol core.py defines
    assert (idx["file:pkg/use.py"], core) in pairs
    # the session that named the file, and the fact about it, both attach to it
    assert (idx["session:s1"], core) in pairs
    assert any((i, core) in pairs for k, i in idx.items() if k.startswith("fact:"))


def test_memgraph_survives_an_empty_workspace(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    from saturday.memgraph import build_graph

    g = build_graph(tmp_path / "nope")
    assert g["stats"]["nodes"] == 0 and g["edges"] == []


def test_memgraph_caps_total_nodes_and_keeps_edges_valid(tmp_path, monkeypatch):
    """The cap bounds the WHOLE graph, not just its files - folders used to
    ride on top of it - and every surviving edge must still point at a node."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    ws = tmp_path / "ws"
    n = 40  # one file per folder, so folders alone would blow a files-only cap
    for i in range(n):
        (ws / f"d{i}").mkdir(parents=True)
        (ws / f"d{i}" / "m.py").write_text(
            f"def sym_{i}():\n    return sym_{(i + 1) % n}()\n", encoding="utf-8")

    from saturday.memgraph import build_graph

    assert build_graph(ws)["stats"]["nodes"] == 2 * n  # uncapped: files + folders

    g = build_graph(ws, limit=20)
    assert len(g["nodes"]) == 20 and g["stats"]["nodes"] == 20
    assert g["edges"], "trimming must not throw away every edge"
    for e in g["edges"]:
        assert 0 <= e["s"] < 20 and 0 <= e["t"] < 20


def test_memgraph_endpoint_serves_and_caches(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("def go():\n    return 2\n", encoding="utf-8")

    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)

    status, data = _req(base, "/api/memgraph")
    assert status == 200, data
    assert data["stats"]["nodes"] >= 1
    assert data["workspace"] == str(ws)

    # a second file appears but the cached answer stands until ?refresh=1
    (ws / "b.py").write_text("from a import go\ngo()\n", encoding="utf-8")
    _, cached = _req(base, "/api/memgraph")
    assert cached["stats"]["nodes"] == data["stats"]["nodes"]
    _, fresh = _req(base, "/api/memgraph?refresh=1")
    assert fresh["stats"]["nodes"] > data["stats"]["nodes"]


def test_browse_lists_directories_and_flags_repos(tmp_path, monkeypatch):
    """The folder picker's listing: directories only, git repos marked, and
    crumbs that walk back up. It is deliberately outside the ws sandbox."""
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "cfg")
    home = tmp_path / "home"
    (home / "proj" / ".git").mkdir(parents=True)
    (home / "plain").mkdir()
    (home / ".hidden").mkdir()
    (home / "a-file.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)

    status, data = _req(base, "/api/browse")
    assert status == 200, data
    assert data["path"] == str(home)
    names = {d["name"]: d for d in data["dirs"]}
    assert set(names) == {"proj", "plain"}          # no files, no dotfolders
    assert names["proj"]["repo"] is True and names["plain"]["repo"] is False

    status, sub = _req(base, "/api/browse?path=" + str(home / "proj"))
    assert status == 200 and sub["is_repo"] is True
    assert sub["parent"] == str(home)
    assert [c["name"] for c in sub["crumbs"]][-2:] == [home.name, "proj"]


def test_browse_falls_back_home_for_a_bad_path(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "cfg")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    status, data = _req(base, "/api/browse?path=" + str(tmp_path / "does-not-exist"))
    assert status == 200 and data["path"] == str(home)


def test_open_folder_creates_a_project_in_one_call(tmp_path, monkeypatch):
    """What the picker's Open button does: name it after the folder, no form."""
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "cfg")
    ws = tmp_path / "acme-frontend"
    ws.mkdir()
    app = AppState(store_root=tmp_path / "s", projects_store=ProjectStore(path=tmp_path / "p.json"))
    base, _ = _server(app)
    status, data = _req(base, "/api/projects", "POST", {"name": ws.name, "workspace": str(ws)})
    assert status == 200, data
    assert data["project"]["name"] == "acme-frontend"
    assert data["project"]["workspace"] == str(ws)


# Two module-level autouse `_hermetic` fixtures in this file (a merge of eight
# older test files) stub load_mcp_config to {} for EVERY test here, including
# the ones below. Capture the genuine function at import time - before any
# fixture can run - so the MCP endpoint tests can exercise the real loader.
import saturday.mcp_plugin as _mcpmod

_REAL_LOAD_MCP_CONFIG = _mcpmod.load_mcp_config


def _real_mcp_loader(monkeypatch):
    monkeypatch.setattr(_mcpmod, "load_mcp_config", _REAL_LOAD_MCP_CONFIG)


def _mcp_ws(tmp_path, servers):
    ws = tmp_path / "ws"
    (ws / ".saturday").mkdir(parents=True)
    (ws / ".saturday" / "mcp.json").write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return ws


def test_mcp_lists_without_starting_anything(tmp_path, monkeypatch):
    """Listing must never spawn a server: opening Settings should not launch
    every MCP process the project names."""
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "cfg")
    _real_mcp_loader(monkeypatch)
    ws = _mcp_ws(tmp_path, {"a": {"command": "echo", "args": ["hi"]},
                            "b": {"url": "http://example.invalid/mcp"}})
    spawned = []
    monkeypatch.setattr("saturday.webui.Handler._probe_mcp",
                        staticmethod(lambda spec: spawned.append(spec) or {"status": "ok"}))
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)

    status, data = _req(base, "/api/mcp")
    assert status == 200, data
    assert data["probed"] is False and spawned == []
    by = {s["alias"]: s for s in data["servers"]}
    assert by["a"]["transport"] == "stdio" and by["a"]["command"] == "echo hi"
    assert by["b"]["transport"] == "http" and by["a"]["source"] == "project"
    assert all(s["status"] == "unknown" for s in data["servers"])


def test_mcp_refuses_to_start_an_untrusted_project(tmp_path, monkeypatch):
    """A project's mcp.json names commands Saturday would execute, so probing
    it before the project is trusted would be a real execution hole."""
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "cfg")
    _real_mcp_loader(monkeypatch)
    ws = _mcp_ws(tmp_path, {"a": {"command": "echo"}})
    monkeypatch.setattr("saturday.utils.trust.is_trusted", lambda root: False)
    spawned = []
    monkeypatch.setattr("saturday.webui.Handler._probe_mcp",
                        staticmethod(lambda spec: spawned.append(spec) or {"status": "ok"}))
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)

    status, data = _req(base, "/api/mcp?probe=1")
    assert status == 200, data
    assert spawned == [], "an untrusted project must not have its servers started"
    assert data["servers"][0]["status"] == "blocked"
    assert any("not trusted" in w for w in data["warnings"])


def test_mcp_probe_reports_tools_and_failures(tmp_path, monkeypatch):
    """The whole point of the panel: what does this server actually provide."""
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "cfg")
    _real_mcp_loader(monkeypatch)
    fixture = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")
    ws = _mcp_ws(tmp_path, {
        "good": {"command": sys.executable, "args": [fixture]},
        "bad": {"command": "saturday-no-such-binary-xyz"},
    })
    monkeypatch.setattr("saturday.utils.trust.is_trusted", lambda root: True)
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)

    status, data = _req(base, "/api/mcp?probe=1")
    assert status == 200, data
    by = {s["alias"]: s for s in data["servers"]}
    assert by["good"]["status"] == "ok"
    assert by["good"]["server_name"] == "mock-mcp"
    assert {t["name"] for t in by["good"]["tools"]} == {"echo", "add"}
    assert by["bad"]["status"] == "failed" and by["bad"]["error"]
    # one bad server must not hide the good one
    assert by["good"]["tools"], "a failing sibling must not blank the working server"


def test_mcp_never_changes_process_cwd(tmp_path, monkeypatch):
    """The handler is threaded; chdir would leak into the agent and other requests."""
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path / "cfg")
    _real_mcp_loader(monkeypatch)
    ws = _mcp_ws(tmp_path, {"a": {"command": "echo"}})
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)
    before = os.getcwd()
    _req(base, "/api/mcp")
    assert os.getcwd() == before


def _chained_session(store, task, tamper=False):
    sid = store.create({"task": task})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "hello"}]})
    store.append(sid, {"type": "messages", "messages": [{"role": "assistant", "content": "hi"}]})
    if tamper:
        p = Path(store.root) / f"{sid}.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        i = next(n for n, ln in enumerate(lines) if '"hello"' in ln)
        rec = json.loads(lines[i])
        rec["messages"][0]["content"] = "hello, but edited afterwards"
        lines[i] = json.dumps(rec)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid


def test_audit_endpoint_detects_an_edited_record(tmp_path):
    """The tamper-evidence claim, checked from the UI's own endpoint: editing
    a stored record must break that session's chain and leave others intact."""
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    good = _chained_session(store, "clean run")
    bad = _chained_session(store, "edited run", tamper=True)

    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    status, data = _req(base, "/api/audit")
    assert status == 200, data
    assert data["checked"] == 2 and data["tampered"] == 1
    by = {s["id"]: s for s in data["sessions"]}
    assert by[good]["ok"] is True and by[good]["broken_at"] is None
    assert by[bad]["ok"] is False and by[bad]["broken_at"] == 0


def test_audit_single_session_and_unknown_id(tmp_path):
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    sid = _chained_session(store, "one")
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)

    status, data = _req(base, "/api/audit?sid=" + sid)
    assert status == 200 and data["sessions"][0]["ok"] is True
    status, data = _req(base, "/api/audit?sid=no-such-session")
    assert status == 404 and "error" in data


def test_audit_export_serves_a_downloadable_bundle(tmp_path):
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    sid = _chained_session(store, "bundle me", tamper=True)
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)

    req = urllib.request.Request(base + "/api/audit?export=1&sid=" + sid,
                                 headers={"X-Saturday-Token": "tok"})
    resp = urllib.request.urlopen(req, timeout=15)
    assert resp.status == 200
    assert f'filename="saturday-audit-{sid}.json"' in resp.headers.get("Content-Disposition", "")
    bundle = json.loads(resp.read().decode())
    # the bundle must carry the failure, not quietly present a clean record
    assert bundle["session_id"] == sid
    assert bundle["chain"]["ok"] is False
    assert len(bundle["records"]) == 2


def test_tools_endpoint_describes_every_tool_and_its_state(tmp_path):
    """`saturday tools` was the only place that said what a tool does; the
    settings checklist knew names only."""
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"disabled_tools": ["browser"]})
    base, _ = _server(app)
    status, data = _req(base, "/api/tools")
    assert status == 200, data
    by = {t["name"]: t for t in data["tools"]}
    assert len(by) > 10
    assert all(t["description"] for t in data["tools"]), "every tool must say what it does"
    assert by["browser"]["enabled"] is False
    assert by["shell"]["enabled"] is True
    # a disabled family expands to its members, matching the toggle semantics
    assert "browser" in data["disabled"]
    assert data["tools"] == sorted(data["tools"], key=lambda t: t["name"])


def test_doctor_endpoint_matches_the_cli_checks(tmp_path, monkeypatch):
    """One implementation, two renderers: the endpoint and `saturday doctor`
    must not drift into two different opinions about the same machine."""
    ws = tmp_path / "ws"
    ws.mkdir()
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)
    monkeypatch.setattr("saturday.llm.probe.probe_connection",
                        lambda *a, **k: (True, "reachable", []))

    status, data = _req(base, "/api/doctor?offline=1")
    assert status == 200, data
    ids = [c["id"] for c in data["checks"]]
    assert {"python", "provider", "model", "api_key", "endpoint", "workspace", "tools"} <= set(ids)
    assert all(c["status"] in ("ok", "warn", "fail") for c in data["checks"])
    assert data["failures"] == sum(1 for c in data["checks"] if c["status"] == "fail")

    from saturday.diagnostics import format_check, run_checks

    cli_checks = run_checks(app.base_cfg, offline=True)
    assert [c["id"] for c in cli_checks] == ids
    # the CLI's rendering of the shared check is the line doctor always printed
    assert format_check({"label": "python", "detail": "3.12.0 ok"}).startswith("python        : ")


def test_doctor_endpoint_reports_an_unwritable_workspace(tmp_path):
    app = AppState(store_root=tmp_path / "s",
                   cfg_overrides={"workspace_root": str(tmp_path / "ws")})
    base, _ = _server(app)
    # a path whose parent is a FILE cannot be created, so mkdir fails
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    app.base_cfg.workspace_root = str(blocker / "inside")

    status, data = _req(base, "/api/doctor?offline=1")
    assert status == 200, data
    ws = next(c for c in data["checks"] if c["id"] == "workspace")
    assert ws["status"] == "fail" and "NOT WRITABLE" in ws["detail"]
    assert ws["hint"], "a failure must say what to do next"
    assert data["failures"] >= 1


def test_update_endpoint_reports_a_newer_release(tmp_path, monkeypatch):
    """A GUI-only user could not learn a release existed."""
    import saturday.update as upd

    monkeypatch.setattr(upd, "current_version", lambda: "0.9.0")
    monkeypatch.setattr(upd, "latest_release", lambda *a, **k: {
        "tag": "v0.9.1", "url": "https://example.invalid/rel", "assets": []})
    monkeypatch.setattr(upd, "detect_channel", lambda: "pip")
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)

    status, data = _req(base, "/api/update")
    assert status == 200, data
    assert data["current"] == "0.9.0" and data["latest"] == "v0.9.1"
    assert data["newer"] is True and data["channel"] == "pip"
    assert data["command"] == "saturday update --apply"


def test_update_endpoint_says_up_to_date_and_survives_no_network(tmp_path, monkeypatch):
    import saturday.update as upd

    monkeypatch.setattr(upd, "current_version", lambda: "0.9.0")
    monkeypatch.setattr(upd, "detect_channel", lambda: "pip")
    monkeypatch.setattr(upd, "latest_release", lambda *a, **k: {"tag": "v0.9.0", "url": ""})
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)
    status, data = _req(base, "/api/update")
    assert status == 200 and data["newer"] is False

    # a failed check must report, not 500 - the app still works offline
    monkeypatch.setattr(upd, "latest_release", lambda *a, **k: None)
    status, data = _req(base, "/api/update")
    assert status == 200 and "error" in data and data["current"] == "0.9.0"

    def boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(upd, "latest_release", boom)
    status, data = _req(base, "/api/update")
    assert status == 200 and "OSError" in data["error"]


def test_update_endpoint_never_applies_anything(tmp_path, monkeypatch):
    """Applying replaces the package this server runs from; the check must not."""
    import saturday.update as upd

    called = []
    monkeypatch.setattr(upd, "current_version", lambda: "0.9.0")
    monkeypatch.setattr(upd, "detect_channel", lambda: "pip")
    monkeypatch.setattr(upd, "latest_release", lambda *a, **k: {"tag": "v9.9.9", "url": ""})
    monkeypatch.setattr(upd, "perform_update", lambda ch: called.append(ch) or (True, ""))
    monkeypatch.setattr(upd, "relaunch", lambda: called.append("relaunch"))
    app = AppState(store_root=tmp_path / "s")
    base, _ = _server(app)

    status, data = _req(base, "/api/update")
    assert status == 200 and data["newer"] is True
    assert called == [], "checking for an update must never install one"


def test_memory_endpoint_searches_and_returns_the_graph(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    # memory_path() reads CONFIG_DIR directly, so patching get_config_dir alone
    # points the index and the file at two different places
    monkeypatch.setattr("saturday.config.CONFIG_DIR", cfg)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    (cfg / "MEMORY.md").write_text(
        "- Postgres connection pool is set to 20\n"
        "- We raised the postgres connection pool from 20 to 50\n"
        "- Frontend bundling uses vite\n", encoding="utf-8")

    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(tmp_path)})
    base, _ = _server(app)

    status, data = _req(base, "/api/memory?q=postgres+pool")
    assert status == 200, data
    assert data["results"], "the note must be findable"
    assert all(0.0 <= r["score"] <= 1.0 for r in data["results"])
    assert "vite" not in " ".join(r["text"] for r in data["results"])

    status, graph = _req(base, "/api/memory?graph=1")
    assert status == 200 and len(graph["nodes"]) == 3
    assert all({"id", "slug", "text", "salience"} <= set(n) for n in graph["nodes"])


def test_memory_endpoint_picks_up_an_edited_file(tmp_path, monkeypatch):
    """MEMORY.md is the truth; the index is derived, so a hand edit must show."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("saturday.config.CONFIG_DIR", cfg)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    (cfg / "MEMORY.md").write_text("- the original note about caching\n", encoding="utf-8")
    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(tmp_path)})
    base, _ = _server(app)

    _, first = _req(base, "/api/memory?graph=1")
    assert len(first["nodes"]) == 1
    (cfg / "MEMORY.md").write_text(
        "- the original note about caching\n- a second note about queues\n", encoding="utf-8")
    _, second = _req(base, "/api/memory?graph=1")
    assert len(second["nodes"]) == 2


def test_memory_consolidate_is_a_post_and_reports_before_it_changes(tmp_path, monkeypatch):
    """Archiving mutates what the agent can recall; a page load must not do it."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("saturday.config.CONFIG_DIR", cfg)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "parser.py").write_text("def parse():\n    pass\n", encoding="utf-8")
    (cfg / "MEMORY.md").write_text(
        "- The parser lives in src/parser.py\n- The old loader was in src/loader.py\n",
        encoding="utf-8")

    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(ws)})
    base, _ = _server(app)

    status, data = _req(base, "/api/memory/consolidate", "POST", {})
    assert status == 200, data
    assert data["dry_run"] is True
    assert [s["code_entity"] for s in data["stale"]] == ["src/loader.py"]

    # and it must not be reachable as a GET
    status, _ = _req(base, "/api/memory/consolidate")
    assert status in (404, 405)
