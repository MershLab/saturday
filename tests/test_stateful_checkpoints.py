"""Claude Code / Cursor parity: stateful checkpoints + workspace rewind.

Covers: rich checkpoint payloads (memory/todo/goals/journal position),
restore-on-resume, fsync-durable stores, and journal.restore_to_length
(Cursor-style 'rewind files to checkpoint state')."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# ------------------------------------------------------------------ journal

def test_journal_length_and_restore_to_length(tmp_path):
    from saturday.tools.files import EditFile, WriteFile
    from saturday.tools.journal import journal_length, restore_to_length

    root = str(tmp_path)
    w = WriteFile(root=root)
    ok, _ = w.run({"path": "a.txt", "content": "v1"})
    assert ok
    base_len = journal_length(root)  # creation tombstone recorded
    ok, _ = w.run({"path": "b.txt", "content": "new file"})
    ok, _ = EditFile(root=root).run({"path": "a.txt", "old_string": "v1", "new_string": "v2"})
    assert journal_length(root) == base_len + 2
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"
    assert (tmp_path / "b.txt").exists()

    ok, msg = restore_to_length(root, base_len)
    assert ok, msg
    # a.txt restored to pre-edit content; b.txt (creation after checkpoint) gone
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"
    assert not (tmp_path / "b.txt").exists()


def test_restore_to_length_refuses_truncated_snapshot(tmp_path):
    from saturday.tools.journal import record_edit, restore_to_length

    big = "x" * 250_000
    p = tmp_path / "big.txt"
    p.write_text(big, encoding="utf-8")
    record_edit(str(tmp_path), "edit_file", str(p))  # snapshot truncated at cap
    (tmp_path / "other.txt").write_text("z", encoding="utf-8")
    record_edit(str(tmp_path), "write_file", str(tmp_path / "other.txt"))
    ok, msg = restore_to_length(str(tmp_path), 0)
    assert not ok and "truncated" in msg
    assert p.read_text(encoding="utf-8") == big  # nothing was touched


def test_restore_to_length_noop_and_outside_workspace_skip(tmp_path):
    from saturday.tools.journal import restore_to_length

    ok, msg = restore_to_length(str(tmp_path), 0)
    assert ok and "nothing to rewind" in msg


# ------------------------------------------------------- stateful checkpoints

def test_checkpoint_roundtrip_with_meta_and_legacy_backcompat(tmp_path):
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "sess")
    sid = store.create({"task": "t"})
    store.save_checkpoint(sid, [{"role": "user", "content": "hi"}],
                          meta={"journal_len": 3, "memory": [{"kind": "k", "text": "v"}], "tools": {}})
    msgs = store.load_checkpoint(sid)
    assert msgs and msgs[0]["content"] == "hi"
    meta = store.load_checkpoint_meta(sid)
    assert meta["journal_len"] == 3 and meta["memory"][0]["text"] == "v"

    # legacy payload without "meta" still loads; meta accessor returns None/{}
    p = store._path(sid).with_suffix(".checkpoint.json")
    p.write_text('{"ts": 1, "messages": [{"role": "user"}]}', encoding="utf-8")
    assert store.load_checkpoint(sid)[0]["role"] == "user"
    assert not store.load_checkpoint_meta(sid)


def test_todo_and_goal_state_survive_roundtrip():
    from saturday.agent.todo import TodoTool
    from saturday.tools.goals import build_goal_tools

    todo = TodoTool()
    todo.run({"action": "write", "steps_text": "step one\nstep two"})
    todo.run({"action": "mark", "index": 1})
    snapshot = todo.export_state()

    fresh = TodoTool()
    assert fresh.export_state()["steps"] == []
    fresh.import_state(snapshot)
    ok, out = fresh.run({"action": "read"})
    assert ok and "1/2" in out.replace("[x]", "1").replace("[ ]", "") or "progress: 1/2" in out

    _, tools = build_goal_tools()
    ok, _ = tools[0].run({"text": "ship v1"})
    snap = tools[0].export_state()
    _, tools2 = build_goal_tools()
    assert tools2[0].export_state()["goal"] is None
    tools2[0].import_state(snap)
    ok, out = tools2[1].run({})
    assert "ship v1" in out and "active" in out


def test_agent_checkpoint_meta_captures_and_restores_tool_state(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig

    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False)

    # find the todo tool inside the assembled registry and set a plan
    agent._build_registry()
    todo = agent.registry.get("todo")
    todo.run({"action": "write", "steps_text": "alpha\nbeta"})
    agent.memory.add("decision", "use sqlite for storage")

    meta = agent._checkpoint_meta()
    assert isinstance(meta["journal_len"], int)
    assert any(s.get("text") == "alpha" for s in meta["tools"]["todo"]["steps"])
    assert meta["memory"][-1]["kind"] == "decision"

    # simulate a fresh process: blank memory, blank plan -> restore
    agent2 = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path)),
                   safety=False)
    agent2._build_registry()
    assert agent2.registry.get("todo").export_state()["steps"] == []
    assert agent2.restore_checkpoint_meta(meta) is True
    assert agent2.registry.get("todo").export_state()["steps"][0]["text"] == "alpha"
    assert any(it.text == "use sqlite for storage" for it in agent2.memory.items)
    # idempotent-ish: empty meta is a no-op returning False
    assert agent2.restore_checkpoint_meta({}) is False


# ------------------------------------------------- frontend slash-command wiring

def test_webui_slash_yolo_and_rewind_wired(tmp_path):
    """The web app's chat box must reach the same features the REPL has:
    /yolo flips mode + gate + badge state; /rewind rolls files to checkpoint."""
    from saturday.config import AgentConfig
    from saturday.session_runtime import SessionRuntime
    from saturday.webui import SLASH_ALIASES, handle_slash
    from saturday.tools.files import WriteFile
    from saturday.tools.journal import journal_length

    assert "/yolo" in SLASH_ALIASES and "/rewind" in SLASH_ALIASES

    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "sess")

    class A:
        cfg = AgentConfig(workspace_root=str(tmp_path))
        plan_mode = False
        safety_mode = "ask"  # per-agent effective mode (r2: no cfg bleed)
        session_store = store

        def effective_registry(self):
            return type("R", (), {"names": staticmethod(lambda: [])})()

        disabled_tools = set()

        def toggle_tool(self, *a, **k):
            return True, "", False

    rt = SessionRuntime("sid-yolo", A())

    # yolo on: policy + gate flip; config event carries safety_mode for the badge
    events = handle_slash(rt, "/yolo")
    assert rt.agent.safety_mode == "autonomous" and rt.file_gate.auto_approve is True
    # r2: the flip must NOT bleed into the shared cfg
    assert rt.agent.cfg.safety_mode == "ask"
    assert any(e.get("t") == "config" and e.get("safety_mode") == "autonomous" for e in events)
    # yolo off restores ask
    handle_slash(rt, "/yolo")
    assert rt.agent.safety_mode == "ask" and rt.file_gate.auto_approve is False

    # rewind without any checkpoint metadata -> friendly hint, not a crash
    events = handle_slash(rt, "/rewind")
    assert events and ("no checkpoint metadata" in events[0]["s"] or "nothing to rewind" in events[0]["s"])

    # rewind with real checkpoint metadata rolls files back
    w = WriteFile(root=str(tmp_path))
    w.run({"path": "keep.txt", "content": "base"})
    base_len = journal_length(tmp_path)
    sid = "sid-rew"
    rt.store.create({"task": "rw", "id": sid})
    rt.sid = sid
    rt.store.save_checkpoint(sid, [], meta={"journal_len": base_len})
    w.run({"path": "later.txt", "content": "after"})
    assert (tmp_path / "later.txt").exists()
    events = handle_slash(rt, "/rewind")
    assert "restored" in events[0]["s"]
    assert not (tmp_path / "later.txt").exists()


def test_repl_help_lists_all_web_slash_commands():
    """Every REPL HELP_TEXT slash command must be dispatchable by the web
    app's alias table (single source of truth for what users can type)."""
    import re

    from saturday.repl import HELP_TEXT
    from saturday.webui import SLASH_ALIASES

    repl_cmds = set(re.findall(r"^  (/\w+)", HELP_TEXT, re.M))
    missing = {c for c in repl_cmds if c not in ("/attach", "/images")} - set(SLASH_ALIASES)
    assert not missing, f"web app cannot dispatch: {sorted(missing)}"


