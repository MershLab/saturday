from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import urllib.parse
import urllib.request
from html import unescape as _unescape
from html.parser import HTMLParser

from saturday.tools.base import Tool

# SSRF guard: web tools are model-driven, and a fetched page can instruct the
# model to fetch anything - so loopback/link-local/private/metadata targets are
# refused at this single choke point (redirects included). Opt out for local
# development endpoints with SATURDAY_ALLOW_LOCAL_FETCH=1.
_BLOCKED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fe80::/10",
        "fc00::/7",
        "ff00::/8",
    )
]


def _local_fetch_allowed() -> bool:
    return os.environ.get("SATURDAY_ALLOW_LOCAL_FETCH", "").strip().lower() in ("1", "true", "yes", "on")


def ip_is_blocked(ip_text: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _BLOCKED_NETS)


def _validated_ip_for_url(url: str) -> str | None:
    """Validate a URL and return the exact address that may be dialed."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"scheme '{scheme or 'none'}' not allowed (use http/https)")
    host = parts.hostname
    if not host:
        raise ValueError("URL has no hostname")
    if _local_fetch_allowed():
        return None
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve host '{host}': {exc}") from exc
    addrs = {info[4][0] for info in infos}
    blocked = sorted(a for a in addrs if ip_is_blocked(a))
    if blocked:
        raise ValueError(
            f"refusing to fetch private/internal address of '{host}' ({blocked[0]}); "
            "set SATURDAY_ALLOW_LOCAL_FETCH=1 to allow local targets"
        )
    if not infos:
        raise ValueError(f"cannot resolve host '{host}': no addresses returned")
    return infos[0][4][0]


def assert_public_url(url: str) -> str | None:
    """Raise ValueError when url points at a private/internal/metadata target."""
    return _validated_ip_for_url(url)


class _Pin:
    """The one validated dial-address for a single fetch. Mutable because a
    redirect hop re-validates AND re-pins before the next connection opens."""

    def __init__(self, ip: str | None = None) -> None:
        self.ip = ip
        self.enforce = ip is not None

    def set_for_url(self, url: str) -> None:
        self.ip = assert_public_url(url)
        self.enforce = self.ip is not None


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Same refusal rules on every hop; re-pins the dial IP to the newly
    validated target so the pinned opener follows redirects correctly."""

    def __init__(self, pin: _Pin | None = None) -> None:
        super().__init__()
        self._pin = pin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._pin is not None:
            self._pin.set_for_url(newurl)
        else:
            assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# DNS-rebinding defense (TOCTOU): assert_public_url resolves + validates, but a
# stock opener resolves DNS AGAIN at connect time — an attacker controlling DNS
# can answer round 1 with a public IP and round 2 with 169.254.169.254. These
# connection classes dial the exact IP that was validated while Host, the
# request line and the TLS server_hostname stay the real hostname, so vhosts
# and certificate verification are unaffected.
def _pinned_conn_class(base: type, ip: str) -> type:
    class _Pinned(base):
        def __init__(self, host, *args, **kwargs):
            super().__init__(host, *args, **kwargs)
            self.pinned_ip = ip

        def connect(self):
            # same steps as http.client's connect(), but the TCP peer is the
            # pre-validated IP instead of a second DNS lookup
            self.sock = socket.create_connection(
                (self.pinned_ip, self.port), self.timeout, self.source_address
            )
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass  # best-effort, same as stdlib
            if isinstance(self, http.client.HTTPSConnection):
                server_hostname = getattr(self, "_tunnel_host", None) or self.host
                self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)
            elif getattr(self, "_tunnel_host", None):
                self._tunnel()

    return _Pinned


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pin: _Pin) -> None:
        super().__init__()
        self._pin = pin

    def http_open(self, req):
        if not self._pin.enforce:
            return super().http_open(req)  # intentional SATURDAY_ALLOW_LOCAL_FETCH opt-out
        return self.do_open(_pinned_conn_class(http.client.HTTPConnection, self._pin.ip), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pin: _Pin) -> None:
        super().__init__()
        self._pin = pin

    def https_open(self, req):
        if not self._pin.enforce:
            return super().https_open(req)
        kwargs: dict = {}
        ctx = getattr(self, "_context", None)
        if ctx is not None:
            kwargs["context"] = ctx
        elif getattr(self, "_check_hostname", None) is not None:
            kwargs["check_hostname"] = self._check_hostname  # <=3.11 handler attr
        return self.do_open(_pinned_conn_class(http.client.HTTPSConnection, self._pin.ip), req, **kwargs)


