"""Functionality review round 1 fixes:

1. Cron: day-of-week 7 (standard-cron Sunday alias) must match Sunday.
2. glob/grep: matches that resolve outside the workspace root ('..' or
   symlinked components) are skipped instead of being listed/searched.
3. memory_search: the recall index understands the SessionStore transcript
   shape ({"type": "messages", "messages": [...]}) — previously it indexed
   ONLY a flat shape no real writer produces, so the tool was dead on real
   sessions.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# ------------------------------------------------------------------- schedule

def test_cron_dow_7_matches_sunday():
    from saturday.schedule import _valid_expr, cron_matches

    sunday = datetime(2026, 8, 30, 9, 0)
    monday = datetime(2026, 8, 31, 9, 0)
    assert _valid_expr("0 9 * * 7")
    assert cron_matches("* * * * 7", sunday), "dow=7 must match Sunday"
    assert not cron_matches("* * * * 7", monday), "dow=7 must not match Monday"
    assert cron_matches("* * * * 0", sunday), "dow=0 still matches Sunday"
    assert cron_matches("* * * * 0,7", sunday), "combined 0,7 matches Sunday once"


def test_cron_dow_7_end_to_end(tmp_path):
    from saturday.schedule import ScheduleStore

    store = ScheduleStore(path=tmp_path / "schedules.json")
    store.add("sun", "0 9 * * 7", "weekly sunday task")
    due = store.due(now=datetime(2026, 8, 30, 9, 0))
    assert [s.id for s in due] == ["sun"]


# ----------------------------------------------------------- glob/grep bounds

def test_glob_skips_matches_outside_workspace(tmp_path):
    from saturday.tools.files import GlobTool

    (tmp_path / "outside.py").write_text("x = 1", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inner.py").write_text("x = 2", encoding="utf-8")

    tool = GlobTool(root=str(root))
    # '../*.py' from the workspace root points at the PARENT dir: every match
    # resolves outside the workspace and must be dropped
    ok, out = tool.run({"pattern": "../*.py"})
    assert ok and out == "(no matches)", out
    # the workspace itself stays fully visible
    ok, out = tool.run({"pattern": "*.py"})
    assert ok and out == "inner.py", out


def test_grep_skips_matches_outside_workspace(tmp_path):
    from saturday.tools.files import GrepTool

    (tmp_path / "outside.py").write_text("SECRET_TOKEN = 'leak'", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inner.py").write_text("SECRET_TOKEN = 'fine'", encoding="utf-8")

    tool = GrepTool(root=str(root))
    ok, out = tool.run({"pattern": "SECRET_TOKEN", "include": "../*.py"})
    assert ok and out == "(no matches)", "content from outside the workspace must not leak"
    ok, out = tool.run({"pattern": "SECRET_TOKEN", "include": "*.py"})
    assert ok and "inner.py" in out and "fine" in out


def test_glob_still_finds_nested_files(tmp_path):
    from saturday.tools.files import GlobTool

    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("pass", encoding="utf-8")
    ok, out = GlobTool(root=str(root)).run({"pattern": "**/*.py"})
    assert ok and "src/app.py" in out


# ------------------------------------------------------------- recall wiring

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