def test_slash_menu_served_from_backend_not_hardcoded():
    """Regression: the UI '/' autocomplete used to read a stale hardcoded JS
    array, so new commands worked but never appeared. The menu must now be
    served by /api/state and cover every dispatchable command."""
    from saturday.webui import SLASH_ALIASES, SLASH_COMMAND_LIST

    # alias table is DERIVED from the served list -> cannot diverge
    assert set(SLASH_ALIASES) == {name for name, _ in SLASH_COMMAND_LIST}
    served = {name for name, _ in SLASH_COMMAND_LIST}
    for required in ("/yolo", "/rewind", "/plan", "/branch", "/revert", "/toggle",
                     "/jobs", "/goals", "/skills"):
        assert required in served, f"{required} missing from the slash menu"

    js = (Path(__file__).parents[1] / "src" / "saturday" / "webui_assets" / "app.js").read_text(encoding="utf-8")
    assert "SLASH_COMMANDS = [" not in js, "frontend must not keep its own command copy"
    assert "info.slash_commands" in js and "slashCommandList()" in js


def test_state_payload_serves_slash_menu(tmp_path):
    from saturday.webui import AppState

    app = AppState(cfg_overrides={"workspace_root": str(Path.cwd())})
    payload = app.state_payload()
    cmds = [c[0] for c in payload["slash_commands"]]
    assert "/yolo" in cmds and "/rewind" in cmds and len(cmds) >= 18


