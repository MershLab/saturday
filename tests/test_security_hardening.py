"""Security hardening: gateway allow-list, serve/webui Host+Origin+token,
SSRF blocklist, project trust gate, privileged-write refusal, sid sanitizing,
bot-token redaction."""
from __future__ import annotations

import http.client
import io
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

TOKEN = "sec" * 8

_ORIG_LOAD_MCP = None


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    global _ORIG_LOAD_MCP
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    if _ORIG_LOAD_MCP is None:
        _ORIG_LOAD_MCP = mcpmod.load_mcp_config
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


# ---------------------------------------------------------------- files.py M5

def test_write_file_refuses_privileged_paths(tmp_path):
    from saturday.tools.files import WriteFile

    tool = WriteFile(root=str(tmp_path))
    for bad in (".env", ".env.local", "sub/.env", ".saturday/mcp.json", "a/b/.saturday/mcp.json"):
        ok, msg = tool.run({"path": bad, "content": "x"})
        assert not ok and "privileged" in msg, bad
        assert not (tmp_path / bad).exists()
    ok, _ = tool.run({"path": "normal.txt", "content": "fine"})
    assert ok


def test_edit_file_refuses_privileged_paths(tmp_path):
    from saturday.tools.files import EditFile

    target = tmp_path / ".saturday" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": ".saturday/mcp.json", "old_string": "{", "new_string": "!!"})
    assert not ok and "privileged" in msg
    assert target.read_text(encoding="utf-8") == "{}"


def test_write_file_refuses_symlink_to_privileged_path(tmp_path):
    from saturday.tools.files import WriteFile

    target = tmp_path / ".saturday" / "config.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "notes.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    ok, msg = WriteFile(root=str(tmp_path)).run({"path": "notes.txt", "content": "evil"})
    assert not ok and "privileged" in msg
    assert target.read_text(encoding="utf-8") == "{}"


def test_privileged_path_normalization():
    from saturday.tools.files import is_privileged_path

    assert is_privileged_path(".ENV")
    assert is_privileged_path("..\\..\\.env")
    assert is_privileged_path(".saturday\\MCP.JSON")
    assert is_privileged_path("a/./b/../../.saturday/mcp.json")
    assert not is_privileged_path("src/env_loader.py")
    assert not is_privileged_path("docs/saturday/mcp_guide.md")


# ------------------------------------------------------------------ web.py H2

def test_ssrf_blocklist_blocks_internal_targets(monkeypatch):
    from saturday.tools import web

    monkeypatch.delenv("SATURDAY_ALLOW_LOCAL_FETCH", raising=False)
    for url in (
        "http://127.0.0.1:8787/x",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.20.3.4/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ):
        with pytest.raises(ValueError):
            web.assert_public_url(url)


def test_ssrf_blocklist_allows_public_hosts(monkeypatch):
    from saturday.tools import web

    monkeypatch.delenv("SATURDAY_ALLOW_LOCAL_FETCH", raising=False)

    def fake_getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(web.socket, "getaddrinfo", fake_getaddrinfo)
    web.assert_public_url("https://example.com/page")  # must not raise

    def private_getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", ("169.254.169.254", 80))]

    monkeypatch.setattr(web.socket, "getaddrinfo", private_getaddrinfo)
    with pytest.raises(ValueError) as ei:
        web.assert_public_url("http://rebind.example/")
    assert "private" in str(ei.value)


def test_ssrf_fetch_uses_one_validated_dns_result(monkeypatch):
    from saturday.tools import web

    monkeypatch.delenv("SATURDAY_ALLOW_LOCAL_FETCH", raising=False)
    monkeypatch.setattr(web.urllib.request, "getproxies", lambda: {})
    calls: list[str] = []

    def rebinding_getaddrinfo(host, port, *a, **k):
        calls.append(host)
        if len(calls) == 1:
            return [(2, 1, 6, "", ("93.184.216.34", 443))]
        return [(2, 1, 6, "", ("169.254.169.254", 80))]

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def geturl(self):
            return "https://rebind.example/"

        def read(self, _max_bytes):
            return b"safe"

    def fake_open(self, _req, timeout=None):
        return Response()

    monkeypatch.setattr(web.socket, "getaddrinfo", rebinding_getaddrinfo)
    monkeypatch.setattr(web.urllib.request.OpenerDirector, "open", fake_open)

    assert web._http_get("https://rebind.example/") == ("https://rebind.example/", "safe")
    assert calls == ["rebind.example"]


def test_ssrf_redirect_revalidates_and_repins_once(monkeypatch):
    from saturday.tools import web

    monkeypatch.delenv("SATURDAY_ALLOW_LOCAL_FETCH", raising=False)
    calls: list[str] = []

    def stable_getaddrinfo(host, port, *a, **k):
        calls.append(host)
        if calls.count(host) > 1:
            raise AssertionError(f"{host} resolved more than once")
        address = "93.184.216.34" if host == "first.example" else "203.0.113.10"
        return [(2, 1, 6, "", (address, 443))]

    monkeypatch.setattr(web.socket, "getaddrinfo", stable_getaddrinfo)
    pin = web._Pin(web._validated_ip_for_url("https://first.example/"))
    handler = web._SafeRedirectHandler(pin)
    request = web.urllib.request.Request("https://first.example/")

    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://next.example/"
    )

    assert redirected is not None
    assert pin.ip == "203.0.113.10"
    assert calls == ["first.example", "next.example"]


