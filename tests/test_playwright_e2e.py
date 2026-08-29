"""Real Playwright e2e: proves JS-rendered browsing works end-to-end."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from saturday.tools.browser_playwright import PlaywrightBrowserTool, playwright_available
from saturday.tools.web import BrowserTool

JS_PAGE = """<!doctype html><html><body>
<h1 id="h">static heading</h1>
<div id="out"></div>
<script>
document.getElementById('out').textContent = 'RENDERED_BY_JS_' + (2+3);
</script>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/js"):
            body = JS_PAGE.encode()
        else:
            body = b"<html><body><p>no js here</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def js_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.mark.skipif(not playwright_available(), reason="playwright not installed")
def test_js_rendering_visible_to_playwright_but_not_text_browser(js_server, tmp_path, monkeypatch):
    # loopback test server: the SSRF guard blocks 127.0.0.1 by default
    monkeypatch.setenv("SATURDAY_ALLOW_LOCAL_FETCH", "1")
    url = js_server + "/js"

    text_tool = BrowserTool()
    ok, out = text_tool.run({"action": "open", "url": url})
    assert ok
    assert "static heading" in out
    assert "RENDERED_BY_JS_5" not in out, "text browser should not see JS output"

    pw = PlaywrightBrowserTool()
    try:
        ok, out = pw.run({"action": "open", "url": url})
        assert ok, out
        assert "static heading" in out
        assert "RENDERED_BY_JS_5" in out, "playwright must execute the script"

        ok, out = pw.run({"action": "screenshot", "url": url})
        assert ok and pw.pending_images
        shot = Path(pw.pending_images[0])
        assert shot.stat().st_size > 1000
        assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        pw.close()
