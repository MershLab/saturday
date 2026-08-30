"""Merged from: tests/test_recall.py, tests/test_project_memory.py."""


from __future__ import annotations
import json
import time
from saturday.recall import RecallIndex, format_recall
from saturday.tools.recall import MemorySearchTool
import sys
from pathlib import Path
import pytest  # noqa: E402
from saturday.agent.core import Agent  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.tools.memory import MemoryTool, load_memory_block, memory_path  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402



# --- from tests/test_recall.py ---

def _write_transcripts(store_root, session_id: str, lines: list[tuple[str, str]], ts: float = 1_700_000_000.0) -> None:
    store_root.mkdir(parents=True, exist_ok=True)
    with (store_root / f"{session_id}.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "meta", "id": session_id, "created": ts}) + "\n")
        for role, text in lines:
            fh.write(json.dumps({"type": role, "role": role, "text": text, "ts": ts}) + "\n")


def test_index_builds_and_searches_fts5(tmp_path):
    store = tmp_path / "sessions"
    _write_transcripts(store, "bg-20260801", [
        ("user", "deploy the billing service to staging"),
        ("assistant", "The deploy script lives in scripts/deploy.py"),
        ("tool", "[tool:shell] exit_code: 0"),
    ])
    _write_transcripts(store, "bg-20260802", [
        ("user", "anything about the billing service?"),
        ("assistant", "not in this session"),
    ])
    idx = RecallIndex(store_root=store, db_path=tmp_path / "recall.db")
    hits = idx.search("deploy billing")
    assert hits, "FTS5 recall should hit the billing/deploy session"
    assert hits[0]["session"] == "bg-20260801"
    # tool-result lines are not indexed — deployment finds user/assistant only
    assert all(not h["text"].startswith("[tool:") for h in hits)


def test_search_degrades_to_like_without_fts5(tmp_path, monkeypatch):
    store = tmp_path / "sessions"
    _write_transcripts(store, "bg-1", [("user", "the API key is in .env (never commit it)")])
    idx = RecallIndex(store_root=store, db_path=tmp_path / "recall.db")
    idx._has_fts5 = False  # simulate a build without FTS5
    hits = idx.search("api key")
    assert hits and "API key is in" in hits[0]["text"]


def test_index_rebuilds_when_new_session_appears(tmp_path):
    store = tmp_path / "sessions"
    _write_transcripts(store, "one", [("user", "first conversation about maps")])
    idx = RecallIndex(store_root=store, db_path=tmp_path / "recall.db")
    assert len(idx.search("maps")) == 1
    time.sleep(0.01)
    _write_transcripts(store, "two", [("user", "second conversation about maps")])
    assert len(idx.search("maps")) == 2, "stale index must rebuild on new files"


def test_memory_search_tool_and_format(tmp_path):
    store = tmp_path / "sessions"
    _write_transcripts(store, "bg-9", [("user", "remember the jenkins pipeline is flaky on fridays")])
    tool = MemorySearchTool(index=RecallIndex(store_root=store, db_path=tmp_path / "recall.db"))
    ok, out = tool.run({"query": "jenkins pipeline"})
    assert ok and "session bg-9" in out and "flaky" in out
    ok2, out2 = tool.run({"query": "nonexistent-zzz"})
    assert ok2 and "no past sessions" in out2
    ok3, out3 = tool.run({})
    assert not ok3


def test_format_recall_shortens_and_strips_newlines():
    rendered = format_recall([{"session": "bg-1", "ts": 0, "role": "user", "text": "hello\nworld very long text " * 30}])
    assert "very long text" in rendered and "\nworld" not in rendered



# --- from tests/test_project_memory.py ---

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    return tmp_path


def test_unscoped_tool_uses_global_file(tmp_path):
    tool = MemoryTool()
    assert tool.run({"action": "append", "text": "likes tea"})[0]
    assert "likes tea" in memory_path().read_text(encoding="utf-8")
    assert memory_path() == tmp_path / "MEMORY.md", "fixture isolation confirmed"


def test_scoped_writes_isolated_from_global(tmp_path):
    ws = tmp_path / "proj-ws"
    ws.mkdir()
    scoped_file = ws / ".saturday" / "MEMORY.md"
    global_mem = memory_path()
    global_mem.write_text("global fact A\n", encoding="utf-8")

    tool = MemoryTool(scope_path=str(scoped_file))
    ok, out = tool.run({"action": "append", "text": "project fact B"})
    assert ok and str(scoped_file) in out
    assert "project fact B" in scoped_file.read_text(encoding="utf-8")
    assert global_mem.read_text(encoding="utf-8") == "global fact A\n", "global file untouched"


def test_scoped_read_merges_global_and_project(tmp_path):
    ws = tmp_path / "proj-ws"
    ws.mkdir()
    scoped_file = ws / ".saturday" / "MEMORY.md"
    memory_path().write_text("global fact A\n", encoding="utf-8")
    tool = MemoryTool(scope_path=str(scoped_file))
    tool.run({"action": "append", "text": "project fact B"})
    ok, merged = tool.run({"action": "read"})
    assert ok
    assert "(global memory)" in merged and "(project memory)" in merged
    assert "global fact A" in merged and "project fact B" in merged


def test_agent_prompt_and_registry_follow_memory_scope(tmp_path):
    ws = tmp_path / "proj-ws"
    ws.mkdir()
    (ws / ".saturday").mkdir(parents=True)
    (ws / ".saturday" / "MEMORY.md").write_text("the deploy key lives in vault\n", encoding="utf-8")

    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False)
    agent.memory_scope = str(ws)
    prompt = agent.system_prompt(agent._build_registry())
    assert "deploy key lives in vault" in prompt
    mem_tool = agent.registry.get("memory")
    assert mem_tool.scope_path.endswith(".saturday" + chr(92) + "MEMORY.md") or mem_tool.scope_path.endswith(".saturday/MEMORY.md")


