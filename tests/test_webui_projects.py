"""Tests for Projects (Codex/Claude-Desktop-style): ProjectStore CRUD, HTTP API,
session tagging, per-project workspace + persona wiring, assign/delete flows."""
from __future__ import annotations

import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.projects import ProjectStore  # noqa: E402
from saturday.webui import AppState, AppServer  # noqa: E402

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


def make_app(tmp_path: Path, turns) -> AppState:
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


# ------------------------------------------------------------------ store unit


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


# ------------------------------------------------------------------ http api


def test_create_and_list_projects_api(tmp_path: Path):
    app = make_app(tmp_path, [])
    ws = tmp_path / "repo"
    ws.mkdir()
    with _Server(app) as srv:
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
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        _, data = req(srv.base, "/api/projects", "POST", {"name": "P1"})
        pid = data["project"]["id"]

        status, data = req(srv.base, f"/api/project/{pid}", "PATCH", {"name": "P1 renamed", "instructions": "new rules"})
        assert status == 200 and data["project"]["name"] == "P1 renamed"
        assert app.projects.get(pid).instructions == "new rules"

        status, _ = req(srv.base, "/api/project/ghost", "PATCH", {"name": "x"})
        assert status == 404


def test_chat_tags_session_with_project(tmp_path: Path):
    turns = [{"content": "hi from project"}]
    app = make_app(tmp_path, turns)
    ws = tmp_path / "projws"
    (ws / "inner").mkdir(parents=True)
    with _Server(app) as srv:
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
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
        status, _ = req(srv.base, "/api/chat", "POST", {"text": "x", "project_id": "ghost"})
        assert status == 400
        assert not app.store.list_sessions(), "no session may be created for a bad project"


def test_untagged_sessions_stay_in_default_view(tmp_path: Path):
    app = make_app(tmp_path, [{"content": "plain"}])
    with _Server(app) as srv:
        events = stream_chat(srv.base, {"text": "plain chat"})
        sid = events[0]["sid"]
        rows = {r["id"]: r for r in app.store.list_sessions()}
        assert rows[sid]["project"] == ""
        rt = app.runtime_for(sid)
        assert rt.project_id is None
        assert rt.agent.cfg.workspace_root == str(tmp_path / "global-ws")


def test_assign_moves_session_between_projects(tmp_path: Path):
    app = make_app(tmp_path, [{"content": "ok"}])
    wsa = tmp_path / "wsa"
    wsb = tmp_path / "wsb"
    for w in (wsa, wsb):
        w.mkdir()
    with _Server(app) as srv:
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
    app = make_app(tmp_path, [{"content": "kept"}])
    with _Server(app) as srv:
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
    app = make_app(tmp_path, [])
    with _Server(app) as srv:
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


# ------------------------------------------------------- competitor-parity layer


def test_project_color_and_knowledge_files_roundtrip(tmp_path: Path):
    app = make_app(tmp_path, [])
    kf1 = tmp_path / "style-guide.md"
    kf1.write_text("always use tabs", encoding="utf-8")
    kf2 = tmp_path / "glossary.md"
    kf2.write_text("df = saturday", encoding="utf-8")
    with _Server(app) as srv:
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
    app = make_app(tmp_path, turns)
    kf = tmp_path / "kb.txt"
    kf.write_text("DF-KNOWLEDGE-MARKER-4242 " + "filler\n" * 50, encoding="utf-8")
    big = tmp_path / "big.txt"
    big.write_text("x" * (25_000), encoding="utf-8")
    with _Server(app) as srv:
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
    app = make_app(tmp_path, [{"content": "ok"}])
    kf = tmp_path / "gone.txt"
    kf.write_text("temp knowledge", encoding="utf-8")
    with _Server(app) as srv:
        _, data = req(srv.base, "/api/projects", "POST", {"name": "Vanish", "files": [str(kf)]})
        pid = data["project"]["id"]
        kf.unlink()
        events = stream_chat(srv.base, {"text": "still works?", "project_id": pid})
        done = [e for e in events if e["t"] == "done"][0]
        assert done["final"] == "ok"
        rt = app.runtime_for(done["sid"])
        assert "(unreadable)" in rt.agent.persona_extra