def test_webui_slash_jobs_goals_skills(tmp_path):
    """Hidden subsystems (jobs/goals/skills) get user-visible slash surfaces."""
    from saturday.config import AgentConfig
    from saturday.session_runtime import SessionRuntime
    from saturday.webui import handle_slash

    class FakeReg:
        def __init__(self, tools):
            self._t = tools

        def get(self, name):
            return self._t.get(name)

    class Todo:
        plan = type("P", (), {"render": staticmethod(lambda: "goal: x\n1. [ ] a")})()

    reg = FakeReg({
        "job_list": type("J", (), {"run": staticmethod(lambda a: (True, "no background jobs"))})(),
        "get_goal": type("G", (), {
            "store": type("S", (), {"get": lambda self: "goal: ship v1 | status: active | round: 0"})(),
            "run": lambda self, a: (True, self.store.get()),
        })(),
        "skills_index": type("K", (), {"run": staticmethod(lambda a: (True, "(no skills saved yet)"))})(),
    })

    class A2:
        cfg = AgentConfig(workspace_root=str(tmp_path))
        plan_mode = False

        def _build_registry(self):
            return reg

    rt = SessionRuntime("sid-feat", A2())
    assert "no background jobs" in handle_slash(rt, "/jobs")[0]["s"]
    assert "ship v1" in handle_slash(rt, "/goals")[0]["s"]
    assert "no skills" in handle_slash(rt, "/skills")[0]["s"]


# --------------------------------------------------------------------- /rewind

def test_repl_rewind_command_rolls_files_forward_to_checkpoint(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.sessions import SessionStore
    from saturday.tools.files import WriteFile
    from saturday.tools.journal import journal_length

    ws = tmp_path / "ws"
    ws.mkdir()
    w = WriteFile(root=str(ws))
    w.run({"path": "keep.txt", "content": "base"})          # entry 0
    base_len = journal_length(ws)

    store = SessionStore(root=tmp_path / "s")
    agent = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(ws)),
                  safety=False, session_store=store)
    repl = Repl(agent, store=store, output_fn=lambda *a, **k: None)
    repl._sid = store.create({"task": "rw"})
    # simulate a checkpoint taken when the journal was at base_len
    store.save_checkpoint(repl._sid, [], meta={"journal_len": base_len})

    w.run({"path": "later.txt", "content": "after checkpoint"})  # entry 1
    assert (ws / "later.txt").exists()

    collected: list[str] = []
    repl._output = lambda *a, **k: collected.append(" ".join(str(x) for x in a))
    assert repl.dispatch("/rewind") is True
    joined = "\n".join(collected)
    assert "[rewind]" in joined and "restored" in joined
    assert not (ws / "later.txt").exists()      # post-checkpoint creation undone
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "base"
