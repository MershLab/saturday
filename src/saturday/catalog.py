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
    """Can this key actually run a paid model, or only list them?

    Reachability is not usability. OpenRouter's model catalogue answers HTTP
    200 with no credentials at all, so a listing proved nothing: the menu
    happily offered 431 models on an account with zero credit, and every one
    of them failed at send time with 402. Where a provider exposes a cheap
    authenticated balance check, use it; otherwise assume usable rather than
    invent a failure."""
    if name != "openrouter":
        return True, ""
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = (_json.loads(r.read()) or {}).get("data", {})
        remaining = float(d.get("total_credits", 0) or 0) - float(d.get("total_usage", 0) or 0)
        if remaining <= 0:
            return False, "no credit on this key - only :free models will run"
        return True, ""
    except urllib.error.HTTPError as e:
        return False, f"key rejected (HTTP {e.code})"
    except Exception:
        return True, ""            # a failed check must not hide a working key


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
