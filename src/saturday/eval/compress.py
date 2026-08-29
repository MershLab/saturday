"""Token-targeted trajectory compression for SFT export (hermes parity).

Fine-tuning datasets don't need every 30 KB tool result: older observations
are replaced by short omission markers while the system prompt, the goal,
recent turns, and every final answer stay verbatim. Pure function over an
export record; stdlib-only.
"""
from __future__ import annotations

from typing import Any

from saturday.agent.memory import estimate_tokens

OMIT_MARKER_MIN_CHARS = 240  # shorter results are kept as-is


def _omit(content: str, name: str) -> str:
    head = content[:120].replace("\n", " ")
    return f"[{name} result omitted during export compression: {len(content)} chars] {head}"


def compress_record(record: dict[str, Any], token_budget: int) -> dict[str, Any]:
    """Shrink ``record["messages"]`` toward ``token_budget``.

    Strategy: walk messages oldest->newest, replacing oversized TOOL results
    with markers until the estimate fits or nothing replaceable remains. The
    seed user message (# Goal) and everything after the halfway point of the
    original transcript are never touched, so recent context stays faithful.
    """
    msgs = record.get("messages")
    if not isinstance(msgs, list) or len(msgs) < 4 or token_budget <= 0:
        return record

    out = [dict(m) for m in msgs]
    est = sum(estimate_tokens(_text_of(m)) for m in out)
    if est <= token_budget:
        return record

    protected_tail = max(4, len(out) // 2)
    for i, m in enumerate(out):
        if est <= token_budget:
            break
        if i >= len(out) - protected_tail:
            break
        if m.get("role") != "tool":
            continue
        text = _text_of(m)
        if len(text) < OMIT_MARKER_MIN_CHARS:
            continue
        replacement = _omit(text, str(m.get("name") or "tool"))
        saved = estimate_tokens(text) - estimate_tokens(replacement)
        out[i] = dict(m, content=replacement)
        est -= max(saved, 0)

    compressed = dict(record)
    compressed["messages"] = out
    meta = dict(record.get("compression") or {})
    meta.update(
        {
            "budget_tokens": token_budget,
            "before_tokens": sum(estimate_tokens(_text_of(m)) for m in msgs),
            "after_tokens": est,
        }
    )
    compressed["compression"] = meta
    return compressed


def _text_of(message: dict[str, Any]) -> str:
    c = message.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(
            str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("type") == "text"
        )
    calls = message.get("tool_calls") or []
    return " ".join(str((tc.get("function") or {}).get("arguments") or "") for tc in calls)
