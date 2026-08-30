"""Auto-delegation: send work to the cheapest resource that can do it.

Cost tiers, cheapest first. The one that matters is tier 2: a Claude Code
or Cursor subscription is already paid for, so using it costs nothing
extra - which no price-per-token router can represent, and which often
makes the "expensive" subscription the cheapest place to send hard work.

Quota is observed, not declared: nobody knows their remaining subscription
quota as a number, so a tier stays available until a real quota error
arrives, then backs off until the window resets.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

LOCAL, FREE, SUBSCRIPTION, METERED = 0, 1, 2, 3
TIER_NAMES = {LOCAL: "local", FREE: "free", SUBSCRIPTION: "subscription", METERED: "metered"}

# external CLIs are normally subscription-billed; providers vary by how they charge
_DEFAULT_TIERS = {
    "claude-code": SUBSCRIPTION,
    "codex": SUBSCRIPTION,
    "cursor": SUBSCRIPTION,
    "antigravity": SUBSCRIPTION,
    "gemini": SUBSCRIPTION,
    "opencode": SUBSCRIPTION,
    # providers, not CLIs - inert until candidates() also considers providers
    "ollama": LOCAL,
    "vllm": LOCAL,
}

QUOTA_BACKOFF_SECONDS = 3600.0


@dataclass
class Candidate:
    agent: str
    tier: int
    installed: bool
    enabled: bool
    ema_success: float = 0.5
    n: int = 0


def _db_path() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "routing.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_stats (
          agent TEXT NOT NULL, task_kind TEXT NOT NULL,
          ema_success REAL DEFAULT 0.5, ema_latency REAL DEFAULT 0.0,
          n INTEGER DEFAULT 0, last_note TEXT,
          PRIMARY KEY (agent, task_kind)
        );
        CREATE TABLE IF NOT EXISTS quota_state (
          agent TEXT PRIMARY KEY, exhausted_at REAL
        );
        """
    )
    return con


def tier_of(agent: str, overrides: dict | None = None, spec=None) -> int:
    if overrides and agent in overrides:
        return int(overrides[agent])
    if spec is not None and getattr(spec, "tier", None) is not None:
        return int(spec.tier)
    # OpenRouter and friends mark no-cost models with a :free suffix
    if spec is not None and getattr(spec, "model", "").endswith(":free"):
        return FREE
    if spec is not None and getattr(spec, "provider", ""):
        return _DEFAULT_TIERS.get(spec.provider, METERED)
    return _DEFAULT_TIERS.get(agent, METERED)


def enabled_agents() -> set[str]:
    """Explicitly enabled agents. Presence on PATH is availability, not permission."""
    from saturday.config import get_config_dir

    path = get_config_dir() / "agents-enabled.json"
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(a) for a in raw} if isinstance(raw, list) else set()


def set_enabled(agent: str, on: bool) -> set[str]:
    from saturday.config import get_config_dir

    path = get_config_dir() / "agents-enabled.json"
    current = enabled_agents()
    current.add(agent) if on else current.discard(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(current)), encoding="utf-8")
    return current


def quota_exhausted(agent: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    with _connect() as con:
        row = con.execute("SELECT exhausted_at FROM quota_state WHERE agent=?", (agent,)).fetchone()
    return bool(row and row[0] and now - row[0] < QUOTA_BACKOFF_SECONDS)


def mark_quota_exhausted(agent: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO quota_state(agent, exhausted_at) VALUES(?,?) "
            "ON CONFLICT(agent) DO UPDATE SET exhausted_at=excluded.exhausted_at",
            (agent, time.time()),
        )


def looks_like_quota_error(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in ("429", "rate limit", "quota", "usage limit", "too many requests"))


def record(agent: str, task_kind: str, ok: bool, latency: float = 0.0, note: str = "", alpha: float = 0.3) -> None:
    with _connect() as con:
        row = con.execute(
            "SELECT ema_success, ema_latency, n FROM agent_stats WHERE agent=? AND task_kind=?",
            (agent, task_kind),
        ).fetchone()
        prev_s, prev_l, n = row if row else (0.5, 0.0, 0)
        con.execute(
            "INSERT INTO agent_stats(agent, task_kind, ema_success, ema_latency, n, last_note) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(agent, task_kind) DO UPDATE SET "
            "ema_success=excluded.ema_success, ema_latency=excluded.ema_latency, "
            "n=excluded.n, last_note=excluded.last_note",
            (
                agent, task_kind,
                (1 - alpha) * prev_s + alpha * (1.0 if ok else 0.0),
                (1 - alpha) * prev_l + alpha * latency,
                n + 1,
                (note or "")[:500] if not ok else None,
            ),
        )


def stats(agent: str, task_kind: str) -> tuple[float, int]:
    with _connect() as con:
        row = con.execute(
            "SELECT ema_success, n FROM agent_stats WHERE agent=? AND task_kind=?", (agent, task_kind)
        ).fetchone()
    return (row[0], row[1]) if row else (0.5, 0)


def candidates(task_kind: str = "general", tier_overrides: dict | None = None) -> list[Candidate]:
    """Enabled agents, cheapest tier first, best record within a tier.

    Covers both external CLIs and provider-backed entries from agents.json."""
    from saturday.tools.external_agent import all_agents, find_binary

    enabled = enabled_agents()
    out: list[Candidate] = []
    seen: set[str] = set()
    for name, spec in all_agents().items():
        if spec.id in seen:  # skip aliases pointing at an already-listed spec
            continue
        seen.add(spec.id)
        ema, n = stats(name, task_kind)
        out.append(Candidate(
            agent=name,
            tier=tier_of(name, tier_overrides, spec),
            installed=True if spec.is_provider else find_binary(spec) is not None,
            enabled=name in enabled,
            ema_success=ema,
            n=n,
        ))
    out.sort(key=lambda c: (c.tier, -c.ema_success))
    return out


def pick(task_kind: str = "general", exclude: set[str] | None = None, tier_overrides: dict | None = None) -> str | None:
    exclude = exclude or set()
    for c in candidates(task_kind, tier_overrides):
        if c.enabled and c.installed and c.agent not in exclude and not quota_exhausted(c.agent):
            return c.agent
    return None
