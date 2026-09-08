"""Local usage accounting: one JSONL line per completed turn.

Local-first telemetry — the opposite of phone-home: records stay in
CONFIG_DIR/usage.jsonl and power the Settings > About stats (tokens by day,
per-model totals). Nothing here leaves the machine.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
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


# Providers that publish per-model prices, so the 400+ models they route to do
# not each need an entry in the table above. {provider: (url, TTL seconds)}.
LIVE_PRICING: dict[str, str] = {
    "openrouter": "https://openrouter.ai/api/v1/models",
}
_PRICING_TTL = 6 * 3600.0
# {provider: (fetched_at, {model_id: (usd_per_1M_in, usd_per_1M_out)})}
_PRICING_CACHE: dict[str, tuple[float, dict[str, tuple[float, float]]]] = {}
_PRICING_INFLIGHT: set[str] = set()
_PRICING_LOCK = threading.Lock()


def _fetch_live_pricing(provider: str, url: str) -> None:
    """Populate the cache from a provider's own price list. Runs off the turn."""
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            rows = json.loads(r.read()).get("data") or []
    except Exception:
        rows = []
    table: dict[str, tuple[float, float]] = {}
    for row in rows:
        mid = str((row or {}).get("id") or "")
        pr = (row or {}).get("pricing") or {}
        try:
            # published per token; the table above is per million
            pin = float(pr.get("prompt")) * 1e6
            pout = float(pr.get("completion")) * 1e6
        except (TypeError, ValueError):
            continue
        if mid:
            table[mid] = (pin, pout)
    with _PRICING_LOCK:
        # an empty result is still an answer: cache it so a provider that is
        # down is retried on the TTL rather than on every single turn
        _PRICING_CACHE[provider] = (time.time(), table)
        _PRICING_INFLIGHT.discard(provider)


def model_pricing(provider: str, model: str) -> tuple[float, float] | None:
    """(USD per million input tokens, USD per million output tokens); None when
    the price is not known (never a fake number).

    The static table is consulted first: it is offline, instant, and covers the
    direct providers. For a router that publishes prices for hundreds of models,
    the answer is fetched from the provider itself so a new model or a price
    change needs no release here.

    That fetch NEVER happens on the turn: this is called per step to enforce
    max_run_cost_usd, and a synchronous request would put a network round trip
    in front of every one. A cold cache reports "unknown" and refreshes behind
    the caller, so the first turn is honest rather than slow."""
    haystack = f"{provider} {model}".lower()
    for needle, price in MODEL_PRICING:
        if needle in haystack:
            return price

    url = LIVE_PRICING.get(provider)
    if not url:
        return None
    with _PRICING_LOCK:
        hit = _PRICING_CACHE.get(provider)
        fresh = bool(hit) and (time.time() - hit[0]) < _PRICING_TTL
        if not fresh and provider not in _PRICING_INFLIGHT:
            _PRICING_INFLIGHT.add(provider)
            threading.Thread(target=_fetch_live_pricing, args=(provider, url),
                             daemon=True).start()
    if not hit:
        return None            # cold: unknown this turn, known by the next
    return hit[1].get(model)   # stale entries still beat no number at all


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
