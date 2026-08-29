from __future__ import annotations

import select
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from saturday.tools.base import Tool
from saturday.tools.web import assert_public_url

INSTALL_HINT = (
    "Playwright is not installed. Enable JS-rendered browsing with:\n"
    "  pip install 'saturday[browser]' && playwright install chromium"
)


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


class _PinnedBrowserProxy:
    """Small loopback proxy that connects browser requests to validated IPs."""

    def __init__(self) -> None:
        self._pins: dict[tuple[str, int], str | None] = {}
        self._lock = threading.Lock()
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _PinnedProxyHandler)
        self._server.daemon_threads = True
        self._server.allow_reuse_address = True
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def server_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def pin(self, url: str, ip: str | None) -> None:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        with self._lock:
            self._pins[(host.lower(), port)] = ip

    def address_for(self, host: str, port: int) -> str | None:
        with self._lock:
            return self._pins.get((host.lower(), port))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class _PinnedProxyHandler(socketserver.BaseRequestHandler):
    _MAX_HEADER_BYTES = 128 * 1024

    def handle(self) -> None:
        self.request.settimeout(30)
        try:
            head, initial = self._read_headers()
            lines = head.split(b"\r\n")
            method, target, version = lines[0].decode("iso-8859-1").split(" ", 2)
            headers = [line for line in lines[1:] if line]
            header_map = self._header_map(headers)
            owner = self.server.owner  # type: ignore[attr-defined]
            if method.upper() == "CONNECT":
                host, port = self._authority(target, 443)
                self._tunnel(owner, host, port, initial)
                return

            target_url = target
            if not target_url.lower().startswith(("http://", "https://")):
                target_url = f"http://{header_map.get('host', '')}{target_url}"
            parts = urlsplit(target_url)
            host = parts.hostname
            if not host:
                raise ValueError("proxy request has no hostname")
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
            remote = self._connect(owner, host, port)
            try:
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                forwarded = [f"{method} {path} {version}".encode("iso-8859-1")]
                for line in headers:
                    name = line.split(b":", 1)[0].lower()
                    if name not in {b"proxy-connection", b"connection"}:
                        forwarded.append(line)
                forwarded.append(b"Connection: close")
                body = initial
                length = int(header_map.get("content-length", "0") or "0")
                while len(body) < length:
                    chunk = self.request.recv(min(65536, length - len(body)))
                    if not chunk:
                        break
                    body += chunk
                remote.sendall(b"\r\n".join(forwarded) + b"\r\n\r\n" + body[:length or None])
                self._relay(self.request, remote)
            finally:
                remote.close()
        except (OSError, ValueError, UnicodeError):
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass

    def _read_headers(self) -> tuple[bytes, bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(65536)
            if not chunk:
                raise OSError("proxy client closed before headers")
            data.extend(chunk)
            if len(data) > self._MAX_HEADER_BYTES:
                raise ValueError("proxy headers too large")
        end = data.index(b"\r\n\r\n")
        return bytes(data[:end]), bytes(data[end + 4 :])

    @staticmethod
    def _header_map(headers: list[bytes]) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in headers:
            if b":" in line:
                name, value = line.split(b":", 1)
                result[name.decode("iso-8859-1").lower()] = value.decode("iso-8859-1").strip()
        return result

    @staticmethod
    def _authority(authority: str, default_port: int) -> tuple[str, int]:
        parsed = urlsplit(f"//{authority}")
        if not parsed.hostname:
            raise ValueError("proxy request has no hostname")
        return parsed.hostname, parsed.port or default_port

    @staticmethod
    def _connect(owner: _PinnedBrowserProxy, host: str, port: int) -> socket.socket:
        ip = owner.address_for(host, port)
        if ip is None:
            from saturday.tools.web import _local_fetch_allowed

            if not _local_fetch_allowed():
                raise OSError("missing validated browser address")
            ip = host
        return socket.create_connection((ip, port), timeout=30)

    def _tunnel(self, owner: _PinnedBrowserProxy, host: str, port: int, initial: bytes) -> None:
        remote = self._connect(owner, host, port)
        try:
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(self.request, remote, initial)
        finally:
            remote.close()

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket, initial: bytes = b"") -> None:
        if initial:
            right.sendall(initial)
        while True:
            readable, _, _ = select.select((left, right), (), (), 30)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (right if source is left else left).sendall(data)


