"""One catalogue of everything you can send a turn to.

Two things could answer a prompt long before this module existed - a provider
model and an external CLI agent - but they were listed in two different places
by two different commands, and the web UI listed neither. Its model menu only
ever offered what you had already used, so a freshly configured key exposed no
way to discover what it had actually bought you.

This is the single source both surfaces read, so the CLI and the GUI cannot
drift apart the way `--help` and the slash registry once did.

Discovery hits the network, so results are cached briefly; `refresh=True`
forces a re-probe when the user explicitly asks for one.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from saturday.config import PROVIDERS

# providers that serve local models and so need no API key to be usable
KEYLESS = ("ollama", "vllm")

_CACHE: dict[str, object] = {}
_CACHE_TTL = 120.0


@dataclass
class ProviderEntry:
    """One provider, and what it turned out to offer."""

    name: str
    configured: bool          # a key is present (or none is needed)
    reachable: bool           # the probe actually succeeded
    detail: str               # why not, when unreachable
    models: list[str] = field(default_factory=list)
    usable: bool = True       # the key can actually run a paid model, not just list them
    note: str = ""            # why not, when it cannot


@dataclass
class AgentEntry:
    """One external CLI agent, and whether it is actually installed."""

    id: str
    available: bool
    binary: str = ""
    install_hint: str = ""
    caution: str = ""
    custom: bool = False
    tier: int | None = None
    provider: str = ""
    model: str = ""


def _usability(name: str, key: str, timeout: float) -> tuple[bool, str]:
    """Deliberately does not guess.

    An earlier version read OpenRouter's credits endpoint and treated a zero
    balance as unusable. Measured against a real completion, that was exactly
    backwards: the zero-credit OpenRouter key returns 200, while the DeepSeek
    key - which reports nothing unusual - returns 402 "Insufficient Balance".
    A balance endpoint answers a different question than "will a request
    work", and it was wrong in both directions.

    The only honest predictor is a real request, which costs money to make on
    every menu open. So usability is learned at use time instead: a failure is
    recorded by the router, which then escalates past that candidate (see
    routing.route and the auto path in webui._run_chat).
    """
    return True, ""


def _probe(name: str, timeout: float) -> ProviderEntry:
    from saturday.llm.probe import probe_connection

    profile = PROVIDERS[name]
    key = profile.resolve_api_key()
    configured = bool(key) or name in KEYLESS
    if not configured:
        return ProviderEntry(name=name, configured=False, reachable=False, detail="no key")
    try:
        ok, detail, models = probe_connection(profile, key, timeout=timeout)
    except Exception as exc:                      # a bad endpoint must not break the list
        return ProviderEntry(name=name, configured=True, reachable=False, detail=str(exc)[:120])
    usable, note = (True, "")
    if ok:
        usable, note = _usability(name, key, timeout)
    return ProviderEntry(
        name=name, configured=True, reachable=bool(ok), detail=detail,
        models=list(models or []), usable=usable, note=note,
    )


def providers(timeout: float = 8.0, only: str | None = None, refresh: bool = False) -> list[ProviderEntry]:
    """Probe configured providers and return what each one offers.

    Only providers with a key (or the keyless local ones) are probed: probing
    the other fifteen would just be fifteen guaranteed failures and a slow
    menu."""
    wanted = [only] if only else [n for n in PROVIDERS if PROVIDERS[n].resolve_api_key() or n in KEYLESS]
    key = ("providers", tuple(wanted), timeout)
    hit = _CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < _CACHE_TTL:   # type: ignore[index]
        return list(hit[1])                                         # type: ignore[index]
    if not wanted:
        out: list[ProviderEntry] = []
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            out = list(pool.map(lambda n: _probe(n, timeout), wanted))
    _CACHE[key] = (time.time(), out)
    return out


def agents() -> list[AgentEntry]:
    """Every registered external agent, with whether its binary is on PATH."""
    from saturday.tools.external_agent import all_agents, find_binary

    out: list[AgentEntry] = []
    for aid, spec in sorted(all_agents().items()):
        # a provider-backed agent runs through Saturday, so it needs no binary
        binary = "" if spec.is_provider else (find_binary(spec) or "")
        out.append(AgentEntry(
            id=aid,
            available=bool(binary) or spec.is_provider,
            binary=binary,
            install_hint=spec.install_hint,
            caution=spec.caution,
            custom=spec.custom,
            tier=spec.tier,
            provider=spec.provider,
            model=spec.model,
        ))
    return out


def catalog(timeout: float = 8.0, refresh: bool = False) -> dict:
    """Everything that can take a turn, in one payload for both surfaces."""
    provs = providers(timeout=timeout, refresh=refresh)
    ags = agents()
    return {
        "providers": [asdict(p) for p in provs],
        "agents": [asdict(a) for a in ags],
        "model_count": sum(len(p.models) for p in provs),
        "agent_count": sum(1 for a in ags if a.available),
    }
