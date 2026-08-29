"""Fixes from the live-verification session: MCP config tolerance, serve routing,
vision persistence, session-id collisions."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.core import Agent  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.mcp_plugin import load_mcp_config  # noqa: E402
from saturday.sessions import SessionStore  # noqa: E402


def _write_cfg(tmp_path: Path, data):
    p = tmp_path / "mcp.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_mcp_config_accepts_wrapper_and_flat_shapes(tmp_path: Path):
    wrapped = _write_cfg(tmp_path / "a", {"servers": {"one": {"command": "python", "args": ["x"]}}})
    assert load_mcp_config(wrapped) == {"one": {"command": "python", "args": ["x"]}}
    flat = _write_cfg(tmp_path / "b", {"two": {"command": "python"}, "env": {"FOO": "1"}})
    got = load_mcp_config(flat)
    assert "two" in got and "env" not in got, "flat shape must be accepted; non-server keys skipped"


def test_mcp_config_reports_invalid_entries(tmp_path: Path):
    p = _write_cfg(tmp_path, {"good": {"command": "python"}, "bad": {"args": []}})
    problems: list[str] = []
    got = load_mcp_config(p, warnings=problems)
    assert list(got) == ["good"]
    assert any("bad" in w for w in problems)
    broken = _write_cfg(tmp_path / "c", "{not json")
    problems2: list[str] = []
    assert load_mcp_config(broken, warnings=problems2) == {}
    assert problems2 and "unreadable" in problems2[0]


def test_serve_payload_routes_sessions(tmp_path: Path, monkeypatch):
    from saturday.cli import handle_message_payload

    store = SessionStore(root=tmp_path / "s")
    monkeypatch.setattr("saturday.sessions.SessionStore", lambda: store)

    class FakeTraj:
        final_answer = "ok"
        stop_reason = "done"

    seen: list[dict] = []

    def run_fn(text, initial_history, session_id):
        seen.append({"text": text, "history": initial_history, "sid": session_id})
        return FakeTraj()

    out = handle_message_payload({"text": "hi", "session_id": ""}, run_fn)
    assert out["ok"] is True and "session_id" not in out
    assert seen[0]["history"] is None and seen[0]["sid"] is None

    store.create({"id": "sess-x", "task": "t"})
    store.save_checkpoint("sess-x", [{"role": "user", "content": "prior"}])
    out = handle_message_payload({"text": "again", "session_id": "sess-x"}, run_fn)
    assert out.get("session_id") == "sess-x"
    assert seen[1]["history"] == [{"role": "user", "content": "prior"}]
    assert seen[1]["sid"] == "sess-x"

    empty = handle_message_payload({"text": "  "}, run_fn)
    assert empty["ok"] is False and "text" in empty["error"]
    boom = handle_message_payload({"text": "x"}, lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert boom["ok"] is False and "RuntimeError" in boom["error"]


def test_trajectory_persists_vision_seed_message(tmp_path: Path):
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    scripted = make_scripted_model([{"content": "seen"}])
    agent = Agent(
        cfg=AgentConfig(provider="vllm", workspace_root=str(tmp_path), max_steps=1),
        registry=None,
        plugins=[],
        enable_subagents=False,
        safety="off",
        session_store=SessionStore(root=tmp_path / "s"),
    )
    agent._ensure_client = lambda: scripted

    from saturday.tools.vision import ViewImageTool  # noqa: F401

    traj = agent.run("look", attachments=[str(image)])
    record = traj.to_jsonl_record()
    first_user = record["messages"][1]
    assert isinstance(first_user["content"], list), "image parts must survive into persisted records"
    assert any(p.get("type") == "image_url" for p in first_user["content"])
    plain = make_scripted_model([{"content": "ok"}])
    agent._ensure_client = lambda: plain
    traj2 = agent.run("plain task")
    assert traj2.messages()[1]["content"].startswith("# Goal")


def test_long_session_ids_do_not_collide(tmp_path: Path):
    store = SessionStore(root=tmp_path)
    a = "x" * 70 + "-A"
    b = "x" * 70 + "-B"
    pa, pb = store._path(a), store._path(b)
    assert pa != pb, f"distinct long ids collapsed to {pa.name}"
    store.append(a, {"type": "messages", "messages": []})
    store.append(b, {"type": "messages", "messages": []})
    assert store.load(a)["meta"]["id"] == a
    assert store.load(b)["meta"]["id"] == b
