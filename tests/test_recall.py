"""Cross-session recall: FTS5 index, search, LIKE fallback, tool wiring."""
from __future__ import annotations

import json
import time

from saturday.recall import RecallIndex, format_recall
from saturday.tools.recall import MemorySearchTool


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