def test_ssrf_public_fetch_disables_ambient_proxy(monkeypatch):
    from saturday.tools import web

    monkeypatch.delenv("SATURDAY_ALLOW_LOCAL_FETCH", raising=False)
    monkeypatch.setattr(web.urllib.request, "getproxies", lambda: {"https": "http://proxy.example"})
    captured = []
    real_build_opener = web.urllib.request.build_opener

    def capture_build_opener(*handlers):
        captured.extend(handlers)
        return real_build_opener(*handlers)

    monkeypatch.setattr(web.urllib.request, "build_opener", capture_build_opener)
    opener = web._build_fetch_opener("93.184.216.34")
    proxy_handlers = [h for h in captured if isinstance(h, web.urllib.request.ProxyHandler)]
    assert proxy_handlers and proxy_handlers[0].proxies == {}
    assert any(isinstance(h, web._PinnedHTTPHandler) for h in opener.handlers)


def test_ssrf_fetch_rejects_missing_validated_address(monkeypatch):
    from saturday.tools import web

    monkeypatch.delenv("SATURDAY_ALLOW_LOCAL_FETCH", raising=False)
    with pytest.raises(ValueError, match="validated address"):
        web._build_fetch_opener(None)


def test_ssrf_local_fetch_opt_in(monkeypatch):
    from saturday.tools import web

    monkeypatch.setenv("SATURDAY_ALLOW_LOCAL_FETCH", "1")

    def boom(host, port, *a, **k):  # resolution skipped entirely when opted in
        raise AssertionError("getaddrinfo should not be called")

    monkeypatch.setattr(web.socket, "getaddrinfo", boom)
    web.assert_public_url("http://127.0.0.1:11434/v1")


# ------------------------------------------------------------- trust gate M1

@pytest.fixture()
def trust_home(tmp_path, monkeypatch):
    home = tmp_path / "dfhome"
    monkeypatch.setattr("saturday.config.CONFIG_DIR", home)
    monkeypatch.delenv("SATURDAY_TRUST_ALL_PROJECTS", raising=False)
    return home


def test_trust_fails_closed_non_interactive(trust_home, monkeypatch, capsys):
    from saturday.utils.trust import ensure_trusted

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert ensure_trusted("/some/project", what="test config") is False
    out = capsys.readouterr().err
    assert "untrusted" in out
    store_file = trust_home / "trusted_projects.json"
    assert not store_file.exists(), "non-interactive denial must not persist"


def test_trust_env_override(trust_home, monkeypatch):
    from saturday.utils.trust import ensure_trusted

    monkeypatch.setenv("SATURDAY_TRUST_ALL_PROJECTS", "1")
    assert ensure_trusted("/any/project", what="test") is True