class PlaywrightBrowserTool(Tool):
    """JS-capable browser backed by Playwright when installed.

    Actions:
      open <url>            -> readable text of the rendered page (+ link count)
      html <url>            -> raw outerHTML snippet
      click <url> <text>    -> click first element containing text, return new page text
      screenshot <url>      -> full-page PNG attached to the next observation (vision)
    """

    name = "web_browser_js"
    description = (
        "Browse with full JavaScript rendering (Playwright). Actions: open, html, click "
        "(by visible text), screenshot. Use instead of 'browser' when pages are SPAs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["open", "html", "click", "screenshot"]},
            "url": {"type": "string"},
            "text": {"type": "string", "description": "for click: visible element text"},
            "max_chars": {"type": "integer"},
        },
        "required": ["action"],
    }

    def __init__(self, timeout_ms: int = 30_000, max_chars: int = 12_000) -> None:
        self.timeout_ms = timeout_ms
        self.max_chars = max_chars
        self._pw: Any = None
        self._ctx: Any = None
        self._proxy: _PinnedBrowserProxy | None = None
        self.pending_images: list[str] = []

    def _ensure(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(INSTALL_HINT) from exc
        if self._pw is None:
            self._pw = sync_playwright().start()
            self._proxy = _PinnedBrowserProxy()
            browser = self._pw.chromium.launch(
                headless=True, proxy={"server": self._proxy.server_url}
            )
            # Service workers can own requests without passing through context
            # routes; block them so every network request reaches the pinning
            # callback and the fail-closed local proxy.
            self._ctx = browser.new_context(
                viewport={"width": 1280, "height": 900}, service_workers="block"
            )
            # The initial URL check is not enough: Chromium follows redirects
            # and pages can navigate or load resources after JavaScript runs.
            # Apply the same public-network policy to every request in the
            # context, including requests triggered by clicks and redirects.
            self._ctx.route(
                "**/*", lambda route, request: self._route_request(route, request, self._proxy)
            )
        return self._ctx

    @staticmethod
    def _route_request(route, request, proxy: _PinnedBrowserProxy | None = None) -> None:
        url = str(getattr(request, "url", "") or "")
        scheme = urlsplit(url).scheme.lower()
        # These schemes do not initiate a network connection. They are needed
        # for inline assets and the browser's initial blank document.
        if scheme in {"about", "data", "blob"}:
            route.continue_()
            return
        try:
            validated_ip = assert_public_url(url)
        except ValueError:
            route.abort(error_code="blockedbyclient")
            return
        if proxy is not None:
            proxy.pin(url, validated_ip)
        route.continue_()

    def _page_text(self, page) -> str:
        return page.evaluate("() => document.body ? document.body.innerText : ''")

    def run(self, args: dict) -> tuple[bool, str]:
        action = args.get("action", "open")
        url = str(args.get("url") or "").strip()
        max_chars = int(args.get("max_chars") or self.max_chars)
        if not url:
            return False, "url required"
        # Dependency check BEFORE the SSRF gate so a missing install surfaces
        # as its actionable RuntimeError regardless of URL validity (the
        # import alone never launches anything).
        if not playwright_available():
            raise RuntimeError(INSTALL_HINT)
        # SSRF gate before the browser ever launches: playwright would happily
        # goto() file://, internal hosts and cloud-metadata IPs. Only public
        # http/https targets pass. The route installed in _ensure repeats this
        # check for redirects and all subsequent browser requests.
        try:
            initial_ip = assert_public_url(url)
        except ValueError as exc:
            return False, f"refused: {exc} (only public http/https URLs are allowed)"
        try:
            ctx = self._ensure()
            if self._proxy is not None:
                self._proxy.pin(url, initial_ip)
            page = ctx.new_page()
            try:
                page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
                page.wait_for_timeout(300)

                if action == "html":
                    html = page.content()
                    return True, html[:max_chars] + (f"\n...[{len(html)} total]" if len(html) > max_chars else "")

                if action == "screenshot":
                    import tempfile
                    import time as _t

                    out = Path(tempfile.gettempdir()) / f"saturday_shot_{int(_t.time()*1000)}.png"
                    page.screenshot(path=str(out), full_page=True)
                    self.pending_images = [str(out)]
                    return True, f"[screenshot attached: {out}]"

                if action == "click":
                    text = str(args.get("text") or "")
                    if not text:
                        return False, "click requires 'text' to match visible element text"
                    locator = page.get_by_text(text, exact=False).first
                    locator.click(timeout=self.timeout_ms)
                    page.wait_for_timeout(400)

                body = self._page_text(page)
                shown = body.strip()[:max_chars]
                suffix = f"\n...[{len(body)} chars total]" if len(body) > max_chars else ""
                return True, f"PAGE: {page.url}\n\n{shown}{suffix}"
            finally:
                page.close()
        except RuntimeError:
            raise
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._ctx = None
            self._pw = None
            if self._proxy is not None:
                self._proxy.close()
                self._proxy = None
