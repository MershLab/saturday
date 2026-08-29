"""Frontier feature tests: web search/browser, skills loop, vision, attachments."""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.loop import AgentLoop  # noqa: E402
from saturday.plugins import install_plugins, learning_plugin  # noqa: E402
from saturday.tools import web as webmod  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.skills import SkillStore, skills_prompt_block  # noqa: E402
from saturday.tools.vision import ViewImageTool  # noqa: E402
from saturday.tools.web import BrowserTool, WebSearchTool, extract_readable  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def make_png(tmp_path: Path) -> Path:
    p = tmp_path / "shot.png"
    p.write_bytes(PNG)
    return p


PAGE_HTML = """
<html><head><style>body { color: red; }</style></head>
<body>
<h1>Pricing</h1>
<p>Plans start at <b>$9</b> per month.</p>
<script>alert("evil")</script>
<a href="/docs">Docs</a>
<a href="https://external.example/x">External</a>
</body></html>
"""


def test_extract_readable_text_and_links():
    text, links = extract_readable(PAGE_HTML)
    assert "Pricing" in text and "$9" in text
    assert "alert" not in text
    assert ("/docs", "Docs") in links
    assert ("https://external.example/x", "External") in links


# Fixture mirrors live DuckDuckGo Lite markup (captured 2026-08): href comes
# BEFORE class, attribute values use single quotes, targets ride in the uddg
# param, and snippets live in single-quoted <td class='result-snippet'> cells.
DDG_HTML = """
<html><head><title>q at DuckDuckGo</title></head><body>
<table>
<tr><td valign="top">1.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fasync%2Dio%2Dpython%2F&amp;rut=a1" class='result-link'>Python&#x27;s asyncio: A Hands-On Walkthrough</a></td>
</tr>
<tr><td>&nbsp;&nbsp;&nbsp;</td>
<td class='result-snippet'>
<b>asyncio</b> library enables you to write concurrent code using the async and await keywords.
</td>
</tr>
<tr><td valign="top">2.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs%3Fa%3D1%26b%3D2&amp;rut=b2" class='result-link'>Query-string docs</a></td>
</tr>
<tr><td>&nbsp;&nbsp;&nbsp;</td>
<td class='result-snippet'>
Target URLs must be percent-decoded exactly once (&#x27;%26&#x27; stays encoded until parse_qs).
</td>
</tr>
<tr><td><a href="https://github.com/example/repo" CLASS="result-link">Legacy-style result</a></td>
<td class="result-snippet">Double-quoted legacy markup still parses.</td></tr>
</table>
<div class="more"><a href="/lite?p=2">Next</a></div>
</body></html>
"""


def _fake_ddg(url, timeout=20.0, max_bytes=2_000_000):
    return url, DDG_HTML


def test_web_search_parses_live_style_fixture(monkeypatch):
    monkeypatch.setattr(webmod, "_http_get", _fake_ddg)
    ok, out = WebSearchTool().run({"query": "saturday harness", "max_results": 5})
    assert ok
    assert "Python's asyncio: A Hands-On Walkthrough" in out
    assert "https://realpython.com/async-io-python/" in out
    assert "concurrent code using the async and await keywords" in out
    assert "https://github.com/example/repo" in out
    assert "Double-quoted legacy markup still parses." in out


def test_web_search_decodes_uddg_exactly_once(monkeypatch):
    # Regression: pre-unquoting the whole href turned %26 into '&' before
    # parse_qs split the query, truncating the target URL after ?a=1.
    monkeypatch.setattr(webmod, "_http_get", _fake_ddg)
    ok, out = WebSearchTool().run({"query": "anything"})
    assert ok
    assert "\n   https://example.com/docs?a=1&b=2\n" in out


def test_web_search_zero_parse_is_error(monkeypatch):
    monkeypatch.setattr(
        webmod, "_http_get", lambda *a, **k: (a[0], "<html><body>unusual traffic detected</body></html>")
    )
    ok, out = WebSearchTool().run({"query": "anything"})
    assert not ok
    assert "no results parsed" in out