def test_trust_prompt_approve_then_remember(trust_home, monkeypatch):
    import saturday.utils.trust as trust

    class FakeIn(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys, "stdin", FakeIn("y\n"))
    assert trust.ensure_trusted(Path("/proj/a"), what="cfg") is True
    data = json.loads((trust_home / "trusted_projects.json").read_text())
    assert len(data["approved"]) == 1

    # remembered approval: no prompt even non-interactively
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert trust.ensure_trusted(Path("/proj/a"), what="cfg") is True


def test_trust_prompt_deny_persists(trust_home, monkeypatch):
    import saturday.utils.trust as trust

    class FakeIn(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys, "stdin", FakeIn("\n"))  # empty -> deny
    assert trust.ensure_trusted(Path("/proj/b"), what="cfg") is False
    # denial is remembered across runs
    assert trust.ensure_trusted(Path("/proj/b"), what="cfg") is False


def test_load_env_skips_untrusted_cwd_env(tmp_path, monkeypatch, trust_home):
    from saturday.utils.env import load_env_file

    (tmp_path / ".env").write_text("SATURDAY_MODEL=evil-model\nOTHER=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SATURDAY_MODEL", raising=False)
    monkeypatch.delenv("OTHER", raising=False)

    loaded = load_env_file()
    assert "SATURDAY_MODEL" not in loaded and "OTHER" not in loaded
    assert "SATURDAY_MODEL" not in os.environ

    try:
        monkeypatch.setenv("SATURDAY_TRUST_ALL_PROJECTS", "1")
        loaded = load_env_file()
        assert loaded.get("SATURDAY_MODEL") == "evil-model"
        assert os.environ.get("SATURDAY_MODEL") == "evil-model"
    finally:
        os.environ.pop("SATURDAY_MODEL", None)
        os.environ.pop("OTHER", None)


def test_mcp_config_gated_by_trust(tmp_path, monkeypatch, trust_home):
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(mcpmod, "load_mcp_config", _ORIG_LOAD_MCP)  # real implementation

    d = tmp_path / "proj" / ".saturday"
    d.mkdir(parents=True)
    cfg = d / "mcp.json"
    cfg.write_text(json.dumps({"servers": {"evil": {"command": "calc.exe"}}}), encoding="utf-8")
    monkeypatch.chdir(tmp_path / "proj")
    problems: list[str] = []

    assert mcpmod.load_mcp_config(warnings=problems) == {}
    assert any("not trusted" in w for w in problems)

    monkeypatch.setenv("SATURDAY_TRUST_ALL_PROJECTS", "1")
    problems.clear()
    got = mcpmod.load_mcp_config(warnings=problems)
    assert list(got) == ["evil"]

    # explicit path stays always-trusted (user-directed)
    monkeypatch.delenv("SATURDAY_TRUST_ALL_PROJECTS", raising=False)
    assert mcpmod.load_mcp_config(cfg) == {"evil": {"command": "calc.exe"}}


def test_project_hooks_gated_by_trust(tmp_path, monkeypatch, trust_home, capsys):
    from saturday.user_hooks import load_hooks
    from saturday.utils.trust import pending_trust_items

    project = tmp_path / "project"
    hooks = project / ".saturday" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({"pre_tool_call": ["echo unsafe"]}), encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    assert load_hooks(project)["pre_tool_call"] == []
    assert any(item["kind"] == "hooks" for item in pending_trust_items(project))
    assert "untrusted" in capsys.readouterr().err

    monkeypatch.setenv("SATURDAY_TRUST_ALL_PROJECTS", "1")
    assert load_hooks(project)["pre_tool_call"] == ["echo unsafe"]


def test_load_env_uses_configured_home(trust_home, monkeypatch, tmp_path):
    from saturday.utils.env import load_env_file

    monkeypatch.chdir(tmp_path)
    (trust_home / ".env").write_text("SATURDAY_MODEL=configured-model\n", encoding="utf-8")
    monkeypatch.delenv("SATURDAY_MODEL", raising=False)

    loaded = load_env_file()
    assert loaded["SATURDAY_MODEL"] == "configured-model"
    assert os.environ["SATURDAY_MODEL"] == "configured-model"


