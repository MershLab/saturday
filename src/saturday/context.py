"""Context-window accounting: what is consuming the model's context budget.

Pure stdlib estimation (chars/4 heuristic shared with agent.memory). Produces a
breakdown dict used by ``GET /api/context``, the webui Context panel and the
``/context`` slash command: per-section token estimates, message counts, image
costs, and headroom against both the compaction threshold and model budget.
"""
from __future__ import annotations

import json
from typing import Any

from saturday.agent.memory import IMAGE_TOKEN_COST, estimate_message_tokens, estimate_tokens

ROLES = ("system", "user", "assistant", "tool")


def pct(n: float, base: float) -> float:
    return round(100.0 * n / base, 1) if base else 0.0


# hermes/opencode parity: the compaction threshold is meaningless against a
# wrong window. Zero-dep resolution order: explicit cfg override (anything
# other than DEFAULT_CONTEXT_TOKENS counts as intentional) -> SATURDAY_MODEL_CONTEXT
# env -> conservative substring table for well-known families -> default.
DEFAULT_CONTEXT_TOKENS = 96_000

MINIMUM_CONTEXT_TOKENS = 8_192

# --------------------------------------------------------------- live probe
# Many OpenAI-compatible backends PUBLISH the true window on GET /models:
#   vLLM:      {"data": [{"id": ..., "max_model_len": 65536}]}
#   OpenRouter: {..., "context_length": 200000}
#   Groq:       {..., "context_window": 131072}
# Asking beats guessing, so resolution order is:
#   explicit cfg -> env override -> LIVE PROVIDER PROBE -> hint table -> default
# The probe is gated (needs a key, a private/local endpoint, or openrouter),
# cached ~10min per (base_url, model), and never raises.

_MODEL_CONTEXT_HINTS: tuple[tuple[str, int], ...] = (
    ("gemini", 1_000_000),
    ("claude", 200_000),
    ("deepseek", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4.1", 128_000),
    ("qwen3-coder", 262_144),
    ("kimi-k2", 262_144),
    ("glm-5", 204_800),
)

_PROBE_CACHE: dict[tuple[str, str], tuple[float, int | None]] = {}
_PROBE_TTL = 600.0
_WINDOW_FIELDS = ("max_model_len", "context_length", "context_window")


def _host_is_local(hostname: str | None) -> bool:
    if not hostname:
        return True
    return hostname in ("localhost", "::1") or hostname.startswith("127.") or hostname.startswith("192.168.") or hostname.startswith("10.")


def _probe_provider_window(provider: str, model: str) -> int | None:
    import json as _json
    import os
    import time as _time
    import urllib.parse
    import urllib.request

    from saturday.config import PROVIDERS

    prof = PROVIDERS.get(provider)
    if prof is None:
        return None
    if os.environ.get("SATURDAY_NO_CONTEXT_PROBE", ""):
        return None
    base = prof.resolve_base_url().rstrip("/")
    key = prof.resolve_api_key()
    host = urllib.parse.urlparse(base).hostname
    allowed = bool(key) or provider == "openrouter" or _host_is_local(host)
    if not allowed:
        return None  # hosted + keyless: probing would just 401
    ck = (base, model)
    now = _time.time()
    hit = _PROBE_CACHE.get(ck)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]
    window: int | None = None
    try:
        req = urllib.request.Request(base + "/models")
        if key:
            if prof.api_key_header:
                req.add_header(prof.api_key_header, key)
            else:
                req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        entries = payload.get("data") if isinstance(payload, dict) else payload
        wanted = str(model or "").lower()
        stem = wanted.split("/")[-1]
        for entry in entries or []:
            eid = str((entry or {}).get("id") or "").lower()
            if not (eid == wanted or eid.split("/")[-1] == stem or eid.endswith(stem)):
                continue
            for field in _WINDOW_FIELDS:
                val = (entry or {}).get(field)
                if isinstance(val, (int, float)) and val > 0:
                    window = int(val)
                    break
            if window:
                break
    except Exception:
        window = None
    _PROBE_CACHE[ck] = (now, window)
    return window