class Pages(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        body = {
            "/": b"<html><body><p>Welcome home</p><a href=\"/docs\">Docs</a></body></html>",
            "/docs": b"<html><body><p>Documentation body</p><a href=\"/\">Home</a></body></html>",
        }.get(self.path, b"<html><body>404</body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def pages_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_browser_open_click_back(pages_server, monkeypatch):
    # loopback test server: the SSRF guard blocks 127.0.0.1 by default
    monkeypatch.setenv("SATURDAY_ALLOW_LOCAL_FETCH", "1")
    b = BrowserTool()
    ok, out = b.run({"action": "open", "url": pages_server + "/"})
    assert ok and "Welcome home" in out and "[1] Docs" in out

    ok, out = b.run({"action": "click", "link_number": 1})
    assert ok and "Documentation body" in out and "/docs" in out

    ok, out = b.run({"action": "back"})
    assert ok and "Welcome home" in out


def test_browser_click_without_open():
    ok, err = BrowserTool().run({"action": "click", "link_number": 1})
    assert not ok


def test_skill_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.tools.skills.skills_dir", lambda: tmp_path / "skills")
    store = SkillStore()

    ok, msg = store.save("My Skill!", "when to use x", "# steps\n1. do the thing")
    assert ok and "my-skill" in msg

    ok, body = store.load("my-skill")
    assert ok and "when to use x" in body and "# steps" in body

    entries = store.index()
    assert entries == [("my-skill", "when to use x")]

    ok, _ = store.save("", "desc", "body")
    assert not ok
    ok, _ = store.save("big", "desc", "x" * 16_001)
    assert not ok and "too large" in _

    block = skills_prompt_block(store)
    assert "- my-skill:" in block


def test_skill_store_reads_hermes_agentskills_format(tmp_path, monkeypatch):
    """Hermes skills use the same front-matter (name/description + optional
    metadata); a skill written by hermes-agent must load here unchanged."""
    monkeypatch.setattr("saturday.tools.skills.skills_dir", lambda: tmp_path / "skills")
    store = SkillStore()
    path = tmp_path / "skills" / "hermes-style" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "name: hermes-style\n"
        "description: open-format skill from hermes-agent\n"
        "author: nousresearch\n"
        "version: 1.0.0\n"
        "license: MIT\n"
        "tags: [ops, deploy]\n"
        "metadata:\n"
        "  source: agentskills.io\n"
        "---\n"
        "## Usage\n"
        "1. load the artifact\n"
        "2. ship it\n",
        encoding="utf-8",
    )
    ok, body = store.load("hermes-style")
    assert ok and "ship it" in body
    entries = store.index()
    assert entries == [("hermes-style", "open-format skill from hermes-agent")]


def test_skills_prompt_block_empty_store():
    class Empty:
        def index(self):
            return []

    block = skills_prompt_block(Empty())
    assert "skill_save" in block


def test_view_image_registry_image_transfer(tmp_path):
    png = make_png(tmp_path)
    tool = ViewImageTool()
    reg = ToolRegistry()
    reg.register(tool)

    result = reg.execute("c1", "view_image", {"path": str(png)})
    assert result.ok and result.images == [str(png.resolve())]
    assert tool.pending_images == []

    bad = tmp_path / "no.txt"
    bad.write_text("plain")
    res2 = reg.execute("c2", "view_image", {"path": str(bad)})
    assert not res2.ok and not res2.images


def test_loop_attaches_tool_images_as_vision_message(tmp_path):
    png = make_png(tmp_path)
    reg = ToolRegistry()
    reg.register(ViewImageTool())
    model = make_scripted_model(
        [
            {"tool_calls": [{"name": "view_image", "arguments": {"path": str(png)}}]},
            {"content": "I saw the image"},
        ]
    )
    traj = AgentLoop(model, reg, max_steps=3).run("sys", "look at the shot")

    assert traj.final_answer == "I saw the image"
    assert traj.steps[0].results[0].images

    msgs = model.calls[1]["messages"]
    vision_msgs = [m for m in msgs if isinstance(m.get("content"), list)]
    assert vision_msgs, "expected a content-parts message"
    parts = vision_msgs[0]["content"]
    assert any(
        p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/png;base64,")
        for p in parts
    )


def test_attachments_become_vision_parts_in_first_message(tmp_path):
    png = make_png(tmp_path)
    model = make_scripted_model([{"content": "noted"}])
    AgentLoop(model, ToolRegistry(), max_steps=1).run("sys", "what is this", attachments=[str(png)])

    first_user = model.calls[0]["messages"][1]
    assert isinstance(first_user["content"], list)
    types = [p["type"] for p in first_user["content"]]
    assert types[0] == "text"
    assert "image_url" in types


def test_learning_plugin_registers_skill_tools():
    reg = ToolRegistry()
    persona: list[str] = []
    install_plugins(reg, [learning_plugin()], persona)
    for name in ("skill_save", "skill_load", "skills_index"):
        assert name in reg.names()
    assert any("skill_save" in p for p in persona)
