from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from saturday.llm.client import LLMClient, LLMError, ModelResponse, StreamEvent  # noqa: F401,E402
from saturday.types import Message, Usage  # noqa: E402


class FakeLLM:
    """Scripted offline model returning real Message objects, like the live client."""

    def __init__(self, turns: list[Message]) -> None:
        self.script = list(turns)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, temperature=0.6, top_p=0.95, max_tokens=8192, stream_callback=None, stop=None):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.script:
            raise LLMError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        # accept bare Messages (direct construction) or (msg, finish_reason)
        msg, finish_reason = item if isinstance(item, tuple) else (item, "stop")
        if stream_callback is not None:
            if msg.reasoning:
                stream_callback(StreamEvent(type="reasoning", reasoning_delta=msg.reasoning))
            if msg.content:
                stream_callback(StreamEvent(type="text", delta_text=msg.content))
        return ModelResponse(message=msg, finish_reason=finish_reason)


def assistant(content: str | None = None, reasoning: str | None = None, tool_calls: list[tuple[str, dict]] | None = None, usage: Usage | None = None) -> Message:
    calls = [
        __import__("saturday.types", fromlist=["ToolCall"]).ToolCall(id=f"call_{i}", name=n, arguments=a)
        for i, (n, a) in enumerate(tool_calls or [])
    ]
    return Message(role="assistant", content=content, reasoning=reasoning, tool_calls=calls, usage=usage)


def make_scripted_model(turns: list[dict]) -> FakeLLM:
    msgs = []
    for t in turns:
        usage = t.get("usage")
        usage_obj = Usage(prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=usage[0] + usage[1]) if usage else None
        msgs.append(
            (
                assistant(
                    content=t.get("content"),
                    reasoning=t.get("reasoning"),
                    tool_calls=[(tc["name"], tc.get("arguments", {})) for tc in t.get("tool_calls") or []],
                    usage=usage_obj,
                ),
                t.get("finish_reason", "stop"),
            )
        )
    return FakeLLM(msgs)