def test_playwright_filters_followup_requests(monkeypatch):
    import saturday.tools.browser_playwright as browser_mod

    checked: list[str] = []

    def assert_public(url):
        checked.append(url)
        if "127.0.0.1" in url:
            raise ValueError("private target")

    class Route:
        def __init__(self):
            self.action = None

        def continue_(self):
            self.action = "continue"

        def abort(self, **kwargs):
            self.action = ("abort", kwargs)

    class Request:
        def __init__(self, url):
            self.url = url

    monkeypatch.setattr(browser_mod, "assert_public_url", assert_public)

    public = Route()
    browser_mod.PlaywrightBrowserTool._route_request(public, Request("https://example.com/next"))
    assert public.action == "continue" and checked == ["https://example.com/next"]

    private = Route()
    browser_mod.PlaywrightBrowserTool._route_request(private, Request("http://127.0.0.1/admin"))
    assert private.action[0] == "abort"

    inline = Route()
    browser_mod.PlaywrightBrowserTool._route_request(inline, Request("data:text/plain,ok"))
    assert inline.action == "continue"


def test_playwright_route_pins_validated_address(monkeypatch):
    import saturday.tools.browser_playwright as browser_mod

    class Route:
        def __init__(self):
            self.action = None

        def continue_(self):
            self.action = "continue"

        def abort(self, **kwargs):
            self.action = ("abort", kwargs)

    class Request:
        url = "https://example.com/"

    class Proxy:
        def __init__(self):
            self.pins = []

        def pin(self, url, ip):
            self.pins.append((url, ip))

    monkeypatch.setattr(browser_mod, "assert_public_url", lambda _url: "93.184.216.34")
    proxy = Proxy()
    route = Route()
    browser_mod.PlaywrightBrowserTool._route_request(route, Request(), proxy)
    assert route.action == "continue"
    assert proxy.pins == [("https://example.com/", "93.184.216.34")]


def test_pinned_browser_proxy_connects_only_to_recorded_address():
    import socket
    from http.server import BaseHTTPRequestHandler
    from saturday.tools.browser_playwright import _PinnedBrowserProxy

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            body = b"pinned"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proxy = _PinnedBrowserProxy()
    try:
        port = server.server_address[1]
        proxy.pin(f"http://example.test:{port}/", "127.0.0.1")
        with socket.create_connection(("127.0.0.1", int(proxy.server_url.rsplit(":", 1)[1]))) as client:
            client.sendall(
                f"GET http://example.test:{port}/ HTTP/1.1\r\n"
                f"Host: example.test:{port}\r\nConnection: close\r\n\r\n".encode()
            )
            response = bytearray()
            while b"pinned" not in response:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        assert b"200" in response and b"pinned" in response
    finally:
        proxy.close()
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------ gateway C2 + M2

def test_gateway_cli_requires_allowlist():
    from saturday.cli import cmd_gateway

    ns = type("NS", (), {})()
    ns.token = "t"
    ns.allow = ""
    ns.allow_all = False
    ns.env = None
    assert cmd_gateway(ns) == 1  # refuses before any agent/polling is created


def test_gateway_allow_parsing():
    ids = {int(c) for c in "-7,42".split(",") if c.strip().lstrip("-").isdigit()}
    assert ids == {-7, 42}


def test_gateway_redact_token():
    from saturday.gateway import redact_token

    text = "<urlopen error HTTP Error 401: Unauthorized> https://api.telegram.org/botSECRET123/getUpdates"
    assert redact_token(text, "SECRET123") == (
        "<urlopen error HTTP Error 401: Unauthorized> https://api.telegram.org/bot***/getUpdates"
    )
    assert redact_token(text, "") == text


# --------------------------------------------------------------- serve C3/H1+

class _FakeTraj:
    final_answer = "ok"
    stop_reason = "done"


