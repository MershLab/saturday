"""Local usage accounting: one JSONL line per completed turn.

Local-first telemetry — the opposite of phone-home: records stay in
CONFIG_DIR/usage.jsonl and power the Settings > About stats (tokens by day,
per-model totals). Nothing here leaves the machine.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

USAGE_FILE = "usage.jsonl"
DAYS_SHOWN = 14

# List-price estimates in USD per million tokens (input, output) for cost
# surfacing in the About pane. Matched by substring against provider/model;
# unknown models simply report no estimate (never a fake number).
MODEL_PRICING: list[tuple[str, tuple[float, float]]] = [
    ("gpt-5", (1.25, 10.0)),
    ("gpt-4o", (2.50, 10.0)),
    ("o4-mini", (1.10, 4.40)),
    ("claude-opus", (15.0, 75.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (0.80, 4.0)),
    ("gemini-3-flash", (0.30, 2.50)),
    ("gemini", (1.25, 10.0)),
    ("deepseek-reasoner", (0.55, 2.19)),
    ("deepseek-chat", (0.27, 1.10)),
    ("deepseek-r1", (0.55, 2.19)),
    ("grok", (3.0, 15.0)),
    ("mistral-large", (2.0, 6.0)),
    ("llama-3.3-70b", (0.59, 0.79)),
    ("kimi", (0.60, 2.50)),
    ("qwen", (1.60, 6.40)),
    ("glm-", (0.60, 2.20)),
    ("hermes", (0.80, 2.40)),
]


def estimate_cost_usd(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Best-effort list-price estimate; None when the model is unknown."""
    pin, pout = model_pricing(provider, model) or (None, None)
    if pin is None:
        return None
    return round(prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout, 6)


def model_pricing(provider: str, model: str) -> tuple[float, float] | None:
    """(USD per million input tokens, USD per million output tokens); None when
    the model is not in the local list-price table (never a fake number)."""
    haystack = f"{provider} {model}".lower()
    for needle, price in MODEL_PRICING:
        if needle in haystack:
            return price
    return None


def _path() -> Path:
    from saturday.config import CONFIG_DIR

    return CONFIG_DIR / USAGE_FILE


def record_usage(
    *,
    provider: str,
    model: str,
    session: str = "",
    steps: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    stop_reason: str = "",
) -> None:
    entry = {
        "ts": time.time(),
        "day": time.strftime("%Y-%m-%d"),
        "provider": provider,
        "model": model,
        "session": session,
        "steps": steps,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "stop_reason": stop_reason,
    }
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_entries(limit_days: int = DAYS_SHOWN) -> list[dict[str, Any]]:
    """Entries from the last N days (older lines are ignored, not deleted)."""
    p = _path()
    if not p.is_file():
        return []
    cutoff = time.time() - limit_days * 86_400
    out: list[dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(e, dict) and float(e.get("ts") or 0) >= cutoff:
                out.append(e)
    except OSError:
        return []
    return out


def usage_summary(limit_days: int = DAYS_SHOWN) -> dict[str, Any]:
    """Aggregate for the About pane / metrics endpoint.

    ``limit_days`` controls the entry window AND the day-bucket cap; the
    est-cost label stays "14d" only at the default window."""
    entries = load_entries(limit_days=limit_days)
    by_day: dict[str, int] = defaultdict(int)
    by_model: dict[str, int] = defaultdict(int)
    by_provider: dict[str, int] = defaultdict(int)
    stops: dict[str, int] = defaultdict(int)
    cost = 0.0
    cost_known = False
    turns = 0
    total_tokens = 0
    done_turns = 0
    for e in entries:
        day = str(e.get("day") or "")
        model = f"{e.get('provider', '?')}/{e.get('model', '?')}"
        provider = str(e.get("provider") or "?")
        toks = int(e.get("total_tokens") or 0)
        by_day[day] += toks
        by_model[model] += toks
        by_provider[provider] += 1
        stop = str(e.get("stop_reason") or "?")
        stops[stop] += 1
        if stop == "done":
            done_turns += 1
        turns += 1
        total_tokens += toks
        est = estimate_cost_usd(
            str(e.get("provider") or ""),
            str(e.get("model") or ""),
            int(e.get("prompt_tokens") or 0),
            int(e.get("completion_tokens") or 0),
        )
        if est is not None:
            cost_known = True
            cost += est
    days = [{"day": d, "tokens": by_day[d]} for d in sorted(by_day)][-limit_days:]
    models = sorted(by_model.items(), key=lambda kv: -kv[1])[:8]
    return {
        "turns": turns,
        "total_tokens": total_tokens,
        "est_cost_usd_14d": round(cost, 4) if cost_known else None,
        # completion health: share of turns that finished with a real answer
        "success_rate": round(done_turns / turns, 3) if turns else None,
        "avg_tokens_per_turn": int(total_tokens / turns) if turns else 0,
        "stop_reasons": dict(sorted(stops.items(), key=lambda kv: -kv[1])),
        "providers": [
            {"provider": p, "turns": n} for p, n in sorted(by_provider.items(), key=lambda kv: -kv[1])
        ],
        "days": days,
        "models": [{"model": m, "tokens": t} for m, t in models],
    }


def render_metrics_text() -> str:
    """Plain-text metrics for the /metrics slash command (repl + webui)."""
    s = usage_summary()
    if not s["turns"]:
        return "(no usage recorded in the last 14 days)"
    rate = f"{round(s['success_rate'] * 100)}%" if s["success_rate"] is not None else "?"
    lines = [
        f"metrics (14d): {s['turns']} turns · {s['total_tokens']:,} tokens · "
        f"{rate} completed · ~{s['avg_tokens_per_turn']:,} tokens/turn"
    ]
    if s.get("est_cost_usd_14d") is not None:
        lines[0] += f" · ~${s['est_cost_usd_14d']:.2f} est."
    if s.get("stop_reasons"):
        lines.append("outcomes: " + ", ".join(f"{k} {v}" for k, v in s["stop_reasons"].items()))
    if s.get("models"):
        lines.append(
            "top models: "
            + ", ".join(f"{m['model']} {m['tokens']:,}" for m in s["models"][:5])
        )
    lines.append("(local only — nothing leaves this machine)")
    return "\n".join(lines)