def resolve_context_window(
    model: str | None,
    configured: int | None = None,
    provider: str | None = None,
) -> tuple[int, str]:
    """(window, source). Source ∈ config | env | provider | table | default."""
    if configured is not None and int(configured) != DEFAULT_CONTEXT_TOKENS:
        return max(MINIMUM_CONTEXT_TOKENS, int(configured)), "config"
    import os

    env = os.environ.get("SATURDAY_MODEL_CONTEXT", "")
    if env.isdigit() and int(env) > 0:
        return int(env), "env"
    if provider:
        probed = _probe_provider_window(provider, model or "")
        if probed:
            return probed, "provider"
    name = str(model or "").lower()
    for needle, window in _MODEL_CONTEXT_HINTS:
        if needle in name:
            return window, "table"
    return DEFAULT_CONTEXT_TOKENS, "default"


_AUTO_COMPACT_RATIO = 0.70   # hermes-style % of the model's real window
_EXPLICIT_CAP_RATIO = 0.90   # an explicit user threshold can go higher, not infinite


def _compact_for(cfg, window: int) -> int:
    """Auto: 70% of the resolved window. Explicit cfg value: honored, capped
    at 90% of window so reply headroom always survives."""
    raw = getattr(cfg, "compact_above_tokens", None)
    if isinstance(raw, int) and raw > 0:
        compact = min(raw, int(window * _EXPLICIT_CAP_RATIO))
    else:
        compact = int(window * _AUTO_COMPACT_RATIO)
    # sane floor for normal windows, but never let it defeat a tiny cap
    compact = max(compact, min(MINIMUM_CONTEXT_TOKENS, window // 2))
    return min(compact, window)


def effective_windows(cfg) -> tuple[int, int]:
    """(context_window, compact_above): derived from the model's real window,
    not absolute defaults."""
    window, _src = resolve_context_window(
        getattr(cfg, "model", None), getattr(cfg, "max_context_tokens", None), getattr(cfg, "provider", None)
    )
    return window, _compact_for(cfg, window)


def resolve_context_info(cfg) -> dict:
    """Full resolution for display: window, compact point, and where the
    window came from (so the UI can say 'per provider' vs 'guess')."""
    window, source = resolve_context_window(
        getattr(cfg, "model", None), getattr(cfg, "max_context_tokens", None), getattr(cfg, "provider", None)
    )
    return {
        "window": window,
        "compact": _compact_for(cfg, window),
        "source": source,
        "model_limit_known": source in ("provider", "config", "env"),
    }


def _count_images(content) -> int:
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")


def analyze_context(
    *,
    system_prompt: str = "",
    history: list[dict] | None = None,
    system_tiers: dict[str, str] | None = None,
    tool_specs: list[dict] | None = None,
    include_tool_schemas: bool = True,
    max_context_tokens: int = 96_000,
    compact_above_tokens: int = 60_000,
    max_reply_tokens: int = 8192,
) -> dict[str, Any]:
    """Build the context breakdown.

    system_tiers (stable/context/volatile) refines the system-prompt row when
    available. include_tool_schemas adds the JSON-schema cost of native
    function-calling tools (in Hermes XML mode the catalog lives inside the
    prompt already and must NOT be double-counted).
    """
    history = history or []
    sections: list[dict[str, Any]] = []

    stable = context_t = volatile = 0
    if system_tiers:
        stable = estimate_tokens(system_tiers.get("stable") or "")
        context_t = estimate_tokens(system_tiers.get("context") or "")
        volatile = estimate_tokens(system_tiers.get("volatile") or "")
    sys_total = stable + context_t + volatile or estimate_tokens(system_prompt)
    sections.append(
        {
            "key": "system",
            "label": "system prompt",
            "tokens": sys_total,
            "detail": {"stable": stable, "context": context_t, "volatile": volatile},
        }
    )

    tools_total = 0
    if include_tool_schemas and tool_specs:
        tools_total = sum(estimate_tokens(json.dumps(s)) for s in tool_specs)
    sections.append({"key": "tools", "label": "tool schemas", "tokens": tools_total, "detail": {"count": len(tool_specs or [])}})

    role_tokens = {r: 0 for r in ROLES}
    role_counts = {r: 0 for r in ROLES}
    images = 0
    image_tokens = 0
    for m in history:
        role = m.get("role")
        if role not in role_tokens:
            role = "user"
        role_counts[role] += 1
        images += _count_images(m.get("content"))
        cost = estimate_message_tokens(m)
        # estimate_message_tokens already bills each image at IMAGE_TOKEN_COST;
        # keep them as a separate visual slice.
        img_here = _count_images(m.get("content"))
        text_cost = max(0, cost - img_here * IMAGE_TOKEN_COST)
        role_tokens[role] += text_cost
        image_tokens += img_here * IMAGE_TOKEN_COST

    sections.append({"key": "user", "label": "user messages", "tokens": role_tokens["user"], "detail": {"messages": role_counts["user"]}})
    sections.append({"key": "assistant", "label": "assistant messages", "tokens": role_tokens["assistant"], "detail": {"messages": role_counts["assistant"]}})
    sections.append({"key": "tool", "label": "tool results", "tokens": role_tokens["tool"], "detail": {"messages": role_counts["tool"]}})
    sections.append({"key": "images", "label": "images in context", "tokens": image_tokens, "detail": {"count": images}})
    sections.append({"key": "reply_headroom", "label": "reserved for reply", "tokens": max_reply_tokens, "detail": {}})

    total = sum(s["tokens"] for s in sections)
    prompt_tokens = total - max_reply_tokens

    user_turns = sum(
        1
        for m in history
        if m.get("role") == "user"
        and not str(m.get("content") or "").startswith("[context was compacted")
        and "[images from tool" not in str(m.get("content") or "")
    )
    return {
        "total": total,
        "prompt_tokens": prompt_tokens,
        "budget": max_context_tokens,
        "compact_above": compact_above_tokens,
        "max_reply": max_reply_tokens,
        "usage_pct": pct(total, max_context_tokens),
        "prompt_pct": pct(prompt_tokens, compact_above_tokens),
        "will_compact": prompt_tokens > compact_above_tokens,
        "messages": {r: role_counts[r] for r in ROLES},
        "images": images,
        "user_turns": user_turns,
        "sections": [s for s in sections],
    }


def render_text(bd: dict[str, Any]) -> str:
    """Plain-text rendering for slash commands / CLI."""
    def bar(pct_value: float, width: int = 24) -> str:
        filled = int(round(width * min(1.0, pct_value / 100.0)))
        return "#" * filled + "-" * (width - filled)

    lines = [
        f"context: {bd['total']:,} tokens ({bd['usage_pct']}% of {bd['budget']:,} budget incl. reply headroom)",
        f"prompt ~{bd.get('prompt_tokens', bd['total']):,} -> {bar(bd.get('prompt_pct', 0))} {bd.get('prompt_pct', 0)}% of compaction point ({bd['compact_above']:,})",
    ]
    if bd.get("window_source"):
        lines.append(f"window: {bd['budget']:,} (from {bd['window_source']})")
    for s in bd["sections"]:
        if s["tokens"] <= 0:
            continue
        share = pct(s["tokens"], bd["total"])
        detail = ", ".join(f"{k}={v}" for k, v in (s.get("detail") or {}).items() if v)
        lines.append(f"  {s['label']:<20} {s['tokens']:>7,}  ({share:>4}%)  {detail}")
    if bd.get("will_compact"):
        lines.append("note: next step will trigger compaction")
    return "\n".join(lines)
