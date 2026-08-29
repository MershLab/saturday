"""Shared hardening helpers for the local HTTP surfaces (serve + web UI).

The agent behind these endpoints can run arbitrary commands, so both surfaces
pin the Host header to the bound loopback address (kills DNS-rebinding) and
reject cross-origin mutating requests (kills drive-by CSRF from web pages)."""
from __future__ import annotations

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1", "[::1]")


def normalize_authority(value: str) -> str:
    """Lowercase a Host/Origin authority and drop default ports."""
    v = (value or "").strip().lower()
    if v.endswith(":80") or v.endswith(":443"):
        v = v.rsplit(":", 1)[0]
    return v.rstrip(".")


def allowed_hosts(bound_host: str, bound_port: int) -> set[str]:
    """Host-header authorities this server accepts.

    A loopback bind only ever accepts loopback names, so a rebinding attacker
    domain (whose Host header is attacker.com) can never reach the API even
    with the token disabled."""
    port = int(bound_port)
    suffixes = {f":{port}"}
    if port in (80, 443):
        suffixes.add("")
    hosts = {name + s for name in LOOPBACK_NAMES for s in suffixes}
    bh = normalize_authority(bound_host)
    if bh and bh not in LOOPBACK_NAMES and bh not in ("0.0.0.0", "::"):
        hosts |= {bh + s for s in suffixes}
    return hosts


def allowed_origins(hosts: set[str]) -> set[str]:
    out: set[str] = set()
    for h in hosts:
        out.add(f"http://{h}")
        out.add(f"https://{h}")
    return out


def authority_allowed(value: str, allowed: set[str]) -> bool:
    norm = {normalize_authority(a) for a in allowed}
    return normalize_authority(value) in norm