def _build_fetch_opener(validated_ip: str | None) -> urllib.request.OpenerDirector:
    """Request-scoped opener with its dial IP pinned to the validated address."""
    if validated_ip is None and not _local_fetch_allowed():
        raise ValueError("validated address required for guarded fetch")
    handlers: list = []
    if urllib.request.getproxies() and not _local_fetch_allowed():
        # An ambient proxy or NO_PROXY decision can resolve the target after
        # this process validates it. Disable that route while the SSRF guard is
        # active so the pinned socket remains the security boundary.
        handlers.append(urllib.request.ProxyHandler({}))
    pin = _Pin(validated_ip)
    handlers.extend((_SafeRedirectHandler(pin), _PinnedHTTPHandler(pin), _PinnedHTTPSHandler(pin)))
    return urllib.request.build_opener(*handlers)


def _http_get(url: str, timeout: float = 20.0, max_bytes: int = 2_000_000) -> tuple[str, str]:
    validated_ip = assert_public_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "saturday-harness/0.4"})
    with _build_fetch_opener(validated_ip).open(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        data = resp.read(max_bytes)
    return final_url, data.decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href")
            self._href = href
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._link_text).split())
            if text:
                self.links.append((self._href, text[:200]))
            self._href = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        clean = data.strip()
        if clean:
            self.parts.append(clean + " ")
            if self._href is not None:
                self._link_text.append(clean + " ")


def extract_readable(html: str) -> tuple[str, list[tuple[str, str]]]:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), parser.links


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch a URL over HTTP(S) and return the readable text plus raw length."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "truncate body to this many chars"},
        },
        "required": ["url"],
    }

    def run(self, args: dict) -> tuple[bool, str]:
        url = args["url"]
        max_chars = int(args.get("max_chars") or 20_000)
        try:
            final_url, html = _http_get(url)
        except Exception as exc:
            return False, f"fetch failed: {type(exc).__name__}: {exc}"
        text, _ = extract_readable(html)
        if not text:
            text = html[:max_chars]
        suffix = f"\n... [{len(text)} chars total]" if len(text) > max_chars else ""
        return True, f"[{final_url}]\n" + text[:max_chars] + suffix


# DuckDuckGo Lite result markup (captured live 2026-08): attribute order and
# quote style vary, so anchor/snippet attributes are parsed order- and
# quote-agnostically instead of with one positional mega-regex.
_ANCHOR_TAG_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_CLOSE_ANCHOR_RE = re.compile(r"</a>", re.IGNORECASE)
_SNIPPET_ATTR_RE = re.compile(r"""(?:\s|^)class\s*=\s*(?:"result-snippet"|'result-snippet')""", re.IGNORECASE)
_RESULT_STOP_RE = re.compile(r"""<div\s+class\s*=\s*(?:"more"|'more'|"navlink"|'navlink')""", re.IGNORECASE)


def _tag_attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""(?:\s|^){name}\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag, re.IGNORECASE)
    if m is None:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def _clean_fragment(fragment: str) -> str:
    # strip tags first so escaped entities like &lt;b&gt; never become tags
    return _unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _unwrap_result_href(raw_href: str) -> str:
    """Decode exactly once: undo the HTML entity, then let parse_qs do the
    single percent-decode of the uddg parameter. Never pre-unquote the whole
    href - that corrupts target URLs containing %xx sequences."""
    href = raw_href.replace("&amp;", "&")
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg")
    return values[0] if values else href


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web (DuckDuckGo Lite) and return title/URL/snippet results. "
        "Use before web_fetch when you need to discover sources."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    }

    def run(self, args: dict) -> tuple[bool, str]:
        query = str(args.get("query") or "").strip()
        if not query:
            return False, "empty query"
        max_results = int(args.get("max_results") or 6)
        url = "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query})
        try:
            _, html = _http_get(url)
        except Exception as exc:
            return False, f"search failed: {type(exc).__name__}: {exc}"

        anchors: list[tuple[int, int, str]] = []
        for m in _ANCHOR_TAG_RE.finditer(html):
            tag = m.group(0)
            if "result-link" not in (_tag_attr(tag, "class") or "").split():
                continue
            href = _tag_attr(tag, "href")
            if href is not None:
                anchors.append((m.start(), m.end(), href))
        # Hard-fail instead of returning ok=True with no data: a zero-parse
        # almost always means a bot-block page or a layout change.
        if not anchors:
            return False, (
                f"web_search: no results parsed for '{query}' - the backend may be "
                "blocking this client or its layout has changed"
            )

        results: list[tuple[str, str, str]] = []
        for i, (_start, tag_end, raw_href) in enumerate(anchors):
            region_end = anchors[i + 1][0] if i + 1 < len(anchors) else len(html)
            stop = _RESULT_STOP_RE.search(html, tag_end, region_end)
            if stop:
                region_end = stop.start()
            close_a = _CLOSE_ANCHOR_RE.search(html, tag_end, region_end)
            title = _clean_fragment(html[tag_end : close_a.start()]) if close_a else ""
            snippet = ""
            attr = _SNIPPET_ATTR_RE.search(html, close_a.end() if close_a else tag_end, region_end)
            if attr:
                gt = html.find(">", attr.end(), region_end)
                if gt != -1:
                    td = html.find("</td>", gt + 1, region_end)
                    snippet = _clean_fragment(html[gt + 1 : td if td != -1 else region_end])
            href = _unwrap_result_href(raw_href)
            results.append((title or href, href, snippet))
            if len(results) >= max_results:
                break
        lines = [f"{i}. {t}\n   {u}\n   {s[:220]}" for i, (t, u, s) in enumerate(results, 1)]
        return True, "\n".join(lines)


