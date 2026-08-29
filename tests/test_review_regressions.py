"""Regression tests for bugs found by manual code review (not by the suite)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.loop import AgentLoop  # noqa: E402
from saturday.llm.client import LLMError, StreamEvent  # noqa: E402
from saturday.safety import ApprovalPolicy, check_command  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402


def test_compact_never_orphans_tool_results():
    """Tail boundary must include the assistant that owns kept tool results."""
    reg = ToolRegistry()
    model = make_scripted_model([{"content": "filler"}])
    loop = AgentLoop(model, reg, max_steps=1, compact_above_tokens=10_000_000)

    history: list[dict] = []
    for i in range(6):
        history.append({"role": "assistant", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "t"}}]})
        history.append({"role": "tool", "tool_call_id": f"c{i}", "name": "t", "content": f"obs {i}"})

    loop._compact(history)

    tool_ids_in_tail = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    owned_ids = set()
    for m in history:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                owned_ids.add(tc["id"])
    assert tool_ids_in_tail <= owned_ids, "orphaned tool result after compaction"


def test_compact_force_boundary_also_safe():
    reg = ToolRegistry()
    model = make_scripted_model([{"content": "x"}])
    loop = AgentLoop(model, reg, max_steps=1)
    history: list[dict] = [{"role": "user", "content": "# Goal\nG"}]
    for i in range(5):
        history.append({"role": "assistant", "tool_calls": [{"id": f"k{i}", "type": "function", "function": {"name": "t"}}]})
        history.append({"role": "tool", "tool_call_id": f"k{i}", "name": "t", "content": "o"})
    loop._compact(history, force=True)
    tool_ids = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    owned = {
        tc["id"]
        for m in history
        if m.get("role") == "assistant"
        for tc in m.get("tool_calls") or []
    }
    assert tool_ids <= owned


def test_safety_blocks_no_preserve_root_bypass():
    reason = check_command(ApprovalPolicy.from_mode("deny"), "shell", {"command": "rm -rf / --no-preserve-root"})
    assert reason and ("HARDLINE" in reason or "no-preserve-root" in reason)


class _StreamFailsAfterEmit:
    def __init__(self, client) -> None:
        self.client = client

    def _chat_stream(self, payload, cb, body=None, model=None):
        cb(StreamEvent(type="text", delta_text="partial"))
        raise ConnectionError("mid-stream death")


def test_stream_retry_does_not_duplicate_deltas(monkeypatch):
    from saturday.llm.client import LLMClient

    client = LLMClient(base_url="http://unit.test", api_key="k", model="primary", max_retries=3, fallback_models=["backup"])
    monkeypatch.setattr(type(client), "_chat_stream", _StreamFailsAfterEmit.__func__ if hasattr(_StreamFailsAfterEmit, "__func__") else _StreamFailsAfterEmit._chat_stream)

    seen: list[str] = []
    try:
        client.chat(
            [{"role": "user", "content": "hi"}],
            stream_callback=lambda evt: seen.append(evt.delta_text),
        )
        raised = None
    except LLMError as exc:
        raised = exc
    assert raised is not None and "duplicate output" in str(raised)
    assert seen == ["partial"], f"deltas duplicated: {seen}"
