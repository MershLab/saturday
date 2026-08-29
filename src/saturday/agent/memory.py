from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MemoryItem:
    kind: str
    text: str


@dataclass
class WorkingMemory:
    """Long-horizon scratchpad that survives context compaction.

    Facts, decisions, and file summaries are pinned here so the agent can
    operate past the context window without losing key state.
    """

    items: list[MemoryItem] = field(default_factory=list)
    max_chars: int = 12_000

    def add(self, kind: str, text: str) -> None:
        self.items.append(MemoryItem(kind=kind, text=text.strip()))

    def render(self) -> str:
        if not self.items:
            return "(empty)"
        blocks = [f"[{i.kind}] {i.text}" for i in self.items[-40:]]
        out = "\n".join(blocks)
        if len(out) > self.max_chars:
            out = out[-self.max_chars:]
            cut = out.find("\n")
            if cut != -1:
                out = out[cut + 1:]
        return out

    def __len__(self) -> int:
        return len(self.items)


_CJK_RX = re.compile(r"[\u1100-\u11FF\u2E80-\u9FFF\uAC00-\uD7FF\uF900-\uFAFF\uFF66-\uFFDC]")


def _cjk_count(text: str) -> int:
    return len(_CJK_RX.findall(text))


def estimate_tokens(text: str) -> int:
    # ~4 chars/token for ASCII-ish text; CJK/Hangul codepoints are ~1 token
    # each (mirrors hermes model_metadata.estimate_tokens_rough). Ceiling so
    # short strings never estimate 0.
    if not text:
        return 1
    cjk = _cjk_count(text)
    other = len(text) - cjk
    return max(1, -(-other // 4) + cjk)


IMAGE_TOKEN_COST = 800


class TokenMeter:
    """Calibrates rough char-based estimates against provider-reported usage.

    Preflight compaction checks run on estimates (no tokenizer dependency);
    every real response reports actual prompt tokens, so we maintain an EMA of
    actual/estimated and project future estimates through it (the hermes
    calibration trick). Uncalibrated meters are identity (ratio 1.0).
    """

    def __init__(self) -> None:
        self.ratio: float = 1.0
        self.samples: int = 0

    def observe(self, estimated: int, actual: int) -> None:
        if actual <= 0 or estimated <= 0:
            return
        sample = max(0.25, min(4.0, actual / estimated))
        # EMA biased toward early samples so calibration converges fast
        alpha = 0.5 if self.samples < 3 else 0.25
        self.ratio = sample if self.samples == 0 else (1 - alpha) * self.ratio + alpha * sample
        self.ratio = max(0.25, min(4.0, self.ratio))
        self.samples += 1

    @property
    def calibrated(self) -> bool:
        return self.samples > 0

    def project(self, estimated: int) -> int:
        return max(1, int(estimated * self.ratio))


def estimate_message_tokens(message: dict) -> int:
    content = message.get("content")
    total = 0
    if content is None or isinstance(content, str):
        total += estimate_tokens(content or "")
    else:
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                total += IMAGE_TOKEN_COST
            elif isinstance(part.get("text"), str):
                total += estimate_tokens(part["text"])
    # reasoning/reasoning_content ride along on persisted assistant messages
    # (deliberately kept on disk for replay/UI); ignoring their bytes made
    # compaction under-count resumed sessions and skip preflight compaction
    # until the provider itself raised context-overflow
    for key in ("reasoning", "reasoning_content"):
        blob = message.get(key)
        if isinstance(blob, str):
            total += estimate_tokens(blob)
    # tool-call payloads ride to the API inside the assistant message; ignoring
    # their argument bytes made compaction underestimate multi-tool turns
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            total += estimate_tokens(args)
        elif args is not None:
            import json as _json

            total += estimate_tokens(_json.dumps(args))
        total += estimate_tokens(str(fn.get("name") or ""))
    return max(total, 1)