class _FakeAgent:
    def run(self, task, **kw):
        return _FakeTraj()


@pytest.fixture()
def serve_srv():
    from saturday.cli import make_serve_handler
    from saturday.utils.httpd import allowed_hosts, allowed_origins

    handler = make_serve_handler(token=TOKEN, agent_factory=lambda: _FakeAgent())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    # finalize pinning now that the ephemeral port is known (mirrors cmd_serve)
    handler.allowed_hosts = allowed_hosts(*srv.server_address[:2])
    handler.allowed_origins = allowed_origins(handler.allowed_hosts)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _post(port, path, body=b'{"text":"hi"}', headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=body, headers=dict(headers or {}))
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


def test_serve_token_enforced(serve_srv):
    port = serve_srv.server_address[1]
    status, body = _post(port, "/message", headers={"X-Saturday-Token": "wrong"})
    assert status == 401

    status, body = _post(port, "/message", headers={"X-Saturday-Token": TOKEN})
    assert status == 200 and json.loads(body)["ok"] is True

    status, _ = _post(port, "/message", headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200


def test_serve_rejects_evil_host_and_origin(serve_srv):
    port = serve_srv.server_address[1]
    auth = {"X-Saturday-Token": TOKEN}

    status, body = _post(port, "/message", headers={**auth, "Host": f"evil.example:{port}"})
    assert status == 403 and b"Host" in body

    status, body = _post(
        port,
        "/message",
        headers={**auth, "Origin": f"http://attacker.example:{port}"},
    )
    assert status == 403 and b"cross-origin" in body

    # legit origin passes
    status, _ = _post(
        port,
        "/message",
        headers={**auth, "Origin": f"http://127.0.0.1:{port}"},
    )
    assert status == 200


# ------------------------------------------------------------- webui H1 + M4

def test_webui_host_origin_cookie_guards(tmp_path, monkeypatch):
    from saturday.webui import AppServer, AppState

    app = AppState(store_root=tmp_path / "s", cfg_overrides={"workspace_root": str(tmp_path)})
    srv = AppServer(("127.0.0.1", 0), app, token=TOKEN)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def req(method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, headers=dict(headers or {}))
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    try:
        # token required
        status, _ = req("GET", "/api/state")
        assert status == 401

        # lookalike cookie must NOT authorize (exact-match cookie parsing)
        status, _ = req("GET", "/api/state", headers={"Cookie": f"xdf_token={TOKEN}"})
        assert status == 401

        # exact cookie authorizes
        status, _ = req("GET", "/api/state", headers={"Cookie": f"df_token={TOKEN}; other=1"})
        assert status == 200

        # ?k= in the URL is no longer an auth channel (r2 review: tokens leak
        # via history/Referer); it only bootstraps a cookie on GET /
        status, _ = req("GET", f"/api/state?k={TOKEN}")
        assert status == 401

        # evil Host rejected even with a valid token (rebinding defense)
        status, body = req("GET", "/api/state", headers={"Host": f"evil.example:{port}", "X-Saturday-Token": TOKEN})
        assert status == 403

        # cross-origin POST rejected (CSRF defense)
        status, body = req(
            "POST",
            "/api/config",
            headers={
                "X-Saturday-Token": TOKEN,
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
            },
        )
        assert status == 403 and b"cross-origin" in body
    finally:
        srv.shutdown()
        srv.server_close()


def test_save_data_urls_sanitizes_sid(tmp_path, monkeypatch):
    import saturday.webui as w

    png64 = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    monkeypatch.setattr(w.tempfile, "gettempdir", lambda: str(tmp_path))
    evil_sid = r"..\..\..\escaped"
    paths, err = w._save_data_urls(evil_sid, [png64])
    assert err is None and paths
    uploads_root = (tmp_path / "saturday-uploads").resolve()
    written = Path(paths[0]).resolve()
    assert uploads_root in written.parents, "must stay inside uploads root"
    subdir = written.parent.name
    assert subdir and all(c.isalnum() or c in "-_" for c in subdir)
