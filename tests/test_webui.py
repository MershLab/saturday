"""Tests for the desktop web app surface (webui.py): HTTP API, streaming chat,
approval bridging, file gate, slash commands, hydration, config."""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("SATURDAY_APPROVAL_TTL", "6")

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.webui import AppState, AppServer, WebApprover  # noqa: E402

TOKEN = "tok"


@pytest.fixture(autouse=True)
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


# --------------------------------------------------------------------- static


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


# ----------------------------------------------------------------------- chat


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


# ------------------------------------------------------------------ approvals


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


# ------------------------------------------------------------------ hydration


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


# --------------------------------------------------------------------- config


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


# ---------------------------------------------------------------- unit pieces


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


# ------------------------------------------------------- review-fix regressions


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
