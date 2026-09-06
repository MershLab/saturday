from __future__ import annotations

import json
import re
from typing import Any


def to_chatml(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m["role"]
        content = m.get("content") or ""
        if m.get("reasoning"):
            content = f"<scratch_pad>{m['reasoning']}</scratch_pad>\n{content}"
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", tc)
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                calls.append(json.dumps({"name": fn.get("name"), "arguments": args}))
            body = "\n".join(f"<tool_call>\n{c}\n</tool_call>" for c in calls)
            parts.append(f"<|im_start|>assistant\n{content}\n{body}<|im_end|>")
            continue
        if role == "tool":
            name = m.get("name", "tool")
            wrapped = f"<tool_response>\n{content}\n</tool_response>"
            parts.append(f"<|im_start|>tool ({name})\n{wrapped}<|im_end|>")
            continue
        parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
    parts.append("<|im_start|>assistant\n")
    return "\n".join(parts)


_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_SCRATCH_RE = re.compile(r"<scratch_pad>(.*?)</scratch_pad>", re.DOTALL)
_OPEN_THINK_RE = re.compile(r"<think>(.*)$", re.DOTALL)
_OPEN_SCRATCH_RE = re.compile(r"<scratch_pad>(.*)$", re.DOTALL)


def split_reasoning(text: str) -> tuple[str | None, str]:
    for closed, opener in ((_THINK_RE, _OPEN_THINK_RE), (_SCRATCH_RE, _OPEN_SCRATCH_RE)):
        m = closed.search(text)
        if m:
            reasoning = m.group(1).strip()
            remainder = (text[: m.start()] + text[m.end():]).strip()
            return (reasoning or None), remainder
    m = _OPEN_THINK_RE.search(text) or _OPEN_SCRATCH_RE.search(text)
    if m:
        return m.group(1).strip() or None, ""
    return None, text


RETRY_HINT = (
    "There was an error executing this tool call. Please check the arguments against the "
    "tool schema and call the function again with correct arguments within <tool_call></tool_call> tags."
)


NATIVE_RETRY_HINT = (
    "That tool call failed. Check the arguments against the tool's schema and call it again."
)


def render_tool_response(name: str, ok: bool, payload: str, native: bool = False) -> str:
    """Wrap a tool result for the wire.

    The Hermes XML envelope is only meaningful to a model being driven through
    the XML protocol. Native function-calling models already receive the result
    in a typed tool message, so wrapping cost double for nothing: json.dumps
    escapes every newline, quote and tab, and with ensure_ascii on, 800
    characters of CJK became 4,869 of six-character escapes, which also
    tokenise far worse than the text they replaced. The XML retry hint is
    worse than useless there
    too, since it tells a native model to answer in <tool_call> tags.
    """
    if native:
        return payload if ok else f"{payload}\n{NATIVE_RETRY_HINT}"
    body = f'{{"name": "{name}", "error": {json.dumps(payload)}}}' if not ok else f'{{"name": "{name}", "content": {json.dumps(payload)}}}'
    if not ok:
        body += f"\n{RETRY_HINT}"
    return f"<tool_response>\n{body}\n</tool_response>"