class BrowserTool(Tool):
    """Text-mode browser: open pages, read extracted text, follow numbered links.

    Honest limitation: no JavaScript rendering. For JS-heavy SPAs use an MCP
    Playwright server instead.
    """

    name = "browser"
    description = (
        "Browse the web as readable text. action='open' fetches a URL and shows text plus numbered links; "
        "action='click' follows a link number from the last page; action='back' returns. No JS rendering."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["open", "click", "back"]},
            "url": {"type": "string"},
            "link_number": {"type": "integer"},
            "max_chars": {"type": "integer"},
        },
        "required": ["action"],
    }

    def __init__(self, max_chars: int = 12_000, max_links: int = 40) -> None:
        self.max_chars = max_chars
        self.max_links = max_links
        self.current: str | None = None
        self.stack: list[str] = []
        self.page_links: list[tuple[str, str]] = []

    def run(self, args: dict) -> tuple[bool, str]:
        action = args.get("action", "open")
        if action == "back":
            if not self.stack:
                return False, "no previous page"
            self.current = self.stack.pop()
            return self._open_current()
        if action == "click":
            n = int(args.get("link_number") or 0)
            if not self.page_links or not 1 <= n <= len(self.page_links):
                return False, f"no such link; open a page first (have {len(self.page_links)} links)"
            href = self.page_links[n - 1][0]
            return self._navigate(href)
        url = str(args.get("url") or "").strip()
        if not url:
            return False, "url required for open"
        return self._navigate(url)

    def _resolve(self, href: str) -> str | None:
        if href.startswith(("data:", "javascript:", "mailto:")):
            return None
        return urllib.parse.urljoin(self.current or "http://x/", href)

    def _navigate(self, url: str) -> tuple[bool, str]:
        if self.current:
            self.stack.append(self.current)
        try:
            final_url, html = _http_get(url)
        except Exception as exc:
            if self.stack:
                self.stack.pop()
            return False, f"fetch failed: {type(exc).__name__}: {exc}"
        self.current = final_url
        text, links = extract_readable(html)
        return self._render(final_url, text, links)

    def _open_current(self) -> tuple[bool, str]:
        assert self.current is not None
        try:
            _, html = _http_get(self.current)
        except Exception as exc:
            return False, f"fetch failed: {type(exc).__name__}: {exc}"
        text, links = extract_readable(html)
        return self._render(self.current, text, links)

    def _render(self, page_url: str, text: str, links: list[tuple[str, str]]) -> tuple[bool, str]:
        self.page_links = [(self._resolve(h), t) for h, t in links]
        self.page_links = [(u, t) for u, t in self.page_links if u][: self.max_links]
        shown = text[: self.max_chars]
        link_lines = "\n".join(f"[{i}] {t}" for i, (u, t) in enumerate(self.page_links, 1))
        body = (
            f"PAGE: {page_url}\n\n{shown}{'...[truncated]' if len(text) > self.max_chars else ''}\n\nLINKS:\n{link_lines}"
        )
        if len(body) > 24_000:
            body = body[:24_000] + "\n...[truncated]"
        return True, body
