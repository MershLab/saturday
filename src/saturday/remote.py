"""Reach a local Saturday from a phone without opening a port.

The server stays bound to loopback; a tunnel process connects outbound and
forwards back down that connection, which is what makes this work behind
NAT and firewalls with no port forwarding. The access token still gates
every request, so a leaked URL alone reaches nothing.

cloudflared needs no account but terminates TLS at its edge; tailscale
needs an account but is end-to-end encrypted.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

_URL_RX = re.compile(r"https://[a-zA-Z0-9._-]+\.(?:trycloudflare\.com|ts\.net)\S*")

INSTALL_HINTS = {
    "cloudflared": "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
    "tailscale": "https://tailscale.com/download",
}


@dataclass
class Tunnel:
    url: str
    host: str
    provider: str
    proc: subprocess.Popen | None = None

    def close(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def available_providers() -> list[str]:
    return [p for p in ("cloudflared", "tailscale") if shutil.which(p)]


def _argv(provider: str, port: int) -> list[str]:
    if provider == "cloudflared":
        return ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"]
    if provider == "tailscale":
        return ["tailscale", "funnel", "--bg=false", str(port)]
    raise ValueError(f"unknown tunnel provider {provider!r}")


def start_tunnel(provider: str, port: int, timeout: float = 45.0) -> Tunnel:
    """Spawn the tunnel, block until it prints its public URL."""
    if not shutil.which(provider):
        hint = INSTALL_HINTS.get(provider, "")
        raise RuntimeError(f"{provider} is not installed" + (f" - see {hint}" if hint else ""))

    proc = subprocess.Popen(
        _argv(provider, port),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    found: dict[str, str] = {}
    tail: list[str] = []

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip())
            del tail[:-40]
            if "url" not in found:
                m = _URL_RX.search(line)
                if m:
                    found["url"] = m.group(0).rstrip("/ \t|")

    threading.Thread(target=read, daemon=True).start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if "url" in found:
            url = found["url"]
            return Tunnel(url=url, host=urlparse(url).netloc, provider=provider, proc=proc)
        if proc.poll() is not None:
            break
        time.sleep(0.2)

    proc.terminate()
    # surface the tunnel's own words; "tunnel failed" alone is unfixable
    detail = "\n".join(tail[-12:]) or "(no output)"
    raise RuntimeError(f"{provider} did not report a public URL within {timeout:.0f}s:\n{detail}")


def qr_lines(text: str) -> list[str]:
    """QR via the qrencode binary, or [] when unavailable."""
    if not shutil.which("qrencode"):
        return []
    try:
        r = subprocess.run(
            ["qrencode", "-t", "UTF8", "-m", "1", text],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return r.stdout.splitlines() if r.returncode == 0 else []
