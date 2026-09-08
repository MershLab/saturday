"""Auto-delegation: send work to the resource the user already has for it.

Tiers, in the order they are tried: a local model, a free endpoint, a CLI
the user already installed and signed in to, then a metered API. The
ordering is about matching work to a tool the user has already chosen,
not about extracting volume from a flat fee - and the distinction is not
cosmetic, because tier 2 delegates by SPAWNING each vendor's own CLI, so
every request goes through that vendor's own client, its own
authentication and its own limits. Saturday never holds a subscription
credential and never speaks a vendor's API on a subscription's behalf;
`tests/test_no_borrowed_credentials.py` is what keeps that true.

Quota is observed, not declared: nobody knows their remaining quota as a
number, so an agent stays available until its own client reports a real
limit, and then Saturday BACKS OFF for an hour rather than retrying. A
router that treats someone else's rate limit as an obstacle to route
around is the thing this deliberately is not.

The tier ladder answers "what can this turn afford"; task_complexity()
answers a different question, "does this turn need judgment" - and only
the second one is allowed to reach past the cheapest tier. A task that
reads as mechanical (open X, go to X) is left on the ladder above exactly
as before. A task that reads as a real decision (why, refactor, debug,
compare) is ranked instead by capability: a real fetched price when one
exists (a flagship costs more than its own vendor's mini variant - that
is a fact, not a guess), or failing that a naming-convention hint shared
by nearly every vendor's lineup (pro/opus/max/reasoner vs
mini/flash/lite/haiku), weighted below a real price and used only when no
real price is known for anything in the pool. Nothing here is a list of
model names: both signals are read off whatever the provider or the
model's own name says today, so a new model generation needs no release.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

LOCAL, FREE, SUBSCRIPTION, METERED = 0, 1, 2, 3
TIER_NAMES = {LOCAL: "local", FREE: "free", SUBSCRIPTION: "subscription", METERED: "metered"}

# Tier 2 means "a CLI the user installed and signed in to", reached by running
# that CLI. It does not mean cheap capacity to prefer on price: several vendors
# restrict what a consumer subscription may be used for, and honouring that is
# the CLI's job, which is exactly why Saturday delegates to it instead of
# reimplementing its client.
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

TRIVIAL, STANDARD, COMPLEX = "trivial", "standard", "complex"

# Mechanical, single-action verbs: the shape of "open X" / "go to X" - doing
# is all that is asked, there is no judgment call in what to do. Kept short
# and unambiguous on purpose; anything not clearly this stays STANDARD rather
# than being guessed into TRIVIAL.
_MECHANICAL_RX = re.compile(
    r"\b(open|launch|start|close|quit|go to|navigate to|switch to|show|"
    r"list|check|ping|screenshot|click|scroll|type)\b",
    re.IGNORECASE,
)

# Vocabulary that shows up when a real decision or synthesis is being asked
# for, not just an action carried out: comparing options, explaining why,
# designing something, or fixing something whose cause is not yet known.
_DECISION_RX = re.compile(
    r"\b(why|design|architecture|refactor|decide|evaluate|compare|"
    r"trade-?offs?|review|analy[sz]e|debug|diagnose|investigate|"
    r"root cause|plan|strateg(?:y|ize)|recommend|optimi[sz]e|"
    r"should i|which is better|pros and cons|best approach)\b",
    re.IGNORECASE,
)

_MULTI_STEP_RX = re.compile(r"\b(and then|after that|step \d)|(?:^|\n)\s*[1-9][.)]\s", re.IGNORECASE)


def task_complexity(text: str) -> str:
    """trivial / standard / complex, from cheap textual signals alone.

    A heuristic proxy for how much judgment a task needs, not a judgment of
    the task itself: it never calls a model and never leaves the process, so
    it adds no cost and no latency to a decision "auto" makes on every turn.
    Deliberately conservative - only a task that clearly reads as mechanical
    or clearly reads as a real decision moves off STANDARD, which keeps
    today's cheapest-first ladder as the default for everything in between,
    exactly as it behaved before this existed."""
    t = (text or "").strip()
    if not t:
        return STANDARD
    words = t.split()
    score = 0
    if _DECISION_RX.search(t):
        score += 3
    if _MECHANICAL_RX.search(t) and len(words) <= 12:
        score -= 2
    if len(words) > 40:
        score += 2
    if "```" in t:
        score += 2
    if _MULTI_STEP_RX.search(t):
        score += 1
    if "?" in t and len(words) > 6:
        score += 1
    if score <= -2:
        return TRIVIAL
    if score >= 3:
        return COMPLEX
    return STANDARD


# Naming-convention "weight class", shared by nearly every vendor's lineup:
# opus/sonnet/haiku, pro/flash, max/mini, v4-pro/v4-flash. Not a list of
# models - a list of the adjectives vendors use to mark a tier, which is a
# far more stable signal than any specific model name and does not go stale
# when the next generation ships. Used only as a fallback when no real price
# is known for anything in the pool (see _rank): a guess from the name is
# weighted below a fetched price, never above it.
_STRONG_NAME_HINTS = ("opus", "ultra", "max", "large", "-pro", "flagship", "thinking", "reasoner", "reasoning")
_WEAK_NAME_HINTS = ("nano", "mini", "lite", "flash", "small", "haiku", "tiny", "-fast")


def _naming_tier_hint(model: str) -> float:
    m = model.lower()
    if any(h in m for h in _STRONG_NAME_HINTS):
        return 0.6
    if any(h in m for h in _WEAK_NAME_HINTS):
        return 0.2
    return 0.0


@dataclass
class Candidate:
    agent: str
    tier: int
    installed: bool
    enabled: bool
    ema_success: float = 0.5
    n: int = 0
    custom: bool = False
    # USD per million tokens (input+output averaged), from a real fetched
    # price. 0.0 means unknown, never "known to be free" - model_pricing()
    # already returns None rather than a fake number, and this preserves
    # that: a real price outranks a naming guess, which outranks neither.
    capability: float = 0.0
    # 0.0, 0.2 or 0.6 from _naming_tier_hint when no real price exists.
    capability_guess: float = 0.0


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


_QUOTA_PHRASES = ("rate limit", "rate-limit", "quota", "usage limit",
                  "too many requests", "overloaded")
_STATUS_429 = re.compile(r"(?<!\d)429(?!\d)")
_STATUS_CONTEXT = re.compile(
    r"\b(http|https|status|code|error|response|limit|exceeded|retry)\b", re.IGNORECASE)


def looks_like_quota_error(text: str) -> bool:
    """Did the delegate report hitting its own limit?

    A bare "429" is not enough on its own. This runs over a failed
    delegation's whole output, and a delegate that ran a test suite can print
    the number for unrelated reasons ("wrote 429 lines") - which would park a
    working agent for an hour and push the work to a metered tier instead."""
    low = (text or "").lower()
    if any(k in low for k in _QUOTA_PHRASES):
        return True
    return bool(_STATUS_429.search(low) and _STATUS_CONTEXT.search(low))


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
            custom=spec.custom,
        ))
    out.sort(key=lambda c: (c.tier, -c.ema_success))
    return out


def pick(task_kind: str = "general", exclude: set[str] | None = None, tier_overrides: dict | None = None) -> str | None:
    exclude = exclude or set()
    for c in candidates(task_kind, tier_overrides):
        if c.enabled and c.installed and c.agent not in exclude and not quota_exhausted(c.agent):
            return c.agent
    return None

# --------------------------------------------------------------- models
# The router originally only chose between installed CLI agents, so a free
# endpoint or a local model - the two cheapest things a user has - could never
# be routed to. Provider models are candidates too, tiered by what they
# actually cost the user rather than by vendor.


def model_tier(provider: str, model: str) -> int:
    """Where a provider model sits on the same ladder the agents use."""
    if provider in ("ollama", "vllm"):
        return LOCAL
    if model.endswith(":free"):
        return FREE
    return METERED


def model_candidates(task_kind: str = "general", timeout: float = 8.0,
                     ignore_parked: bool = False) -> list[Candidate]:
    """Provider models that can actually run right now.

    A provider whose key cannot run paid models contributes only its free
    tier: routing to one would repeat the exact failure the catalogue exists
    to prevent - a listed model that 402s the moment it is used."""
    from saturday import catalog
    from saturday.usage import model_pricing

    out: list[Candidate] = []
    for p in catalog.providers(timeout=timeout):
        if not p.reachable or not p.usable:
            # unusable means the key cannot run anything here, free tier
            # included; routing to it would just reproduce the failure
            continue
        for m in p.models:
            tier = model_tier(p.name, m)
            if not ignore_parked and quota_exhausted(f"{p.name}:{TIER_NAMES[tier]}"):
                continue          # this provider's whole tier just failed
            ident = f"{p.name}/{m}"
            ema, n = stats(ident, task_kind)
            # model_pricing never blocks: a cold router cache answers None
            # here and refreshes behind this call, so this stays on the
            # request path without adding a network round trip to it.
            price = model_pricing(p.name, m)
            capability = ((price[0] + price[1]) / 2.0) if price else 0.0
            out.append(Candidate(agent=ident, tier=tier, installed=True,
                                 enabled=True, ema_success=ema, n=n,
                                 capability=capability,
                                 capability_guess=0.0 if capability else _naming_tier_hint(m)))
    out.sort(key=lambda c: (c.tier, -c.ema_success))
    return out


def _rank(pool: list[tuple[Candidate, str]], complexity: str) -> tuple[Candidate, str] | None:
    """Pick one candidate from an already-filtered, already-safe pool.

    complexity == STANDARD (or TRIVIAL): unchanged from before this existed -
    cheapest tier first, ties broken by the better observed record.

    complexity == COMPLEX: ranked by capability instead, so a task that reads
    as a real decision can reach past the cheapest tier for something known
    to be stronger. A real fetched price wins whenever one exists anywhere in
    the pool; a naming-convention guess is used only when nothing in the pool
    has a real price at all - it never outranks real data, and a pool with no
    signal either way falls through to the same cheapest-first order as
    everything else. Tier, enabled, installed and quota constraints are
    already baked into the pool by the caller; only the ORDER changes here."""
    if not pool:
        return None
    if complexity == COMPLEX:
        priced = [t for t in pool if t[0].capability > 0]
        if priced:
            priced.sort(key=lambda t: (-t[0].capability, -t[0].ema_success, t[0].agent))
            return priced[0]
        guessed = [t for t in pool if t[0].capability_guess > 0]
        if guessed:
            guessed.sort(key=lambda t: (-t[0].capability_guess, -t[0].ema_success, t[0].agent))
            return guessed[0]
        # nothing in the pool carries any capability signal at all: there is
        # nothing honest to rank by, so fall through rather than guess
    ranked = sorted(pool, key=lambda t: (t[0].tier, -t[0].ema_success, t[0].agent))
    return ranked[0]


def route(task_kind: str = "general", exclude: set[str] | None = None,
          tier_overrides: dict | None = None, timeout: float = 8.0,
          text: str = "", complexity_aware: bool = True) -> tuple[str, str] | None:
    """Pick the cheapest capable thing: ("agent", id) or ("model", provider/id).

    One ladder over both kinds, so a local model outranks a metered API even
    though one is a model and the other a CLI. Ties inside a tier go to the
    better observed record for this kind of task.

    *text* is the task itself, classified by task_complexity() purely to
    decide HOW to rank the pool below (see _rank) - it never changes WHICH
    candidates are eligible. Omitting it (the default) is exactly today's
    behavior: task_complexity("") is STANDARD, so every existing caller that
    does not pass text is unaffected. complexity_aware=False forces STANDARD
    regardless of text, for a caller (or a user's setting) that wants the
    plain cheapest-first ladder unconditionally."""
    complexity = task_complexity(text) if complexity_aware else STANDARD
    exclude = exclude or set()
    pool: list[tuple[Candidate, str]] = []
    for c in candidates(task_kind, tier_overrides):
        if c.enabled and c.installed and not quota_exhausted(c.agent):
            pool.append((c, "agent"))
    for c in model_candidates(task_kind, timeout=timeout):
        if not quota_exhausted(c.agent):
            pool.append((c, "model"))
    pool = [(c, k) for c, k in pool if c.agent not in exclude]
    picked = _rank(pool, complexity)
    if picked is None:
        # Everything is parked. Refusing here would let a stale backoff disable
        # auto entirely - and a candidate that failed 15 minutes ago is a
        # better bet than giving up, because the reason may have been fixed.
        # Retry the ladder ignoring parks, same ranking rules as above.
        retry: list[tuple[Candidate, str]] = []
        for c in candidates(task_kind, tier_overrides):
            if c.enabled and c.installed and c.agent not in exclude:
                retry.append((c, "agent"))
        for c in model_candidates(task_kind, timeout=timeout, ignore_parked=True):
            if c.agent not in exclude:
                retry.append((c, "model"))
        picked = _rank(retry, complexity)
        if picked is None:
            return None
    best, kind = picked
    return kind, best.agent

_FAIL_BACKOFF_SECONDS = 900.0


def group_of(ident: str) -> str:
    """The blast radius of a usability failure.

    "Insufficient balance" is a fact about a provider's tier, not about one
    model on it: parking a single model made auto walk 18 free models one at a
    time, failing identically each turn. Agents stay individual - one broken
    CLI says nothing about another."""
    if "/" not in ident:
        return ident                       # an agent
    provider = ident.split("/", 1)[0]
    model = ident.split("/", 1)[1]
    return f"{provider}:{TIER_NAMES[model_tier(provider, model)]}"


def mark_unusable(ident: str, reason: str = "") -> None:
    """A candidate that just failed at use time is parked for a while.

    Reuses the quota table: the distinction that matters to the router is
    "do not pick this right now", and a 402 and a rate limit mean the same
    thing from here. Learned from a real failure, because balance endpoints
    proved unreliable in both directions."""
    mark_quota_exhausted(ident)
    grp = group_of(ident)
    if grp != ident:
        mark_quota_exhausted(grp)


_UNUSABLE_PHRASES = (
    "insufficient balance", "payment required", "402",
    "invalid api key", "unauthorized", "401", "invalid_request_error",
)


def looks_unusable(text: str) -> bool:
    """Does this error mean the candidate cannot serve requests at all?"""
    low = (text or "").lower()
    return any(p in low for p in _UNUSABLE_PHRASES)

