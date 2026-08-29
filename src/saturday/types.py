from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


def _uid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class ToolCall:
    id: str = field(default_factory=_uid)
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments)},
        }

    @classmethod
    def from_openai(cls, raw: dict[str, Any]) -> "ToolCall":
        fn = raw.get("function", {})
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": args}
        return cls(id=raw.get("id") or _uid(), name=fn.get("name", ""), arguments=args or {})


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    usage: Usage | None = None

    def to_openai(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            out["content"] = self.content
        if self.tool_calls:
            out["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        if self.role == "tool":
            out["tool_call_id"] = self.tool_call_id or ""
            if self.name:
                out["name"] = self.name
        return out

    @classmethod
    def from_openai(cls, raw: dict[str, Any], usage: Usage | None = None) -> "Message":
        reasoning_content = raw.get("reasoning_content") or raw.get("reasoning")
        if not reasoning_content:
            # OpenRouter: when reasoning_details is present the flat
            # "reasoning" field is ignored — join the detail texts
            details = raw.get("reasoning_details")
            if isinstance(details, list):
                texts = [d.get("text", "") for d in details if isinstance(d, dict) and d.get("text")]
                reasoning_content = "\n".join(texts) or None
        content = raw.get("content")
        if content is None:
            # strict OpenAI models return refusal, not content
            content = raw.get("refusal")
        reasoning = reasoning_content
        tool_calls = [ToolCall.from_openai(tc) for tc in raw.get("tool_calls") or []]
        if not reasoning and isinstance(content, str) and "<think>" in content:
            match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
            if match:
                reasoning = match.group(1).strip()
                content = (content[: match.start()] + content[match.end():]).strip()
        elif not reasoning and isinstance(content, str) and "<｜Assistant｜>" in content and "</think>" in content:
            start = content.find("<｜Assistant｜>") + len("<｜Assistant｜>")
            end = content.find("</think>")
            if end < start:  # malformed: closer before opener -> leave untouched
                return cls(role="assistant", content=content, tool_calls=tool_calls, reasoning=reasoning, usage=usage)
            reasoning = content[start:end].strip()
            tail = content[end + len("</think>"):]
            tail = tail.replace("<｜tool▁calls▁begin｜>", "").replace("<｜tool▁calls▁end｜>", "")
            content = tail.strip()
        return cls(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning=reasoning,
            usage=usage,
        )


@dataclass
class ToolResult:
    call_id: str
    name: str
    ok: bool
    output: str
    error: str | None = None
    images: list[str] = field(default_factory=list)

    def render(self) -> str:
        head = f"tool:{self.name} -> {'ok' if self.ok else 'error'}"
        body = self.output if self.ok else (self.error or self.output)
        return f"[{head}] {body}"


@dataclass
class Step:
    index: int
    assistant: Message
    results: list[ToolResult] = field(default_factory=list)
    # Exact tool-role messages as appended to the live history (same
    # render_tool_response format the model actually saw). Exported
    # trajectories must be byte-faithful to the observed context, so
    # Trajectory.messages() prefers these over re-rendering from results.
    tool_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Trajectory:
    task: str
    system_prompt: str
    steps: list[Step] = field(default_factory=list)
    final_answer: str | None = None
    stop_reason: Literal["done", "max_steps", "budget", "error"] | None = None
    usage: Usage = field(default_factory=Usage)
    reward: float | None = None
    seed_user_message: dict[str, Any] | None = None

    def messages(self) -> list[dict[str, Any]]:
        first_user = self.seed_user_message or {"role": "user", "content": self.task}
        msgs: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}, dict(first_user)]
        for step in self.steps:
            m = step.assistant.to_openai()
            if step.assistant.reasoning:
                m["reasoning"] = step.assistant.reasoning
            msgs.append(m)
            if step.tool_messages:
                # byte-faithful replay of what the live loop appended
                msgs.extend(dict(tm) for tm in step.tool_messages)
                continue
            for r in step.results:
                msgs.append({"role": "tool", "tool_call_id": r.call_id, "name": r.name, "content": r.render()})
        return msgs

    def to_jsonl_record(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "system": self.system_prompt,
            "messages": self.messages(),
            "final_answer": self.final_answer,
            "stop_reason": self.stop_reason,
            "reward": self.reward,
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "total_tokens": self.usage.total_tokens,
            },
        }