def test_load_memory_block_scope_only_when_set(tmp_path):
    assert isinstance(load_memory_block(None), str)
    assert load_memory_block(scope=tmp_path) == "", "empty scope dir contributes nothing"


TOKEN = "tok"


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


def test_project_sessions_get_memory_scope(tmp_path):
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    proj_ws = tmp_path / "pws"
    proj_ws.mkdir()
    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    with _Server(app) as srv:
        _, d = _req(srv.base, "/api/projects", "POST", {"name": "Scoped", "workspace": str(proj_ws)})
        pid = d["project"]["id"]
        # untagged session: no scope
        free = app.store.create({"task": "free", "surface": "app"})
        rt_free = app.runtime_for(free)
        assert getattr(rt_free.agent, "memory_scope", None) is None
        # tagged session: scoped to the project workspace
        payload = {"text": "hi there", "project_id": pid}
        r = urllib.request.Request(srv.base + "/api/chat", data=json.dumps(payload).encode(), method="POST")
        r.add_header("X-Saturday-Token", TOKEN)
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=120) as resp:
            first_line = resp.read().decode().split("\n", 1)[0]
        tagged_sid = json.loads(first_line)["sid"]
        rt_tagged = app.runtime_for(tagged_sid)
        assert getattr(rt_tagged.agent, "memory_scope", None) == str(proj_ws)
        mem_tool = rt_tagged.agent.registry.get("memory")
        assert mem_tool is not None and str(proj_ws) in mem_tool.scope_path


def test_recall_indexes_real_session_shape(tmp_path):
    from saturday.recall import RecallIndex
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "sessions")
    sid = store.create({"task": "deploy notes"})
    store.append(sid, {"type": "messages", "messages": [
        {"role": "user", "content": "deploy the billing service to staging"},
        {"role": "assistant", "content": "The deploy script lives in scripts/deploy.py"},
        {"role": "tool", "tool_call_id": "t1", "name": "shell", "content": "ok"},
    ]})

    idx = RecallIndex(store_root=tmp_path / "sessions", db_path=tmp_path / "recall.db")
    assert idx.rebuild() == 2, "user + assistant messages indexed; tool message skipped"
    hits = idx.search("deploy billing")
    assert hits, "real transcript shape must be searchable"
    assert hits[0]["session"] == sid


def test_recall_memory_search_tool_end_to_end(tmp_path, monkeypatch):
    import saturday.recall as recall_mod
    from saturday.sessions import SessionStore
    from saturday.tools.recall import MemorySearchTool

    store = SessionStore(root=tmp_path / "sessions")
    sid = store.create({"task": "t"})
    store.append(sid, {"type": "messages", "messages": [
        {"role": "user", "content": "where did we put the database schema?"},
    ]})

    monkeypatch.setattr(recall_mod, "default_store_root", lambda: tmp_path / "sessions")
    tool = MemorySearchTool()
    ok, out = tool.run({"query": "database schema"})
    assert ok and "database schema" in out and sid in out
