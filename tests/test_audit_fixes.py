"""Regression tests for bugs found in the pre-commit audit of the staged tree."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.loop import AgentLoop, enforce_message_invariants  # noqa: E402
from saturday.agent.memory import estimate_message_tokens  # noqa: E402
from saturday.safety import ApprovalPolicy, check_command  # noqa: E402
from saturday.sessions import SessionStore  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.files import ReadFile  # noqa: E402


def test_compact_noop_on_short_history():
    model = make_scripted_model([{"content": "x"}])
    loop = AgentLoop(model, ToolRegistry(), max_steps=1)
    history = [
        {"role": "user", "content": "# Goal\nkeep me"},
        {"role": "assistant", "content": "working"},
        {"role": "tool", "tool_call_id": "c0", "name": "t", "content": "obs"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
    ]
    loop._compact(history)
    assert history[0]["role"] == "user"
    assert "keep me" in str(history[0].get("content"))
    assert not any(i.kind == "compaction-summary" for i in loop.memory.items)


def test_compact_force_noop_on_short_history():
    model = make_scripted_model([{"content": "x"}])
    loop = AgentLoop(model, ToolRegistry(), max_steps=1)
    history = [{"role": "user", "content": "# Goal\nG"}]
    loop._compact(history, force=True)
    assert len(history) == 1 and "# Goal" in str(history[0]["content"])


def test_invariants_merge_text_and_vision_user_messages():
    vision = [
        {"type": "text", "text": "[images from tool 'view_image']"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "user", "content": vision},
        {"role": "user", "content": "[empty response] Continue pursuing the goal."},
    ]
    out = enforce_message_invariants(history)
    users = [m for m in out if m["role"] == "user"]
    assert len(users) == 2
    merged = users[-1]["content"]
    assert isinstance(merged, list), f"vision parts lost to string merge: {merged!r}"
    assert {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}} in merged


def test_run_survives_empty_response_after_vision_message(tmp_path: Path):
    reg = ToolRegistry()
    reg.register(ReadFile(root=str(tmp_path)))
    image = tmp_path / "tiny.png"
    image.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6300010000050001"))
    from saturday.tools.vision import ViewImageTool

    view = ViewImageTool(root=str(tmp_path))
    reg.register(view)

    model = make_scripted_model(
        [
            {"tool_calls": [{"name": "view_image", "arguments": {"path": str(image)}}]},
            {"content": ""},  # empty response -> nudge becomes adjacent to vision message
            {"content": "done"},
        ]
    )
    loop = AgentLoop(model, reg, max_steps=3)
    traj = loop.run("sys", "inspect the image")
    assert traj.stop_reason == "done"


def test_tool_call_cap_keeps_history_well_formed():
    from saturday.agent.loop import MAX_TOOL_CALLS_PER_STEP

    # send more calls than the per-step cap allows; the loop must execute only
    # the first MAX and serialize exactly what it executed (no orphan results)
    calls = [{"name": "read_file", "arguments": {"path": "f"}} for _ in range(MAX_TOOL_CALLS_PER_STEP + 1)]
    model = make_scripted_model([{"tool_calls": calls}, {"content": "ok"}])
    reg = ToolRegistry()

    class _FakeRead:
        name = "read_file"
        description = "fake"
        parameters = {"type": "object", "properties": {}}

        def run(self, args):
            return True, "contents"

    reg.register(_FakeRead())
    loop = AgentLoop(model, reg, max_steps=2)
    traj = loop.run("sys", "fan out")
    assert traj.stop_reason == "done"

    first_request = model.calls[1]["messages"]
    assistants = [m for m in first_request if m.get("role") == "assistant"]
    tools = [m for m in first_request if m.get("role") == "tool"]
    assert len(assistants) == 1
    declared = assistants[0].get("tool_calls") or []
    assert len(declared) <= MAX_TOOL_CALLS_PER_STEP, (
        f"serialized {len(declared)} tool_calls but cap is {MAX_TOOL_CALLS_PER_STEP}"
    )
    assert len(tools) == len(declared)


def test_ask_approval_of_one_pattern_still_gates_others():
    approved: list[str] = []

    def approver(command: str, reason: str) -> bool:
        approved.append(reason)
        return True

    policy = ApprovalPolicy.from_mode("ask", approver)
    reason = check_command(policy, "shell", {"command": "git push --force origin main & reg delete HKLM\\Software"})
    assert reason is None
    assert len(approved) >= 2, f"only one pattern gated ({approved}); later patterns skipped"


def test_estimate_message_tokens_ignores_base64_bulk():
    huge_b64 = "A" * (900 * 1024)
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "screenshot"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge_b64}"}},
        ],
    }
    est = estimate_message_tokens(msg)
    assert est < 5000, f"base64 inflated estimate to {est}"


def test_run_with_resume_and_attachments_appends_vision_message(tmp_path: Path):
    image = tmp_path / "pic.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    model = make_scripted_model([{"content": "seen it"}])
    loop = AgentLoop(model, ToolRegistry(), max_steps=1)
    prior = [{"role": "user", "content": "earlier turn"}]
    loop.run("sys", "what is this?", initial_history=prior, attachments=[str(image)])
    sent = model.calls[0]["messages"]
    last = [m for m in sent if m.get("role") == "user"][-1]
    assert isinstance(last["content"], list)
    assert any(p.get("type") == "image_url" for p in last["content"])


def test_append_to_unknown_session_gets_meta_header(tmp_path: Path):
    store = SessionStore(root=tmp_path)
    store.append("brand-new-id", {"type": "messages", "messages": [{"role": "user", "content": "hi"}]})
    data = store.load("brand-new-id")
    assert data is not None
    assert data["meta"].get("id") == "brand-new-id"
    assert data["records"] and data["records"][0]["type"] == "messages"
