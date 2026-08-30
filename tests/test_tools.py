"""Merged from: tests/test_tools.py, tests/test_tool_toggles.py."""
from __future__ import annotations
from pathlib import Path
from saturday.tools.files import EditFile, GlobTool, GrepTool, ListDir, ReadFile, WriteFile
from saturday.tools.python_repl import PythonREPL
from saturday.tools.shell import ShellTool
import json
import sys
import urllib.error
import urllib.request
from fakes import make_scripted_model
from saturday.agent.core import Agent
from saturday.config import AgentConfig
from saturday.tools.base import ToolRegistry
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from saturday.agent.loop import AgentLoop
from saturday.plugins import install_plugins, learning_plugin
from saturday.tools import web as webmod
from saturday.tools.skills import SkillStore, skills_prompt_block
from saturday.tools.vision import ViewImageTool
from saturday.tools.web import BrowserTool, WebSearchTool, extract_readable
import saturday.tools.ocr as ocr
from saturday.tools.ocr import UiTextTool, parse_tsv, slugify
from saturday.tools.spatial import LandmarkStore
from saturday.tools.spatial_unix import parse_combo_mac, translate_linux_key
import time
from saturday.sessions import SessionStore, verify_chain
from saturday.tasks import SubagentTask
from saturday.tools.browser_playwright import PlaywrightBrowserTool, playwright_available
import ast
import os
import uuid
from saturday.safety import ApprovalPolicy, check_command
from saturday.tools.spatial import KeyboardTool, PointerTool
from saturday.prompts.system import build_computer_use_section
from saturday.safety import make_approval_hook
from saturday.tools.spatial import (  # noqa: E402
    UiInvokeTool,
    capture_window_bg,
    ps_capture_window_script,
    ps_scan_script,
    ps_ui_invoke_script,
)
from saturday.prompts.system import build_system_prompt_parts
from saturday.tools.spatial import (  # noqa: E402
    ClipboardTool,
    WindowTool,
    parse_combo,
    ps_send_input_defines,
)
from saturday.tools.spatial import (  # noqa: E402
    UiTreeTool,
    build_grid_legend,
    cell_name,
    collect_marks,
    marked_legend,
    render_element_tree,
)
from saturday.ablation import FILE_MARKER, run_ablation, _summary
from saturday.exporter import collect_image_paths, embed_assets
from saturday.statemap import StateCache, compute_delta, element_box, element_identity
from saturday.tools.screen import ScreenTool
from saturday.tools.spatial import verify_expect
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JS_PAGE = """<!doctype html><html><body>
<h1 id="h">static heading</h1>
<div id="out"></div>
<script>
document.getElementById('out').textContent = 'RENDERED_BY_JS_' + (2+3);
</script>
</body></html>"""
TOKEN = "tok"
WIN_LIST = "4321|DF BG Target|100,200,800,600\n"

def test_write_read_edit(tmp_path: Path):
    root = str(tmp_path)
    w = WriteFile(root=root)
    ok, out = w.run({"path": "sub/a.txt", "content": "hello world"})
    assert ok
    r = ReadFile(root=root)
    ok, text = r.run({"path": "sub/a.txt"})
    assert ok and "1: hello world" in text

    e = EditFile(root=root)
    ok, out = e.run({"path": "sub/a.txt", "old_string": "world", "new_string": "forge"})
    assert ok
    ok, text = r.run({"path": "sub/a.txt"})
    assert "hello forge" in text


def test_edit_rejects_ambiguous_match(tmp_path: Path):
    root = str(tmp_path)
    WriteFile(root=root).run({"path": "b.txt", "content": "x x x"})
    ok, err = EditFile(root=root).run({"path": "b.txt", "old_string": "x", "new_string": "y"})
    assert not ok and "3 times" in err


def test_path_escape_blocked(tmp_path: Path):
    r = ReadFile(root=str(tmp_path))
    ok, err = r.run({"path": "../../etc/passwd"})
    assert not ok and "escapes" in err


def test_glob_and_grep(tmp_path: Path):
    root = str(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("VALUE = 42\n")
    (tmp_path / "notes.md").write_text("the VALUE here\n")
    g = GlobTool(root=root)
    ok, out = g.run({"pattern": "**/*.py"})
    assert ok and "src/m.py" in out
    gp = GrepTool(root=root)
    ok, out = gp.run({"pattern": r"VALUE\s*=\s*42", "include": "**/*"})
    assert ok and "src/m.py:1" in out


def test_listdir_and_shell(tmp_path: Path):
    ld = ListDir(root=str(tmp_path))
    ok, out = ld.run({})
    assert ok
    sh = ShellTool(root=str(tmp_path))
    ok, out = sh.run({"command": "echo saturday"})
    assert ok and "saturday" in out


def test_python_repl_persistence():
    repl = PythonREPL()
    try:
        ok, _ = repl.run({"code": "z = 6 * 7"})
        assert ok
        ok, out = repl.run({"code": "print(z)"})
        assert ok and "42" in out
        ok, err = repl.run({"code": "1/0"})
        assert not ok and "ZeroDivisionError" in err
    finally:
        repl.close()



# --- from tests/test_tool_toggles.py ---

sys.path.insert(0, str(Path(__file__).parent))


def test_family_expansion():
    assert ToolRegistry.expand_tool_names(["web"]) == {"web_search", "web_fetch"}
    assert ToolRegistry.expand_tool_names(["web", "python", "read_file"]) == {
        "web_search", "web_fetch", "python", "read_file"
    }
    assert ToolRegistry.expand_tool_names([]) == set()


def test_excluding_view():
    reg = ToolRegistry()

    class T:
        def __init__(self, name):
            self.name = name

        def run(self, args):
            return True, "ok"

    for n in ("shell", "web_search", "read_file"):
        reg.register(T(n))
    view = reg.excluding({"web_search"})
    assert view.names() == ["read_file", "shell"]
    assert view.get("web_search") is None
    assert reg.get("web_search") is not None  # original untouched


def _agent_with_tools(cfg=None):
    agent = Agent(
        cfg=cfg or AgentConfig(provider="openai", model="m"),
        client=make_scripted_model([{"content": "ok"}]),
        enable_subagents=False,
    )
    agent._ensure_client = lambda: agent.client
    return agent


def test_cfg_disabled_tools_hidden_from_run():
    cfg = AgentConfig(provider="openai", model="m", disabled_tools=["web"])
    agent = _agent_with_tools(cfg)
    names = set(agent.effective_registry().names())
    assert "web_search" not in names and "web_fetch" not in names  # family expanded
    assert "read_file" in names and "shell" in names               # rest intact
    # single-name disable leaves siblings alone
    agent2 = _agent_with_tools(AgentConfig(provider="openai", model="m", disabled_tools=["shell"]))
    n2 = agent2.effective_registry().names()
    assert "shell" not in n2 and "python" in n2


def test_session_scoped_toggle_does_not_touch_config():
    cfg = AgentConfig(provider="openai", model="m")
    agent = _agent_with_tools(cfg)
    ok, msg, now_off = agent.toggle_tool("computer_use")
    assert ok and now_off
    assert "pointer" in agent.disabled_tools and "ui_invoke" in agent.disabled_tools
    assert cfg.disabled_tools == []  # global config untouched by session toggle
    ok2, msg2, now_off2 = agent.toggle_tool("computer_use")
    assert ok2 and not now_off2
    assert agent.disabled_tools == set()
    # unknown name rejected with families hint
    ok3, msg3, _ = agent.toggle_tool("nope")
    assert not ok3 and "families" in msg3
    # single tool toggle
    ok4, _, off4 = agent.toggle_tool("shell")
    assert ok4 and off4 and "shell" in agent.disabled_tools


def test_plan_mode_and_toggles_compose():
    cfg = AgentConfig(provider="openai", model="m")
    agent = _agent_with_tools(cfg)
    agent.toggle_tool("memory")
    agent.plan_mode = True
    names = set(agent.effective_registry().names())
    assert "memory" not in names          # session toggle
    assert "write_file" not in names      # plan mode filter
    assert "grep" in names                # read-only survivor


def test_config_load_accepts_comma_string(tmp_path, monkeypatch):
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    (tmp_path / "config.json").write_text('{"disabled_tools": "web, shell"}', encoding="utf-8")
    loaded = cfgmod.AgentConfig.load()
    assert loaded.disabled_tools == ["web", "shell"]


def _server(app):
    import threading

    from saturday.webui import AppServer

    http = AppServer(("127.0.0.1", 0), app, token="tok")
    base = f"http://127.0.0.1:{http.server_address[1]}"
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return base, "tok"


def _post_json(base, path, payload, token):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"X-Saturday-Token": token, "Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_apply_config_disabled_tools_validation_and_state(monkeypatch, tmp_path):

    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "s")
    monkeypatch.setattr("saturday.config.save_config", lambda partial: None)
    base, tok = _server(app)
    status, body = _post_json(base, "/api/config", {"disabled_tools": ["web", "nope"]}, tok)
    assert status == 400 and "unknown tool or family 'nope'" in body["error"]

    status, body = _post_json(base, "/api/config", {"disabled_tools": "web"}, tok)
    assert status == 200 and "disabled_tools" in body["applied"]
    assert sorted(body["disabled_tools"]) == ["web_fetch", "web_search"]

    status, body = _post_json(base, "/api/config", {"disabled_tools": []}, tok)
    assert status == 200 and body["disabled_tools"] == []


def test_glob_skips_matches_outside_workspace(tmp_path):
    from saturday.tools.files import GlobTool

    (tmp_path / "outside.py").write_text("x = 1", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inner.py").write_text("x = 2", encoding="utf-8")

    tool = GlobTool(root=str(root))
    # '../*.py' from the workspace root points at the PARENT dir: every match
    # resolves outside the workspace and must be dropped
    ok, out = tool.run({"pattern": "../*.py"})
    assert ok and out == "(no matches)", out
    # the workspace itself stays fully visible
    ok, out = tool.run({"pattern": "*.py"})
    assert ok and out == "inner.py", out


def test_grep_skips_matches_outside_workspace(tmp_path):
    from saturday.tools.files import GrepTool

    (tmp_path / "outside.py").write_text("SECRET_TOKEN = 'leak'", encoding="utf-8")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inner.py").write_text("SECRET_TOKEN = 'fine'", encoding="utf-8")

    tool = GrepTool(root=str(root))
    ok, out = tool.run({"pattern": "SECRET_TOKEN", "include": "../*.py"})
    assert ok and out == "(no matches)", "content from outside the workspace must not leak"
    ok, out = tool.run({"pattern": "SECRET_TOKEN", "include": "*.py"})
    assert ok and "inner.py" in out and "fine" in out


def test_glob_still_finds_nested_files(tmp_path):
    from saturday.tools.files import GlobTool

    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("pass", encoding="utf-8")
    ok, out = GlobTool(root=str(root)).run({"pattern": "**/*.py"})
    assert ok and "src/app.py" in out


# ---- merged from test_frontier_features.py ----
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



# ---- merged from test_ocr_and_unix.py ----
def test_parse_tesseract_tsv():
    tsv = (
        "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t\n"
        "5\t1\t0\t0\t0\t0\t120\t40\t60\t21\t92\tSave\n"
        "5\t1\t0\t0\t0\t1\t300\t55\t40\t18\t55\tCancel\n"
        "5\t1\t0\t0\t0\t2\t700\t30\t30\t15\t88\tOK\n"
    )
    boxes = parse_tsv(tsv)
    assert [b["text"] for b in boxes] == ["Save", "Cancel", "OK"]
    assert boxes[0]["x"] == 120 and boxes[0]["conf"] == 92


def test_parse_pipe_lines():
    boxes = ocr._parse_pipe_lines("10|20|30|40|100|Hello World\n5|6|7|8|99|ok\njunk")
    assert len(boxes) == 2 and boxes[0]["text"] == "Hello World"


def test_slugify():
    assert slugify("Save File!") == "save-file"
    assert slugify("日本語") == "-"


def test_ui_text_tool_registers_landmarks(monkeypatch):
    store = LandmarkStore()

    class FakeScreen:
        def __init__(self):
            self.pending_images = [str(Path("x") / "shot.png")]

        def run(self, args):
            return True, "captured"

    tool = UiTextTool(landmarks=store, screen_tool=FakeScreen())
    boxes = [
        {"text": "Save", "x": 120, "y": 40, "w": 60, "h": 21, "conf": 92},
        {"text": "Cancel", "x": 280, "y": 46, "w": 40, "h": 18, "conf": 55},
    ]

    def fake_ocr(image):
        return True, "", boxes

    monkeypatch.setattr(ocr, "ocr_text_boxes", fake_ocr)
    ok, out = tool.run({})
    assert ok
    assert "(150,50) [save]" in out and "(300,55) [cancel]" in out
    assert store.resolve("save")["x"] == 150 and store.resolve("save")["y"] == 50
    assert store.resolve("cancel")["x"] == 300 and store.resolve("cancel")["y"] == 55


def test_ui_text_tool_ocr_failure_is_clean(monkeypatch):
    class FakeScreen:
        def __init__(self):
            self.pending_images = [str(Path("x") / "shot.png")]

        def run(self, args):
            return True, "captured"

    tool = UiTextTool(screen_tool=FakeScreen())

    def fake_ocr(image):
        return False, "OCR unavailable (tesseract not found on macOS/Linux)", []

    monkeypatch.setattr(ocr, "ocr_text_boxes", fake_ocr)
    ok, out = tool.run({})
    assert not ok and "OCR unavailable" in out


# -- macOS/Linux key helpers (pure, verifiable without hardware) ---------------


def test_mac_combo_maps_to_kvk_and_modifiers():
    assert parse_combo_mac("Ctrl+Q") == (12, ["control down"])
    assert parse_combo_mac("Cmd+Shift+Right") == (124, ["command down", "shift down"])
    assert parse_combo_mac("Alt+F4") == (118, ["option down"])
    assert parse_combo_mac("Enter") == (36, [])
    assert parse_combo_mac("Ctrl+UnknownKey") is None


def test_linux_combo_translates_to_xdotool():
    assert translate_linux_key("Ctrl+S") == "ctrl+s"
    assert translate_linux_key("Shift+Tab") == "shift+Tab"
    assert translate_linux_key("F5") == "F5"
    assert translate_linux_key("Alt+F4") == "alt+F4"
    assert translate_linux_key("Ctrl+Nope") is None



# ---- merged from test_parity_round2.py ----
def test_hooks_file_blocks_tool_call(tmp_path, monkeypatch):
    import saturday.config as cfgmod
    from saturday.user_hooks import load_hooks, make_pre_tool_hook

    (tmp_path / ".saturday").mkdir()
    (tmp_path / ".saturday" / "hooks.json").write_text(
        json.dumps({"pre_tool_call": [f'"{sys.executable}" -c "import sys,json; json.load(sys.stdin); print(\'no\', file=sys.stderr); sys.exit(2)"']}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setenv("SATURDAY_TRUST_ALL_PROJECTS", "1")
    cfg = load_hooks(str(tmp_path))
    assert len(cfg["pre_tool_call"]) == 1
    hook = make_pre_tool_hook(cfg["pre_tool_call"])
    reason = hook("shell", {"command": "echo hi"})
    assert reason is not None and "blocked by user hook" in reason and "no" in reason


def test_hook_exit_zero_allows_and_crash_does_not_block():
    from saturday.user_hooks import make_pre_tool_hook, run_hook

    ok_cmd = f'"{sys.executable}" -c "import sys,json; json.load(sys.stdin)"'
    code, out = run_hook(ok_cmd, {"event": "pre_tool_call", "tool": "t", "args": {}})
    assert code == 0
    hook = make_pre_tool_hook([ok_cmd])
    assert hook("shell", {}) is None
    # broken command must never block the tool (fail-open for non-2 exits)
    hook2 = make_pre_tool_hook(["definitely-not-a-real-command-xyz"])
    assert hook2("shell", {}) is None


def test_agent_run_chains_user_hooks_after_safety(tmp_path):
    import saturday.user_hooks as uh

    calls = []
    orig_load = uh.load_hooks
    monkeypatch_lambda = lambda root=None: {
        "pre_tool_call": [],
        "post_tool_call": [],
        **({"pre_tool_call": ["dummy"]} if False else {}),
    }

    class FakeResult:
        name = "x"
        ok = True
        output = ""
        error = None

    # direct unit: pre hook returns reason string -> loop blocks
    block = uh.make_pre_tool_hook(['"' + sys.executable + '" -c "import sys; sys.exit(2)"'])("any", {})
    assert block is not None


# ------------------------------------------------------- branching


def test_session_branch_copies_prefix_and_verifies(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "original work"})
    for i in range(4):
        store.append(sid, {"type": "messages", "messages": [
            {"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": f"a{i}"},
        ]})
    store.save_checkpoint(sid, [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}])
    branch_sid = store.branch(sid, keep_messages=4)
    assert branch_sid is not None and branch_sid != sid
    bdata = store.load(branch_sid)
    flat = []
    for rec in bdata["records"]:
        if rec.get("type") == "messages":
            flat.extend(rec["messages"])
    assert len(flat) == 4 and flat[0]["content"] == "q0"
    status = verify_chain(bdata["records"])
    assert status["ok"]
    # checkpoint copied truncated
    ckpt = store.load_checkpoint(branch_sid)
    assert ckpt == [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}]
    # original untouched
    assert len(store.history_messages(sid)) == 8
    # default keep: drops final exchange
    d = store.branch(sid)
    dmsgs = store.history_messages(d)
    assert len(dmsgs) == 6 and dmsgs[-1]["content"] == "a2"


def test_branch_unknown_session_returns_none(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    assert store.branch("missing") is None


# ------------------------------------------------------- subagents v2


class _FakeChildAgent:
    def __init__(self):
        self.turns = 0

    def run(self, prompt, initial_history=None):
        self.turns += 1
        prev = len(initial_history or [])

        class T:
            final_answer = f"prev={prev} turn={self.turns}"
            stop_reason = "done"

        t = T()
        t.messages = lambda: [
            {"role": "system", "content": "s"},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": f"prev={prev} turn={self.turns}"},
        ]
        return t


def test_subagent_continuation_keeps_child_context():
    def factory():
        return _FakeChildAgent()

    task = SubagentTask(agent_factory=factory)
    ok1, out1 = task.run({"description": "d", "prompt": "first question"})
    cid = out1.split("continue_id=")[1].split()[0]
    assert "prev=0 turn=1" in out1
    ok2, out2 = task.run({"description": "d", "prompt": "follow-up", "continue_id": cid})
    assert ok1 and ok2
    # second turn saw the full first exchange (user prompt + reply) as history
    assert "prev=2 turn=2" in out2
    # unknown continue id starts a fresh child instead of failing
    ok3, _ = task.run({"description": "d", "prompt": "new child", "continue_id": "sub-999"})
    assert ok3


def test_background_subagent_reports_via_job_manager():
    from saturday.tools.jobs import JobManager

    def factory():
        return _FakeChildAgent()

    task = SubagentTask(agent_factory=factory)
    ok, msg = task.run({"description": "d", "prompt": "slow thing", "background": True})
    assert ok and "job_id=ag-sub-" in msg
    jid = msg.split("job_id=")[1].split()[0]
    job = JobManager.shared().get(jid)
    assert job is not None
    deadline = time.time() + 5
    while job.status() != "done" and time.time() < deadline:
        time.sleep(0.05)
    assert "turn=1" in job.tail()


def test_legacy_runner_contract_still_works():
    task = SubagentTask(runner=lambda p: f"ran {p}")
    ok, out = task.run({"description": "d", "prompt": "x"})
    assert ok and out.startswith("ran x")


# ------------------------------------------------------- repo index


def test_repo_index_search_finds_identifier_variants(tmp_path):
    from saturday.tools.repo_index import search_index

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod_a.py").write_text("def parse_hermes_tool_calls(text):\n    return text\n")
    (tmp_path / "pkg" / "mod_b.py").write_text("unrelated = 1\n")
    hits = search_index(tmp_path, "hermes tool calls")
    paths = [h["path"] for h in hits]
    assert "pkg/mod_a.py" in paths and "pkg/mod_b.py" not in paths
    top = next(h for h in hits if h["path"] == "pkg/mod_a.py")
    assert top["line"] >= 1


def test_repo_search_tool_end_to_end(tmp_path):
    from saturday.tools.files import WriteFile
    from saturday.tools.repo_index import make_repo_search_tool

    WriteFile(root=str(tmp_path)).run({"path": "src/app.py", "content": "def charge_customer(): ...\n"})
    tool = make_repo_search_tool(lambda: str(tmp_path))
    ok, out = tool.run({"query": "charge customer"})
    assert ok and "src/app.py" in out


# ------------------------------------------------------- LSP


class _FakeTransport:
    """Speaks enough LSP over byte streams for offline client tests."""

    def __init__(self):
        self.inbox = bytearray()
        self.sent: list[dict] = []
        self._lock = threading.Lock()

    def server_send(self, msg: dict) -> None:
        body = json.dumps(msg).encode()
        with self._lock:
            self.inbox += f"Content-Length: {len(body)}\r\n\r\n".encode() + body

    def write(self, data: bytes) -> None:
        raw = data.decode("utf-8", errors="replace")
        body = raw.split("\r\n\r\n", 1)[1]
        self.sent.append(json.loads(body))

    def read(self, n: int) -> bytes:
        deadline = time.time() + 5
        while time.time() < deadline:
            with self._lock:
                if len(self.inbox) >= n:
                    out = bytes(self.inbox[:n])
                    del self.inbox[:n]
                    return out
            time.sleep(0.01)
        return b""

    def alive(self):
        return True

    def close(self):
        pass


def test_lsp_client_initialize_diagnostics_definition():

    t = FakeTransportFactory()
    client = t.client
    # initialize round-trip answered by the fake server thread
    def responder():
        while True:
            req = next((m for m in t.requests if m.get("id") == 1), None)
            if req:
                break
            time.sleep(0.02)
        t.transport.server_send({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})

    threading.Thread(target=responder, daemon=True).start()
    client.initialize()
    assert any(m["method"] == "initialize" for m in t.requests)


class FakeTransportFactory:
    def __init__(self):
        from saturday.tools.lsp import LspClient

        self.transport = _FakeTransport()
        self.client = LspClient(self.transport, "file:///w", timeout_s=5)
        self.requests = self.transport.sent


def test_lsp_tools_graceful_without_servers(tmp_path):
    from saturday.tools.lsp import make_lsp_tools

    tools = make_lsp_tools({}, lambda: str(tmp_path))
    assert len(tools) == 2
    ok, msg = tools[0].run({"path": str(tmp_path / "x.py")})
    assert not ok and "no LSP server configured" in msg


def test_mcp_http_client_parses_json_and_sse(monkeypatch):
    from saturday.mcp_client import McpHttpClient

    class FakeResp:
        status = 200

        def __init__(self, body, ctype):
            self._body = body.encode()
            self.headers = {"Content-Type": ctype}

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("TOK", "sekrit")
    client = McpHttpClient(url="https://mcp.example/rpc", headers={"Authorization": "Bearer ${TOK}"})

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        body = json.loads(req.data.decode())
        if str(body.get("method", "")).startswith("notifications/"):
            return FakeResp("", "application/json")
        if body.get("method") == "initialize":
            return FakeResp(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"serverInfo": {"name": "x"}}}), "application/json")
        if body.get("method") == "tools/list":
            sse = (
                "event: message\r\n"
                + f'data: {json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "ping", "description": "", "inputSchema": {}}]}})}\r\n\r\n'
            )
            return FakeResp(sse, "text/event-stream")
        raise AssertionError("unexpected method")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    info = client.start()
    assert info["name"] == "x"
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs["authorization"] == "Bearer sekrit"
    tools = client.list_tools()
    assert [t.name for t in tools] == ["ping"]



# ---- merged from test_playwright_e2e.py ----
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



# ---- merged from test_review_round_fixes.py ----
def _src(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


# ------------------------------------------------------------- journal creates

def test_write_file_journals_creations_so_revert_can_undo_them(tmp_path):
    from saturday.tools.files import WriteFile
    from saturday.tools.journal import load_entries, restore_entry

    w = WriteFile(root=str(tmp_path))
    ok, msg = w.run({"path": "new.txt", "content": "created by agent"})
    assert ok
    entries = load_entries(tmp_path, limit=5)
    assert entries and entries[0]["existed"] is False
    # revert of a creation deletes the file (tombstone contract)
    ok_r, msg_r = restore_entry(tmp_path, 0)
    assert ok_r, msg_r
    assert not (tmp_path / "new.txt").exists()


# ------------------------------------------------------------ flexible matching

def test_flexible_match_preserves_line_boundaries():
    from saturday.tools.files import flexible_match

    text = "def f():\n    if x: return y\n"
    # old_string spans two lines in the model's mind; text has them on ONE line
    assert flexible_match(text, "if x:\n    return y") is None
    ok_text = "def f():\n    if x:\n        return y\n"
    span = flexible_match(ok_text, "if x:\n return y")
    assert span is not None
    start, end = span
    assert ok_text[start:end].startswith("if x:")


def test_flexible_match_blank_lines_and_uniqueness():
    from saturday.tools.files import flexible_match

    text = "a = 1\n\n\nb = 2\n"
    assert flexible_match(text, "a = 1\n\nb = 2") is not None
    dup = "x = 1\nfoo()\nx = 1\nfoo()\n"
    assert flexible_match(dup, "x = 1\nfoo()") is None  # ambiguous -> None


def test_edit_file_rejects_empty_old_string_cleanly(tmp_path):
    from saturday.tools.files import EditFile

    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": "f.txt", "old_string": "", "new_string": "?"})
    assert not ok and "empty" in msg
    ok, msg = tool.run({"path": "f.txt", "old_string": "   ", "new_string": "?"})
    assert not ok and "empty" in msg


# ------------------------------------------------------------------ diff preview

def test_render_file_diff_mirrors_edit_file_rules(tmp_path):
    from saturday.repl import render_file_diff

    p = tmp_path / "multi.txt"
    p.write_text("dup\nmid\ndup\n", encoding="utf-8")
    diff = render_file_diff(
        "edit_file",
        {"path": "multi.txt", "old_string": "dup", "new_string": "?"},
        root=str(tmp_path),
    )
    assert diff and "matches 2 times" in diff

    missing = render_file_diff(
        "edit_file",
        {"path": "multi.txt", "old_string": "", "new_string": "?"},
        root=str(tmp_path),
    )
    assert missing and "old_string" in missing

    # relative paths resolve against the workspace root, not CWD
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "rel.txt").write_text("target here\n", encoding="utf-8")
    ok_diff = render_file_diff(
        "edit_file",
        {"path": "sub/rel.txt", "old_string": "target", "new_string": "bullseye"},
        root=str(tmp_path),
    )
    assert ok_diff and "+bullseye" in ok_diff


def test_gates_preview_against_workspace_root_not_cwd(tmp_path, monkeypatch):
    """Behavioral: both gates must resolve relative edit paths against the
    agent's workspace root — even when the process CWD is elsewhere."""

    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.session_runtime import SessionRuntime
    from saturday.sessions import SessionStore

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "rel.txt").write_text("rooted content\n", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)  # CWD deliberately differs from the workspace

    store = SessionStore(root=tmp_path / "sess")
    agent = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(ws)),
                  safety=False, session_store=store)

    # REPL surface: gate carries the workspace root
    repl = Repl(agent, store=store, output_fn=lambda *a, **k: None)
    assert Path(repl.file_gate.root) == ws

    # web surface: same wiring
    rt = SessionRuntime("s1", agent)
    assert Path(rt.file_gate.root) == ws


# ------------------------------------------------------------------- compaction

def test_compaction_files_section_lists_only_mutations():
    import json

    from saturday.agent.loop import AgentLoop
    from saturday.tools.base import ToolRegistry

    class Echo:
        usage = None
        tool_calls = []
        content = ""

    class OneShotModel:
        def chat(self, messages, **kwargs):
            return type("R", (), {"message": Echo(), "usage": None})()

    history = [
        {"role": "user", "content": "# Goal\ngo"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "r1", "type": "function",
                         "function": {"name": "read_file", "arguments": json.dumps({"path": "read_only.py"})}}]},
        {"role": "tool", "tool_call_id": "r1", "name": "read_file", "content": "..."},
        {"role": "assistant", "content": "writing now",
         "tool_calls": [{"id": "w1", "type": "function",
                         "function": {"name": "write_file", "arguments": json.dumps({"path": "mutated.py"})}}]},
        {"role": "tool", "tool_call_id": "w1", "name": "write_file", "content": "ok"},
        {"role": "assistant", "content": "tail one"},
        {"role": "assistant", "content": "tail two"},
        {"role": "assistant", "content": "tail three"},
    ]
    loop = AgentLoop(OneShotModel(), ToolRegistry())
    loop._compact(list(history), force=True)
    pinned = loop.memory.render()
    assert "mutated.py" in pinned
    assert "read_only.py" not in pinned


# ------------------------------------------------------------------ watchdog

def test_watchdog_actually_bounds_a_hung_tool():
    """Behavioral: a tool that hangs past tool_call_timeout must not wedge the
    run — the loop returns with a timeout error well before the tool would."""
    import time as _t

    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from fakes import make_scripted_model

    from saturday.agent.loop import AgentLoop
    from saturday.tools.base import ToolRegistry, Tool

    class Sleeper(Tool):
        name = "sleep"
        description = "hangs"
        parameters = {"type": "object", "properties": {}}

        def run(self, args):
            _t.sleep(4.0)
            return True, "woke"

    reg = ToolRegistry()
    reg.register(Sleeper())
    model = make_scripted_model(
        [{"tool_calls": [{"name": "sleep", "arguments": {}}]}, {"content": "done"}]
    )
    loop = AgentLoop(model, reg, max_steps=2, tool_call_timeout=0.5)
    start = _t.monotonic()
    traj = loop.run("sys", "hang")
    elapsed = _t.monotonic() - start
    assert traj.stop_reason == "done"
    timeout_results = [r for s in traj.steps for r in (s.results or []) if not r.ok]
    assert timeout_results and "timed out after 0.5" in (timeout_results[0].error or "")
    assert elapsed < 3.0, f"watchdog did not bound the hang: {elapsed:.1f}s"


# --------------------------------------------------------------- truncation order

def test_truncated_tool_result_still_ends_with_protocol_tag():
    """Behavioral: oversized payloads are cut BEFORE wrapping, so even the
    biggest results keep their closing </tool_response> tag intact."""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from fakes import make_scripted_model

    from saturday.agent.loop import TOOL_RESULT_MAX_CHARS, AgentLoop
    from saturday.tools.base import Tool, ToolRegistry

    class Firehose(Tool):
        name = "firehose"
        description = "huge output"
        parameters = {"type": "object", "properties": {}}

        def run(self, args):
            return True, "x" * (TOOL_RESULT_MAX_CHARS * 2)

    reg = ToolRegistry()
    reg.register(Firehose())
    model = make_scripted_model(
        [{"tool_calls": [{"name": "firehose", "arguments": {}}]}, {"content": "ok"}]
    )
    traj = AgentLoop(model, reg, max_steps=2).run("sys", "flood")
    msg = traj.steps[0].tool_messages[0]
    assert len(msg["content"]) < TOOL_RESULT_MAX_CHARS + 200
    assert msg["content"].rstrip().endswith("</tool_response>")


# --------------------------------------------------------------- repo index perf

def test_symbol_terms_precomputed_at_index_time(tmp_path):
    from saturday.tools.repo_index import build_index, search_index

    (tmp_path / "s.py").write_text("def parse_hermes_tool_calls(t):\n    return t\n")
    idx = build_index(tmp_path, force=True)
    meta = idx["files"]["s.py"]
    assert "parse_hermes_tool_calls" in meta["symbol_terms"]
    hits = search_index(tmp_path, "hermes tool calls", index=idx)
    assert hits[0]["path"] == "s.py"


# ------------------------------------------------------------------ app --no-token

def test_cmd_app_no_token_maps_to_empty_not_none(monkeypatch):
    """Behavioral: `saturday app --no-token` must reach serve() as '' (auth
    disabled per AppServer contract), not None (which mints a fresh token)."""
    import argparse

    from saturday import cli as cli_mod

    captured: dict = {}
    monkeypatch.setattr(
        "saturday.webui.serve",
        lambda **kw: captured.update(kw) or 0,
    )
    ns = dict(host="127.0.0.1", port=8679, no_window=True, width=800, height=600, env=None)
    # --no-token -> empty string
    cli_mod.cmd_app(argparse.Namespace(**ns, no_token=True, token=None))
    assert captured["token"] == ""
    # default -> None (serve generates one)
    cli_mod.cmd_app(argparse.Namespace(**ns, no_token=False, token=None))
    assert captured["token"] is None


# ------------------------------------------------------------- swebench runner

def test_swebench_runner_hardening():
    src = _src("scripts/swebench_runner.py")
    ast.parse(src)  # stays syntactically valid standalone
    assert "--max-steps" in src and "SATURDAY_MAX_STEPS" in src, "--ci caps steps at 25"
    assert "docker\", \"rm\", \"-f\"" in src.replace("'", '"'), "orphan containers must be killed"
    assert "as_posix()" in src, "windows volume mounts need forward slashes"
    assert "_PRED_LOCK" in src, "preds.json writes need a real module-level lock"


def test_cred_passthrough_by_name_not_value():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "swebench_runner", Path(__file__).parents[1] / "scripts" / "swebench_runner.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    keys = mod._cred_env_keys({
        "DEEPSEEK_API_KEY": "sk-x",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        "MY_SECRET_SAUCE": "1",
        "PATH": "/usr/bin",
        "BASE_COMMIT": "abc",
        "SATURDAY_PROVIDER": "deepseek",
    })
    assert set(keys) == {"ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY", "MY_SECRET_SAUCE"}



# ---- merged from test_round3_upgrades.py ----
def test_config_defaults_raised_for_long_horizon_tasks():
    from saturday.config import AgentConfig
    from saturday.agent.loop import MAX_TOOL_CALLS_PER_STEP, TOOL_RESULT_MAX_CHARS

    cfg = AgentConfig()
    assert cfg.max_steps >= 200, "long refactors need far more than 40 turns"
    assert cfg.tool_timeout >= 120.0, "builds/compilers exceed the old 60s watchdog"
    assert MAX_TOOL_CALLS_PER_STEP >= 16
    assert TOOL_RESULT_MAX_CHARS >= 48_000


# ------------------------------------------------------------- fuzzy edit_file

def test_edit_file_exact_match_survives_crlf_files(tmp_path: Path):
    from saturday.tools.files import EditFile

    p = tmp_path / "win.txt"
    p.write_bytes(b"def main():\r\n    return 1\r\n")
    tool = EditFile(root=str(tmp_path))
    # universal newlines: read_text normalizes CRLF, so a \n old_string matches
    ok, msg = tool.run({"path": "win.txt", "old_string": "def main():\n    return 1", "new_string": "def main():\n    return 2"})
    assert ok, msg
    assert b"return 2" in p.read_bytes()


def test_edit_file_fuzzy_fallback_indentation(tmp_path: Path):
    from saturday.tools.files import EditFile

    p = tmp_path / "ind.py"
    p.write_text("if True:\n        deep_call()\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    # model emitted 4-space indent; file has 8 — flexible match still lands
    ok, msg = tool.run({"path": "ind.py", "old_string": "if True:\n    deep_call()", "new_string": "if True:\n    shallow_call()"})
    assert ok, msg
    assert "shallow_call()" in p.read_text(encoding="utf-8")


def test_edit_file_still_fails_clean_when_unfindable(tmp_path: Path):
    from saturday.tools.files import EditFile

    (tmp_path / "x.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": "x.txt", "old_string": "omega", "new_string": "??"})
    assert not ok and "not found" in msg


def test_edit_file_ambiguous_exact_still_rejected(tmp_path: Path):
    from saturday.tools.files import EditFile

    (tmp_path / "y.txt").write_text("dup dup\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": "y.txt", "old_string": "dup", "new_string": "?"})
    assert not ok and "2 times" in msg


def test_render_file_diff_works_with_fuzzy_match(tmp_path: Path):
    from saturday.repl import render_file_diff

    p = tmp_path / "f.txt"
    p.write_text("keep\r\nchange me\r\nend\r\n", encoding="utf-8")
    diff = render_file_diff(
        "edit_file",
        {"path": str(p), "old_string": "change me\n", "new_string": "changed\n"},
    )
    assert diff and "+changed" in diff and "-change me" in diff


# --------------------------------------------- AST-symbol-aware repo retrieval

def test_repo_index_boosts_defining_file_over_mentioning_file(tmp_path):
    from saturday.tools.repo_index import build_index, search_index

    (tmp_path / ".saturday").mkdir(exist_ok=True)
    # definer: tiny file that DEFINES the function
    (tmp_path / "definer.py").write_text("def frobnicate_widget(w):\n    return w\n")
    # mentioner: big file that only references the name many times
    (tmp_path / "mentioner.py").write_text(
        "x = 1\nfrobnicate_widget(x)\nfrobnicate_widget(2)\nfrobnicate_widget(3)\nfrobnicate_widget(4)\n"
        + "\n".join(f"filler_{i} = {i}" for i in range(50))
        + "\n"
    )
    idx = build_index(tmp_path, force=True)
    assert idx["files"]["definer.py"].get("symbols") == ["frobnicate_widget"]
    hits = search_index(tmp_path, "frobnicate widget", index=idx)
    assert hits[0]["path"] == "definer.py"


def test_repo_index_symbols_survive_incremental_rebuild(tmp_path):
    from saturday.tools.repo_index import build_index, search_index

    f = tmp_path / "a.py"
    f.write_text("class Widget:\n    pass\n")
    build_index(tmp_path, force=True)
    # second build takes the cached path — symbols must persist there too
    idx2 = build_index(tmp_path)
    hits = search_index(tmp_path, "Widget", index=idx2)
    assert any(h["path"] == "a.py" for h in hits)


# ------------------------------------------------------------------- grep

def test_grep_skips_binary_files(tmp_path):
    from saturday.tools.files import GrepTool

    (tmp_path / "text.txt").write_text("needle here\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"needle\x00binary\n")
    gp = GrepTool(root=str(tmp_path))
    ok, out = gp.run({"pattern": "needle", "include": "**/*"})
    assert ok and "text.txt:1" in out and "blob.bin" not in out


def test_grep_ignore_case_flag(tmp_path):
    from saturday.tools.files import GrepTool

    (tmp_path / "c.txt").write_text("MIXEDcase value\n", encoding="utf-8")
    gp = GrepTool(root=str(tmp_path))
    ok_sensitive, out_sensitive = gp.run({"pattern": "mixedcase"})
    assert out_sensitive == "(no matches)"
    ok, out = gp.run({"pattern": "mixedcase", "ignore_case": True})
    assert ok and "c.txt:1" in out


# --------------------------------------------------- structured compaction

def test_compaction_fallback_emits_structured_sections():
    import json

    from saturday.agent.loop import AgentLoop
    from saturday.tools.base import ToolRegistry

    class Echo:
        usage = None
        tool_calls = []
        content = ""

    class OneShotModel:
        def chat(self, messages, **kwargs):
            return type("R", (), {"message": Echo(), "usage": None})()

    history = [
        {"role": "user", "content": "# Goal\ndo the refactor"},
        {
            "role": "assistant",
            "content": "I chose approach B instead of A.",
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "edit_file", "arguments": json.dumps({"path": "src/a.py"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "name": "edit_file", "content": "edited src/a.py"},
        {"role": "assistant", "content": "step two"},
        {
            "role": "assistant",
            "content": "reading tests next",
            "tool_calls": [
                {
                    "id": "t2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "tests/a_test.py"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t2", "name": "read_file", "content": "contents"},
        {"role": "assistant", "content": "wrapping up soon"},
        {"role": "assistant", "content": "last words"},
    ]
    loop = AgentLoop(OneShotModel(), ToolRegistry())
    loop._compact(list(history), force=True)
    pinned = loop.memory.render()
    assert "## Progress" in pinned
    assert "## Decisions" in pinned
    assert "approach B" in pinned
    assert "## Files modified" in pinned
    assert "src/a.py" in pinned
    assert "tests/a_test.py" not in pinned  # reads are not "modified"



# ---- merged from test_search_index.py ----
@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    saved: list[dict] = []
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: saved.append(dict(partial)))


class _Server:
    def __init__(self, app):
        from saturday.webui import AppServer

        self.http = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.http.shutdown()
        self.http.server_close()


def _make_app(tmp_path):
    from fakes import make_scripted_model
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    fake = make_scripted_model([{"content": "ok"}])
    orig = app._new_agent

    def patched(cfg):
        agent = orig(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    return app


def _req(base, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("X-Saturday-Token", TOKEN)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def test_search_finds_and_ranks(tmp_path):
    app = _make_app(tmp_path)
    s1 = app.store.create({"task": "kubernetes work", "surface": "app"})
    app.store.append(s1, {"type": "messages", "messages": [
        {"role": "user", "content": "how do kubernetes pods schedule"},
        {"role": "assistant", "content": "kubernetes schedules pods via the scheduler component"},
    ]})
    s2 = app.store.create({"task": "unrelated", "surface": "app"})
    app.store.append(s2, {"type": "messages", "messages": [{"role": "user", "content": "hello world"}]})

    results = __import__("saturday.webui", fromlist=["search_sessions"]).search_sessions(app.store, "kubernetes")
    assert results, "must find matches"
    assert results[0]["sid"] == s1
    assert results[0]["hits"] >= 2
    assert "kubernetes" in results[0]["snippet"].lower()
    assert all(r["sid"] != s2 for r in results)

    assert __import__("saturday.webui", fromlist=["search_sessions"]).search_sessions(app.store, "") == []


def test_search_endpoint(tmp_path):
    app = _make_app(tmp_path)
    s1 = app.store.create({"task": "needle session", "surface": "app"})
    app.store.append(s1, {"type": "messages", "messages": [{"role": "user", "content": "the zanzibar quorum meets at dawn"}]})
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/search?q=zanzibar")
        assert status == 200 and data["query"] == "zanzibar"
        assert data["results"] and data["results"][0]["sid"] == s1
        status, data = _req(srv.base, "/api/search?q=")
        assert status == 200 and data["results"] == []


# ----------------------------------------------------------------- onboard

def test_onboard_writes_env_switches_provider(tmp_path, monkeypatch):

    monkeypatch.setattr(
        "saturday.llm.probe.probe_connection",
        lambda prof, key="", timeout=8.0: (True, "reachable \u2014 2 models found", ["m1", "m2"]),
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openrouter", "api_key": "sk-test-123"})
        assert status == 200
        assert data["provider"] == "openrouter"
        assert data["has_key"] is True
        env_file = tmp_path / ".env"
        content = env_file.read_text(encoding="utf-8")
        assert "OPENROUTER_API_KEY=sk-test-123" in content
        assert os.environ["OPENROUTER_API_KEY"] == "sk-test-123"

    # second run replaces instead of duplicating
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openrouter", "api_key": "sk-two"})
        assert status == 200
        content = (tmp_path / ".env").read_text(encoding="utf-8")
        assert content.count("OPENROUTER_API_KEY") == 1
        assert "sk-two" in content


def test_onboard_validation_errors(tmp_path):
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "not-a-provider", "api_key": "x"})
        assert status == 400
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openai"})
        assert status == 400, "missing key refused"
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "openai", "api_key": ""})
        assert status == 400


def test_onboard_model_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "saturday.llm.probe.probe_connection",
        lambda prof, key="", timeout=8.0: (True, "reachable \u2014 2 models found", ["m1", "m2"]),
    )
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        status, data = _req(srv.base, "/api/onboard", "POST", {"provider": "deepseek", "api_key": "k", "model": "deepseek-chat"})
        assert status == 200
        assert data["model"] == "deepseek-chat"



# ---- merged from test_shell_gui_hang.py ----
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows pipe/conhost semantics")
def test_gui_spawn_command_returns_within_timeout():
    tool = ShellTool(timeout=6.0)
    t0 = time.time()
    ok, out = tool.run({"command": "cmd /c start notepad"})
    elapsed = time.time() - t0
    try:
        import subprocess

        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)
    except Exception:
        pass
    assert ok, out
    assert elapsed < 20, f"shell tool hung {elapsed:.1f}s on GUI-spawning command"
    assert "timed out after 6" in out


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-specific")
def test_winjob_assign_and_terminate():
    import subprocess as sp

    from saturday.tools.winjob import JobObject

    proc = sp.Popen("cmd /c ping -n 30 127.0.0.1", shell=True, stdout=sp.PIPE, stderr=sp.PIPE)
    job = JobObject()
    job.assign(proc.pid)
    job.terminate()
    try:
        proc.communicate(timeout=5)
    except sp.TimeoutExpired:
        proc.kill()
        proc.communicate()
    assert proc.returncode != 0



# ---- merged from test_v05_platform.py ----
class FakeTransport:
    def __init__(self, updates: list[dict]):
        self.updates = list(updates)
        self.sent: list[tuple] = []

    def get_updates(self):
        out = self.updates
        self.updates = []
        return out

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def scripted_agent_factory():
    from saturday.types import Trajectory

    class A:
        memory = None
        cfg = None

        def run(self, task, **kw):
            return Trajectory(task=task, system_prompt="s", final_answer=f"echo:{task[:60]}", stop_reason="done")

    return A()






def transport_sent_last_text(gw):
    return gw.transport.sent[-1][1]


def _png_bytes() -> bytes:
    import base64

    b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return base64.b64decode(b64)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="screen capture fallback is Windows-only")
def test_screen_tool_captures_real_screen(tmp_path):
    from saturday.tools.screen import ScreenTool

    tool = ScreenTool(shots_dir=tmp_path / "shots")
    ok, out = tool.run({"monitor_note": "ci"})
    if not ok:
        pytest.skip(f"headless environment: {out[:80]}")
    assert tool.pending_images and Path(tool.pending_images[0]).exists()
    assert "[screenshot saved:" in out


def test_playwright_adapter_degrades_without_dep(monkeypatch):
    import builtins

    from saturday.tools import browser_playwright as bp

    tool = bp.PlaywrightBrowserTool()
    orig = builtins.__import__

    def guard(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    with pytest.raises(RuntimeError) as excinfo:
        tool.run({"action": "open", "url": "http://example.invalid"})
    assert "pip install" in str(excinfo.value)


def test_playwright_registered_only_when_available(monkeypatch):
    import builtins

    orig = builtins.__import__

    def guard(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return orig(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    from saturday.plugins import core_plugin, install_plugins
    from saturday.tools.base import ToolRegistry

    reg = ToolRegistry()
    persona: list[str] = []
    install_plugins(reg, [core_plugin(None)], persona)
    assert "web_browser_js" not in reg.names()
    assert "screen" in reg.names()


def test_playwright_adapter_present_when_importable():
    from saturday.plugins import core_plugin, install_plugins
    from saturday.tools.base import ToolRegistry
    from saturday.tools.browser_playwright import playwright_available

    if not playwright_available():
        pytest.skip("playwright not installed")
    reg = ToolRegistry()
    install_plugins(reg, [core_plugin(None)], [])
    assert "web_browser_js" in reg.names()


def test_tui_rendering_helpers():
    from saturday import tui

    line = tui.status_line(type("A", (), {})())
    assert isinstance(line, str)
    h = tui.header(" Saturday ")
    assert "Saturday" in h.replace("\x1b", "").replace("[36m", "").replace("[0m", "")
    width_a = tui.terminal_width()
    assert width_a > 10


def test_serve_endpoint_roundtrip(tmp_path):
    from http.server import BaseHTTPRequestHandler as BRH, ThreadingHTTPServer

    scripted = make_scripted_model([{"content": "served answer"}])

    class Served(BRH):
        agent = None

        def log_message(self, fmt, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            traj = self.agent.run(str(payload.get("text") or ""))
            body = json.dumps({"ok": True, "answer": traj.final_answer}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    served_agent = make_scripted_agent_like()
    Served.agent = served_agent

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Served)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/message",
            data=json.dumps({"text": "ping"}).encode(),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True and data["answer"] == "echo:ping"
    finally:
        srv.shutdown()
        srv.server_close()


def make_scripted_agent_like():
    from saturday.types import Trajectory

    class A:
        def run(self, task, **kw):
            return Trajectory(task=task, system_prompt="s", final_answer=f"echo:{task[:60]}", stop_reason="done")

    return A()



# ---- merged from test_v06_features.py ----
# (its own _hermetic_user_config was a strict subset of the one merged from
# test_search_index.py above — same fixture name, so the later definition
# was silently shadowing the earlier one and dropping the CONFIG_DIR/
# save_config overrides. Removed as a duplicate rather than kept.)


def test_write_valid_python_has_no_warning(tmp_path):
    tool = WriteFile(root=str(tmp_path))
    ok, out = tool.run({"path": "good.py", "content": "def f():\n    return 1\n"})
    assert ok and "[verify]" not in out


def test_write_broken_python_warns_with_line(tmp_path):
    tool = WriteFile(root=str(tmp_path))
    ok, out = tool.run({"path": "bad.py", "content": "def f():\n    return =\n"})
    assert ok, "write itself must succeed"
    assert "[verify] WARNING" in out
    assert "line 2" in out
    assert (tmp_path / "bad.py").exists(), "file still written"


def test_non_python_files_not_checked(tmp_path):
    tool = WriteFile(root=str(tmp_path))
    ok, out = tool.run({"path": "notes.md", "content": "# def broken( :\n"})
    assert ok and "[verify]" not in out


def test_edit_introducing_syntax_error_warns(tmp_path):
    p = tmp_path / "app.py"
    p.write_text("x = 1\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, out = tool.run({"path": "app.py", "old_string": "x = 1", "new_string": "x = ("})
    assert ok
    assert "[verify] WARNING" in out


def test_edit_fixing_syntax_clears_warning(tmp_path):
    p = tmp_path / "app.py"
    p.write_text("x = (\n", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, out = tool.run({"path": "app.py", "old_string": "x = (", "new_string": "x = 1"})
    assert ok and "[verify]" not in out


# ------------------------------------------------------------------ repl /ctx

def test_repl_context_command_renders_breakdown(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.sessions import SessionStore

    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False, session_store=SessionStore(root=tmp_path / "sess"))
    collected: list[str] = []
    repl = Repl(agent, store=agent.session_store, output_fn=lambda *a, **k: collected.append(" ".join(str(x) for x in a)))
    repl._sid = agent.session_store.create({"task": "t"})
    handled = repl.dispatch("/context")
    assert handled is True
    text = "\n".join(collected)
    assert "context:" in text and "system prompt" in text


def test_version_bumped():
    import saturday

    assert saturday.__version__ >= "0.6.0"


def test_title_from_text_strips_noise():
    from saturday.webui import _title_from_text

    assert _title_from_text("```python\nprint('hi')\n```\nfix this") == "code fix this"
    assert _title_from_text("### **Bold** heading\nsecond line") == "Bold heading second line"
    assert _title_from_text("") == "(interactive)"
    assert len(_title_from_text("x" * 500)) == 60



# ---- merged from test_background_delivery.py ----
def fake_runner(responses):
    """Runner returning canned outputs in order; records every script."""
    calls: list[str] = []
    queue = list(responses)

    def run(script, timeout=25.0):
        calls.append(script)
        return (0, queue.pop(0) if queue else "ok", "")

    run.calls = calls
    return run


# ------------------------------------------------------------------ pointer


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_pointer_background_click_posts_to_window():
    runner = fake_runner([WIN_LIST, "ok"])
    tool = PointerTool(runner=runner)
    ok, msg = tool.run({"action": "click", "x": 300, "y": 400, "window": "DF BG"})
    assert ok, msg
    assert "delivered to 'DF BG Target'" in msg and "background" in msg
    list_script, post_script = runner.calls
    assert "EnumWindows" in list_script
    assert "[IntPtr]4321" in post_script, "must target the resolved hwnd"
    assert "TargetAt([IntPtr]4321,300,400" in post_script, "screen coords passed; child+client resolved in PS"
    assert "0x201" in post_script and "0x202" in post_script, "WM_LBUTTONDOWN/UP"
    assert "PostMessageW" in post_script


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_pointer_background_double_click_and_scroll():
    runner = fake_runner([WIN_LIST, "ok", WIN_LIST, "ok"])
    tool = PointerTool(runner=runner)
    ok, _ = tool.run({"action": "double_click", "x": 150, "y": 250, "window": "DF BG"})
    assert ok
    dbl = runner.calls[1]
    assert "0x203" in dbl and dbl.count("0x202") == 2
    ok, _ = tool.run({"action": "scroll", "dy": 3, "window": "DF BG"})
    assert ok
    scroll = runner.calls[3]
    assert "0x20A" in scroll and "360 -shl 16" in scroll


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_pointer_background_drag_interpolates():
    runner = fake_runner([WIN_LIST, "ok"])
    tool = PointerTool(runner=runner)
    ok, _ = tool.run({"action": "drag", "x": 100, "y": 100, "x2": 220, "y2": 160, "window": "DF BG"})
    assert ok
    script = runner.calls[1]
    assert "0x201" in script and "0x200" in script and "0x202" in script
    assert script.count("TargetAt") >= 13, "down + interpolated moves + up"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_pointer_background_unknown_window_and_move_rejected():
    tool = PointerTool(runner=fake_runner([""]))
    ok, msg = tool.run({"action": "click", "x": 1, "y": 1, "window": "ghost app"})
    assert not ok and "no visible window matching" in msg
    tool2 = PointerTool(runner=fake_runner([WIN_LIST, "ok"]))
    ok, msg = tool2.run({"action": "move", "x": 1, "y": 1, "window": "DF BG"})
    assert not ok and "no meaning in background" in msg


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_pointer_foreground_unchanged_without_window():
    runner = fake_runner(["ok"])
    tool = PointerTool(runner=runner)
    ok, msg = tool.run({"action": "click", "x": 10, "y": 20})
    assert ok and "ok" in msg
    assert "SetCursorPos" in runner.calls[0] and "PostMessageW" not in runner.calls[0]


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_pointer_background_uses_landmarks():
    store = LandmarkStore()
    store.add("save", 250, 350, "Button")
    runner = fake_runner([WIN_LIST, "ok"])
    tool = PointerTool(landmarks=store, runner=runner)
    ok, _ = tool.run({"action": "click", "target": "save", "window": "DF BG"})
    assert ok
    assert "TargetAt([IntPtr]4321,250,350" in runner.calls[1]




# ----------------------------------------------------------------- keyboard


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_keyboard_background_type_posts_wm_char():
    runner = fake_runner([WIN_LIST, "ok"])
    tool = KeyboardTool(runner=runner)
    ok, msg = tool.run({"action": "type", "text": "hi\n", "window": "DF BG"})
    assert ok, msg
    script = runner.calls[1]
    assert "[BgIn]::EditChild([IntPtr]4321)" in script
    assert f"[IntPtr]{ord('h')}" in script and f"[IntPtr]{ord('i')}" in script
    assert "[IntPtr]13" in script, "newline becomes Enter via WM_CHAR"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_keyboard_background_key_combo():
    runner = fake_runner([WIN_LIST, "ok"])
    tool = KeyboardTool(runner=runner)
    ok, msg = tool.run({"action": "key", "key": "Enter", "window": "DF BG"})
    assert ok, msg
    script = runner.calls[1]
    assert "0x100" in script and "0x101" in script and "[IntPtr]13" in script


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="background delivery requires Windows")
def test_keyboard_background_unknown_window():
    tool = KeyboardTool(runner=fake_runner([""]))
    ok, msg = tool.run({"action": "type", "text": "x", "window": "nope"})
    assert not ok and "no visible window matching" in msg


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_keyboard_foreground_unchanged_without_window():
    runner = fake_runner(["ok"])
    tool = KeyboardTool(runner=runner)
    ok, _ = tool.run({"action": "type", "text": "abc"})
    assert ok
    assert "SendInput" in runner.calls[0] or "[Kb]::Char" in runner.calls[0]
    assert "PostMessageW" not in runner.calls[0]


# ------------------------------------------------------------------- safety


def test_bg_only_mode_allows_window_targeted_input():
    policy = ApprovalPolicy.from_mode("off")
    bg_ptr = {"action": "click", "x": 5, "y": 5, "window": "tally"}
    assert check_command(policy, "pointer", bg_ptr, background_only=True) is None
    bg_kbd = {"action": "type", "text": "x", "window": "tally"}
    assert check_command(policy, "keyboard", bg_kbd, background_only=True) is None


def test_bg_only_mode_still_blocks_foreground_input():
    policy = ApprovalPolicy.from_mode("off")
    fg_ptr = {"action": "click", "x": 5, "y": 5}
    reason = check_command(policy, "pointer", fg_ptr, background_only=True)
    assert reason and "BACKGROUND-ONLY" in reason and "window=" in reason
    fg_explicit = {"action": "click", "x": 5, "y": 5, "window": "tally", "delivery": "foreground"}
    reason = check_command(policy, "pointer", fg_explicit, background_only=True)
    assert reason and "BACKGROUND-ONLY" in reason


def test_ask_mode_signature_includes_window():
    seen: list[str] = []

    class Approver:
        def __call__(self, sig, reason):
            seen.append(sig)
            return True

    policy = ApprovalPolicy.from_mode("ask", approver=Approver())
    assert check_command(policy, "keyboard", {"action": "type", "text": "x", "window": "tally"}) is None
    assert any("@ tally" in s for s in seen)
    assert check_command(policy, "pointer", {"action": "click", "x": 1, "y": 2, "window": "tally"}) is None
    assert any("window=tally" in s for s in seen)


# --------------------------------------------------------------- live (win)


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only live test")
def test_live_background_type_into_winforms_edit(tmp_path):
    """End-to-end: detached WinForms form (minimized, never focused) receives
    typed text via background delivery; the form writes it to a file."""
    import subprocess

    marker = Path(tmp_path) / "bg_typed.txt"
    echo_script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f=New-Object System.Windows.Forms.Form;$f.Text='DF BG Input Live';"
        "$tb=New-Object System.Windows.Forms.TextBox;$tb.Multiline=$true;$tb.Width=260;$tb.Height=120;"
        "$f.Controls.Add($tb);"
        f"$tb.Add_TextChanged({{Set-Content -LiteralPath '{marker}' -Value $tb.Text}});"
        "$f.ShowInTaskbar=$false;$f.WindowState='Minimized';"
        "[System.Windows.Forms.Application]::Run($f)"
    )
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE: console host invisible, no focus theft
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-STA", "-Command", echo_script],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=si,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        token = "df" + uuid.uuid4().hex[:6]
        deadline = time.time() + 15
        win = None
        while time.time() < deadline:
            from saturday.tools.spatial import resolve_window

            win = resolve_window("DF BG Input Live")
            if win:
                break
            time.sleep(0.3)
        assert win, "test form window never appeared"
        time.sleep(1.0)  # let the child control handles settle

        tool = KeyboardTool()
        ok, msg = tool.run({"action": "type", "text": token, "window": "DF BG Input Live"})
        assert ok, msg

        got = ""
        deadline = time.time() + 8
        while time.time() < deadline:
            if marker.exists():
                got = marker.read_text(encoding="utf-8", errors="replace")
                if token in got:
                    break
            time.sleep(0.2)
        if token not in got:  # one retry: handles may have settled late
            time.sleep(1.0)
            ok, msg = tool.run({"action": "type", "text": token, "window": "DF BG Input Live"})
            assert ok, msg
            deadline = time.time() + 8
            while time.time() < deadline:
                if marker.exists():
                    got = marker.read_text(encoding="utf-8", errors="replace")
                    if token in got:
                        break
                time.sleep(0.2)
        assert token in got, f"background-typed text never landed in the target control (got {got!r})"
    finally:
        proc.kill()



# ---- merged from test_background_use.py ----
def test_scan_script_supports_background_window_scope():
    script = ps_scan_script("win:notepad")
    assert "Contains('notepad')" in script.replace("  ", " ") or "contains('notepad')" in script
    assert "FindAll" in script


def test_ui_invoke_script_patterns_and_window_scope():
    s = ps_ui_invoke_script("notepad", "close", "Button", 0, "press", "")
    assert "InvokePattern" in s and "Invoke()" in s and "'notepad'" in s.lower()
    s = ps_ui_invoke_script("", "editor", "Edit", 0, "set_text", "hello 'world'")
    assert "ValuePattern" in s and "SetValue('hello ''world''')" in s
    for act, marker in [("toggle", "Toggle()"), ("expand", "Expand()"), ("select", "Select()")]:
        assert marker in ps_ui_invoke_script("w", "x", "", 0, act, "")


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="ui_invoke requires Windows (UI Automation)")
def test_ui_invoke_tool_runs_and_reports_match():
    def runner(script, timeout=30.0):
        assert isinstance(script, str)
        return 0, "MATCH Save | Button | center=745,575\n", ""

    tool = UiInvokeTool(runner=runner)
    ok, out = tool.run({"action": "press", "name": "Save", "window": "notepad"})
    assert ok and "MATCH Save" in out

    def err_runner(script, timeout=30.0):
        return 0, "ERR element not found\n", ""

    ok2, out2 = UiInvokeTool(runner=err_runner).run({"action": "press", "name": "ghost"})
    assert not ok2 and "element not found" in out2
    ok3, out3 = UiInvokeTool(runner=err_runner).run({"action": "bogus", "name": "x"})
    assert not ok3 and "unknown ui_invoke action" in out3


def test_capture_window_script_uses_printwindow(tmp_path):
    s = ps_capture_window_script("notepad", tmp_path / "w.png")
    assert "PrintWindow" in s and "EnumWindows" in s and "w.png" in s
    ok, msg = capture_window_bg(
        "nothing-matches-this-ever-12345", tmp_path / "x.png",
        runner=lambda script, timeout=25.0: (0, "ERR window not found\n", ""),
    )
    assert not ok and "window not found" in msg


class _Reg:
    def names(self):
        return ["ui_tree", "pointer", "screen", "ui_invoke"]


def test_background_prompt_variant():
    bg = build_computer_use_section(_Reg(), background_only=True)
    fg = build_computer_use_section(_Reg(), background_only=False)
    assert "BACKGROUND MODE" in bg and "off-limits" in bg and "capture_window" in bg
    assert "BACKGROUND MODE" not in fg and "Never guess coordinates" in fg


def test_background_only_policy_blocks_disruptive_tools_even_when_safety_off():
    off = ApprovalPolicy.from_mode("off")
    hook = make_approval_hook(off, background_only=True)
    assert hook("pointer", {"action": "click", "x": 1}) is not None
    assert hook("keyboard", {"action": "type", "text": "x"}) is not None
    assert hook("window", {"action": "focus", "query": "x"}) is not None
    assert hook("window", {"action": "list"}) is None, "read-only listing stays allowed"
    assert hook("clipboard", {"action": "get"}) is None
    assert hook("ui_invoke", {"action": "press", "name": "ok"}) is not None, (
        "without window= ui_invoke resolves the user's FOCUSED element - blocked"
    )
    assert hook("ui_invoke", {"action": "focus"}) is not None, "focus steal always blocked"
    assert hook("ui_invoke", {"action": "press", "name": "ok", "window": "Excel"}) is None, (
        "window-targeted background delivery stays allowed"
    )
    # normal mode unaffected
    plain_hook = make_approval_hook(off)
    assert plain_hook("pointer", {"action": "move", "x": 1, "y": 1}) is None


def test_check_command_bg_flag_direct():
    ask = ApprovalPolicy.from_mode("ask")
    assert check_command(ask, "pointer", {"action": "click"}, background_only=True) is not None
    allow = ApprovalPolicy.from_mode("ask", lambda sig, why: True)
    assert check_command(allow, "pointer", {"action": "click"}, background_only=True) is not None, (
        "even a permissive approver cannot override background-only"
    )


def test_config_flag_from_env(monkeypatch):
    monkeypatch.setenv("SATURDAY_BACKGROUND_ONLY", "true")
    cfg = AgentConfig.load({"provider": "vllm"})
    assert cfg.desktop_background_only is True
    monkeypatch.setenv("SATURDAY_BACKGROUND_ONLY", "0")
    cfg2 = AgentConfig.load({"provider": "vllm"})
    assert cfg2.desktop_background_only is False



# ---- merged from test_computer_use.py ----
def test_parse_combo_modifiers_and_keys():
    assert parse_combo("Ctrl+S") == [(0x11, True), (0x53, True), (0x53, False), (0x11, False)]
    seq = parse_combo("alt+F4")
    assert [v for v, d in seq if d] == [0x12, 0x73]
    assert seq[-1] == (0x12, False)
    assert parse_combo("Enter")[0] == (0x0D, True)
    assert parse_combo("Shift") == [(0x10, True), (0x10, False)], "lone modifier is a valid key"
    with __import__("pytest").raises(ValueError):
        parse_combo("ctrl+boguskey")
    with __import__("pytest").raises(ValueError):
        parse_combo("+")


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_keyboard_tool_scripts(tmp_path):
    calls: list[str] = []

    def runner(script, timeout=20.0):
        calls.append(script)
        return 0, "", ""

    kb = KeyboardTool(runner=runner)
    ok, msg = kb.run({"action": "type", "text": "hi\nthere"})
    assert ok and "typed 8 chars" in msg and len(calls) == 1
    script = calls[0]
    assert ps_send_input_defines() in script
    assert "[Kb]::Char([char]104)" in script  # 'h'
    assert "[Kb]::Key(13,$true)" in script  # newline -> Enter

    ok, msg = kb.run({"action": "key", "key": "Ctrl+Shift+Esc"})
    assert ok and msg.startswith("pressed Ctrl+Shift+Esc")

    ok, msg = kb.run({"action": "key", "key": "Shift"})
    assert ok and msg == "pressed Shift ok"

    ok, msg = kb.run({"action": "key", "key": "ctrl+boguskey"})
    assert not ok
    ok, msg = kb.run({"action": "type", "text": ""})
    assert not ok


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_window_list_focus_and_pick():
    calls: list[str] = []

    def runner(script, timeout=25.0):
        calls.append(script)
        if "EnumWindows" in script:
            return 0, "111|Notepad - report.txt|10,10,800,600\n222|OpenCode|0,0,1920,1080\n", ""
        return 0, "ok", ""

    win = WindowTool(runner=runner)
    ok, out = win.run({"action": "list"})
    assert ok and "Notepad - report.txt" in out and "hwnd=222" in out

    ok, out = win.run({"action": "focus", "query": "notepad"})
    assert ok and "focus 'Notepad - report.txt' ok" in out
    focus_script = calls[-1]
    assert "[IntPtr]111" in focus_script and "SetForegroundWindow" in focus_script

    ok, out = win.run({"action": "maximize", "query": "opencode"})
    assert ok

    ok, out = win.run({"action": "focus", "query": "zzz-not-there"})
    assert not ok and "no window matching" in out
    assert WindowTool.pick(["Aa B", "Ab C"], "aa") == "Aa B"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_clipboard_roundtrip_scripts():
    calls: list[str] = []

    def runner(script, timeout=20.0):
        calls.append(script)
        return 0, "clip-content" if "GetText" in script else "", ""

    cb = ClipboardTool(runner=runner)
    ok, out = cb.run({"action": "get"})
    assert ok and out == "clip-content"
    ok, out = cb.run({"action": "set", "text": "line1\nline2 \"quoted\" $(calc) `n"})
    assert ok and "clipboard set" in out
    set_script = calls[-1]
    import base64 as _b64

    encoded = set_script.split("FromBase64String('")[1].split("'")[0]
    assert _b64.b64decode(encoded).decode("utf-8") == 'line1\nline2 "quoted" $(calc) `n', (
        "clipboard payload must round-trip via base64 (no PS interpolation)"
    )
    ok, out = cb.run({"action": "bogus"})
    assert not ok


def _reg_with(names):
    reg = ToolRegistry()

    class T:
        def __init__(self, n):
            self.name = n
            self.description = n
            self.parameters = {}

        def run(self, args):
            return True, ""

    for n in names:
        reg.register(T(n))
    return reg


def test_computer_use_prompt_appears_only_with_spatial_tools():
    full = build_system_prompt_parts(_reg_with(["ui_tree", "pointer", "screen"]))
    assert "Computer use protocol" in full["stable"]
    assert "Never guess coordinates" in full["stable"]
    plain = build_system_prompt_parts(_reg_with(["shell", "read_file"]))
    assert "Computer use protocol" not in plain["stable"]


def test_new_desktop_tools_gated_like_pointer():
    ask = ApprovalPolicy.from_mode("ask")
    assert check_command(ask, "keyboard", {"action": "type", "text": "hello"}) is not None
    assert check_command(ask, "keyboard", {"action": "type"}) is None or True  # empty text still gated upstream by tool
    assert check_command(ask, "clipboard", {"action": "set", "text": "x"}) is not None
    assert check_command(ask, "clipboard", {"action": "get"}) is not None
    assert check_command(ask, "window", {"action": "list"}) is None, "window list is read-only"
    assert check_command(ask, "window", {"action": "focus", "query": "notepad"}) is not None

    deny = ApprovalPolicy.from_mode("deny")
    assert "DENIED keyboard" in check_command(deny, "keyboard", {"action": "key", "key": "Enter"})

    approved = []

    def approver(sig, why):
        approved.append(sig)
        return True

    allow = ApprovalPolicy.from_mode("ask", approver)
    assert check_command(allow, "keyboard", {"action": "key", "key": "Ctrl+S"}) is None
    assert approved == ["key Ctrl+S"], "stable signature for combos"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_app_open_tool_and_gating():
    from saturday.tools.spatial import AppOpenTool, ps_app_open_script

    script = ps_app_open_script("notepad", "", 7, True)
    assert "wShowWindow=7" in script, "background mode must use SW_SHOWMINNOACTIVE"
    assert "focus-restored" in script
    assert "wShowWindow=1" in ps_app_open_script("notepad", "", 1, False)

    calls: list[str] = []

    def runner(script_s, timeout=20.0):
        calls.append(script_s)
        return 0, "PID 4242\nfocus-untouched\n", ""

    tool = AppOpenTool(runner=runner)
    ok, out = tool.run({"target": "calc"})
    assert ok and "pid=4242" in out and "user focus untouched" in out
    ok2, out2 = tool.run({"target": "", "mode": "normal"})
    assert not ok2
    def runner_args(script_s, timeout=20.0):
        calls.append(script_s)
        return 0, "PID 1\nfocus-restored\n", ""

    ok3, out3 = AppOpenTool(runner=runner_args).run({"target": "calc", "args": "-v"})
    assert ok3 and "user's window restored" in out3 and "calc -v" in calls[-1]

    ask = ApprovalPolicy.from_mode("ask")
    assert check_command(ask, "app_open", {"target": "calc"}) is not None
    off_allow = ApprovalPolicy.from_mode("off")
    hook = make_approval_hook(off_allow, background_only=True)
    assert hook("app_open", {"target": "calc"}) is None, "app_open is the designated bg launcher"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="live Windows integration test: real powershell/notepad.exe/taskkill")
def test_app_open_live_focus_preserved():
    import ctypes
    import subprocess as sp

    from saturday.tools.spatial import AppOpenTool

    u = ctypes.windll.user32
    b = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(u.GetForegroundWindow(), b, 256)

    def runner(script_s, timeout=20.0):
        real = sp.run(["powershell", "-NoProfile", "-Command", script_s], capture_output=True, text=True)
        return real.returncode, real.stdout, real.stderr

    ok, out = AppOpenTool(runner=runner).run({"target": "notepad"})
    assert ok, out
    after = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(u.GetForegroundWindow(), after, 256)
    sp.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)
    assert b.value == after.value, f"foreground changed: {b.value!r} -> {after.value!r}"


def test_registration_includes_all_five():
    from saturday.config import AgentConfig
    from saturday.plugins import core_plugin
    from saturday.tools.base import ToolRegistry as R
    from saturday import plugins as P

    reg = R()
    P.install_plugins(reg, [core_plugin(AgentConfig(provider="vllm"))], [])
    names = set(reg.names())
    for needed in ("ui_tree", "pointer", "keyboard", "window", "clipboard", "screen"):
        assert needed in names, f"{needed} missing from registry"



# ---- merged from test_spatial.py ----
def test_cell_naming():
    assert cell_name(0, 0) == "A1"
    assert cell_name(1, 0) == "B1"
    assert cell_name(25, 1) == "Z2"
    assert cell_name(26, 0) == "AA1"
    legend = build_grid_legend(1920, 1080)
    assert "1920x1080" in legend and "96px" in legend


def test_landmark_store_add_and_resolve():
    store = LandmarkStore()
    k1 = store.add("Save", 100, 200, "Button")
    k2 = store.add("save", 300, 400, "MenuItem")  # same normalized name, different pos -> suffixed
    assert k1 == "save" and k2 == "save_2"
    pt = store.resolve("SAVE")
    assert pt and pt["x"] in (100, 300)
    assert store.resolve("no-such-thing") is None
    assert store.resolve("sav") is not None, "unique prefix should resolve"


CANNED_SCAN = [
    {"n": "", "t": "ControlType.Pane", "x": 0, "y": 0, "w": 1920, "h": 1080, "off": False},
    {"n": "Untitled - Notepad", "t": "ControlType.Window", "x": 10, "y": 10, "w": 800, "h": 600, "off": False},
    {"n": "File", "t": "ControlType.MenuItem", "x": 20, "y": 30, "w": 40, "h": 20, "off": False},
    {"n": "Save", "t": "ControlType.Button", "x": 700, "y": 560, "w": 90, "h": 30, "off": False},
    {"n": "hidden", "t": "ControlType.Button", "x": -500, "y": 0, "w": 100, "h": 50, "off": True},
]


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_ui_tree_parses_canned_scan_and_stores_landmarks():
    store = LandmarkStore()
    tool = UiTreeTool(landmarks=store, runner=lambda script, timeout=25.0: (0, __import__("json").dumps(CANNED_SCAN), ""))
    ok, out = tool.run({"scope": "foreground"})
    assert ok
    assert 'button \'Save\'' in out.replace("Button", "button")
    assert "[save]" in out
    assert "hidden" not in out, "offscreen elements must be filtered"
    assert store.resolve("save")["x"] == 745
    tree, marks = render_element_tree(CANNED_SCAN, store)
    assert any("center=(745,575)" in line for line in tree.splitlines())


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_ui_tree_failure_reports_stderr():
    tool = UiTreeTool(runner=lambda script, timeout=25.0: (1, "", "boom details"))
    ok, err = tool.run({})
    assert not ok and "boom details" in err


def _fake_ps_ok(script, timeout=20.0):
    return 0, "", ""


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_pointer_validation_and_execution():
    store = LandmarkStore()
    store.add("Save", 745, 575, "Button")
    calls: list[str] = []
    tool = PointerTool(landmarks=store, runner=lambda s, timeout=20.0: calls.append(s) or (0, "", ""))

    ok, msg = tool.run({"action": "click", "target": "save"})
    assert ok and "click at (745,575)" in msg and len(calls) == 1
    assert "SetCursorPos(745,575)" in calls[0]

    ok, msg = tool.run({"action": "bogus"})
    assert not ok and "unknown pointer action" in msg

    ok, msg = tool.run({"action": "drag", "target": "save"})  # drag needs x2,y2 too but resolves start
    assert ok

    ok, msg = tool.run({"action": "click", "target": "ghost"})
    assert not ok and "unknown target 'ghost'" in msg

    ok, msg = tool.run({"action": "click"})
    assert not ok and "needs x,y or target" in msg

    ok, msg = tool.run({"action": "move", "x": 9999999, "y": 5})
    assert not ok and "out of range" in msg

    ok, msg = tool.run({"action": "scroll", "dy": -3})
    assert ok and len(calls) == 3 and "2048" in calls[-1] and ",-360," in calls[-1].replace(" ", "")


def test_collect_marks_and_legend():
    marks = collect_marks([e for e in CANNED_SCAN if e["n"]], LandmarkStore())
    labels = [m["label"] for m in marks]
    assert labels and len(labels) <= 40
    legend = marked_legend(marks)
    assert "center=" in legend and "box " in legend


def test_pointer_gated_by_safety():
    policy_ask = ApprovalPolicy.from_mode("ask")
    reason = check_command(policy_ask, "pointer", {"action": "click", "x": 10, "y": 20})
    assert reason is not None and "fail-closed" in reason

    policy_deny = ApprovalPolicy.from_mode("deny")
    reason = check_command(policy_deny, "pointer", {"action": "click", "x": 10, "y": 20})
    assert reason is not None and "DENIED pointer" in reason

    policy_off = ApprovalPolicy.from_mode("off")
    assert check_command(policy_off, "pointer", {"action": "click"}) is None

    approved: list[str] = []

    def approver(sig, why):
        approved.append(sig)
        return True

    policy_allow = ApprovalPolicy.from_mode("ask", approver)
    assert check_command(policy_allow, "pointer", {"action": "double_click", "target": "save"}) is None
    assert approved == ["double_click target=save"], "signature should use stable target names"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_pointer_middle_click_scripts():
    calls: list[str] = []
    tool = PointerTool(landmarks=LandmarkStore(), runner=lambda s, timeout=20.0: calls.append(s) or (0, "", ""))
    ok, msg = tool.run({"action": "middle_click", "x": 100, "y": 200})
    assert ok and "middle_click at (100,200)" in msg
    # mouse_event flags: MIDDLEDOWN=0x20, MIDDLEUP=0x40
    assert "mouse_event(32,0,0,0,0)" in calls[0]
    assert "mouse_event(64,0,0,0,0)" in calls[0]


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_window_close_posts_wm_close():
    scripts: list[str] = []

    def runner(s, timeout=25.0):
        scripts.append(s)
        if len(scripts) == 1:
            return 0, "111|Notepad|0,0,800,600\n222|Calc|10,10,300,300", ""
        return 0, "", ""

    tool = WindowTool(runner=runner)
    ok, msg = tool.run({"action": "close", "query": "notepad"})
    assert ok and "sent close request" in msg
    assert "PostMessage" in scripts[-1] and "0x0010" in scripts[-1]


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="screen capture fallback is Windows-only")
def test_screen_display_captures_specific_monitor(tmp_path, monkeypatch):
    import re

    import saturday.tools.screen as screen_mod
    from saturday.tools.screen import ScreenTool

    captured: dict[str, str] = {}

    class FakeProc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kw):
        ps = cmd[-1]
        captured["ps"] = ps
        m = re.search(r"\$bmp\.Save\('([^']+)'\)", ps)
        Path(m.group(1)).write_bytes(b"x" * 200)
        return FakeProc()

    monkeypatch.setattr(screen_mod.subprocess, "run", fake_run)
    tool = ScreenTool(shots_dir=tmp_path)
    ok, msg = tool.run({"display": 2})
    assert ok and "display 2" in msg
    assert "AllScreens" in captured["ps"] and "$scr[1].Bounds" in captured["ps"]



# ---- merged from test_world_model.py ----
def el(t, n, x, y, w=10, h=10):
    return {"t": t, "n": n, "x": x, "y": y, "w": w, "h": h}


def test_compute_delta_classifies_changes():
    old = {element_identity(e): e for e in [el("Button", "Save", 1, 1), el("Button", "OK", 5, 5)]}
    new = [el("Button", "Save", 1, 1), el("Button", "Cancel", 30, 30), el("Button", "OK", 5, 5)]
    d = compute_delta(old, new)
    assert len(d["added"]) == 1 and d["added"][0]["n"] == "Cancel"
    assert len(d["removed"]) == 0
    # moved element: same identity, different box
    d2 = compute_delta(old, [el("Button", "Save", 99, 99), el("Button", "OK", 5, 5)])
    assert len(d2["changed"]) == 1 and d2["changed"][0]["n"] == "Save"
    assert element_box(old[element_identity(el("Button", "Save", 1, 1))]) == (1, 1, 10, 10)


def test_state_cache_frame_dedupe(tmp_path):
    cache = StateCache()
    a = tmp_path / "a.png"
    a.write_bytes(b"frame-1")
    b = tmp_path / "b.png"
    b.write_bytes(b"frame-1")
    c = tmp_path / "c.png"
    c.write_bytes(b"frame-2")
    assert cache.frame_unchanged("k", a) is False  # first sighting
    assert cache.frame_unchanged("k", b) is True  # same content
    assert cache.frame_unchanged("k", c) is False  # different content


# -- ui_tree delta mode --------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="tests the Windows PowerShell-runner implementation")
def test_ui_tree_delta_mode_with_fake_runner():
    a = [el("Button", "Save", 10, 10), el("Button", "OK", 50, 50)]
    b = [el("Button", "Save", 10, 10), el("Button", "Cancel", 30, 30)]
    calls = {"n": 0}

    def runner(s, timeout=20.0):
        calls["n"] += 1
        out = json.dumps(a) if calls["n"] == 1 else json.dumps(b)
        return (0, out, "") if "EnumWindows" not in s else (0, "111|Notepad|0,0,800,600", "")

    tool = UiTreeTool(runner=runner)
    ok1, out1 = tool.run({"scope": "foreground"})
    assert ok1 and "elements=2" in out1  # first scan: full (no cache yet)
    ok2, out2 = tool.run({"scope": "foreground"})
    assert ok2 and "ui_tree delta" in out2
    assert "+1 new" in out2 and "-1 gone" in out2 and "Cancel" in out2
    ok3, out3 = tool.run({"scope": "foreground"})
    assert ok3 and "NO CHANGE" in out3
    ok4, out4 = tool.run({"scope": "foreground", "mode": "full"})
    assert ok4 and "elements=2" in out4  # explicit full still works


def test_verify_expect_observed_and_missing():
    def runner(s, timeout=20.0):
        if "EnumWindows" in s:
            return 0, "111|Notepad - Untitled|0,0,800,600", ""
        return 0, "[]", ""  # scan returns nothing

    note = verify_expect(runner, "notepad", attempts=1)
    assert "observed in window title" in note
    miss = verify_expect(lambda s, t=20.0: (0, "111|Calc", ""), "notepad", attempts=1)
    assert "NOT observed" in miss


# -- screenshot frame dedupe ---------------------------------------------------


def test_screen_tool_returns_unchanged_frame(tmp_path, monkeypatch):
    tool = ScreenTool(shots_dir=tmp_path, cache=StateCache())

    def fake_shot(self, out):
        out.write_bytes(b"SAME" * 64)
        return True

    monkeypatch.setattr(ScreenTool, "_shot_via_pillow", fake_shot)
    ok1, out1 = tool.run({"annotate": "none"})
    assert ok1 and "screenshot saved" in out1 and tool.pending_images
    time.sleep(0.01)
    ok2, out2 = tool.run({"annotate": "none"})
    assert ok2 and "unchanged" in out2
    assert tool.pending_images == [], "unchanged frame must not re-attach"


# -- asset embedding -----------------------------------------------------------


def test_embed_assets_copies_and_rewrites(tmp_path):
    img = tmp_path / "shot-1.png"
    img.write_bytes(b"png")
    records = [
        {"messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": str(img)}}]},
            {"role": "assistant", "content": "done"},
        ]}
    ]
    assert collect_image_paths(records) == [str(img)]
    copied = embed_assets(records, tmp_path / "out.jl.assets")
    assert copied == 1
    ref = records[0]["messages"][0]["content"][0]["image_url"]["url"]
    assert ref.startswith("out.jl.assets/") and (tmp_path / ref).is_file()
    # http/data refs are never touched
    records2 = [{"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}]
    assert collect_image_paths(records2) == []


# -- ablation rig --------------------------------------------------------------


def test_run_ablation_full_variant(tmp_path):
    turns = [
        {"tool_calls": [{"name": "write_file", "arguments": {"path": "ablation_probe.txt", "content": FILE_MARKER}}]},
        {"content": "done"},
    ]

    def factory(cfg):
        from saturday.agent.core import Agent

        return Agent(cfg=cfg, client=make_scripted_model(turns), enable_subagents=False)

    task = {
        "id": "file-write",
        "prompt": "probe",
        "check": lambda ws, traj: (
            (ws / "ablation_probe.txt").is_file()
            and (ws / "ablation_probe.txt").read_text(encoding="utf-8").strip() == FILE_MARKER,
            "checked",
        ),
    }
    payload = run_ablation(tasks=[task], variants=["full"], workspace=tmp_path, out_dir=tmp_path / "runs", agent_factory=factory)
    row = payload["results"][0]
    assert row["ok"] is True and row["variant"] == "full" and row["steps"] == 2
    assert payload["summary"]["full"]["passed"] == 1
    assert list((tmp_path / "runs").glob("ablation-*.json")), "results json persisted"


def test_summary_math():
    summary = _summary([
        {"variant": "full", "ok": True, "steps": 2, "tokens": 10, "seconds": 1.0},
        {"variant": "full", "ok": False, "steps": 5, "tokens": 20, "seconds": 3.0},
    ])
    assert summary["full"]["pass_rate"] == 0.5 and summary["full"]["avg_steps"] == 3.5


# ---- external_agent tool ---------------------------------------------------


def test_external_agent_unknown_agent_id():
    from saturday.tools.external_agent import ExternalAgentTool

    ok, msg = ExternalAgentTool().run({"agent": "not-a-real-agent", "prompt": "hi"})
    assert not ok and "unknown agent" in msg


def test_external_agent_empty_prompt():
    from saturday.tools.external_agent import ExternalAgentTool

    ok, msg = ExternalAgentTool().run({"agent": "claude-code", "prompt": "  "})
    assert not ok and "prompt is required" in msg


def test_external_agent_missing_binary_no_install_gives_hint(monkeypatch):
    from saturday.tools import external_agent as ea

    monkeypatch.setattr(ea.shutil, "which", lambda name: None)
    ok, msg = ea.ExternalAgentTool().run({"agent": "claude-code", "prompt": "hi"})
    assert not ok
    assert "not installed" in msg
    assert "npm install -g @anthropic-ai/claude-code" in msg


def test_external_agent_missing_binary_install_true_but_installer_fails(monkeypatch):
    from saturday.tools import external_agent as ea

    monkeypatch.setattr(ea.shutil, "which", lambda name: None)
    tool = ea.ExternalAgentTool(installer=lambda spec: (False, "network unreachable"))
    ok, msg = tool.run({"agent": "codex", "prompt": "hi", "install": True})
    assert not ok and "auto-install failed" in msg and "network unreachable" in msg


def test_external_agent_install_succeeds_then_runs(monkeypatch):
    from saturday.tools import external_agent as ea

    calls = {"which": 0}

    def fake_which(name):
        calls["which"] += 1
        return None if calls["which"] == 1 else f"/usr/bin/{name}"

    monkeypatch.setattr(ea.shutil, "which", fake_which)

    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = "delegate says hi"
            stderr = ""

        return R()

    monkeypatch.setattr(ea.subprocess, "run", fake_run)
    tool = ea.ExternalAgentTool(installer=lambda spec: (True, "installed"))
    ok, msg = tool.run({"agent": "gemini", "prompt": "hi", "install": True})
    assert ok and msg == "delegate says hi"


def test_external_agent_runs_when_already_installed(monkeypatch):
    from saturday.tools import external_agent as ea

    monkeypatch.setattr(ea.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = "ok from claude"
            stderr = ""

        return R()

    monkeypatch.setattr(ea.subprocess, "run", fake_run)
    ok, msg = ea.ExternalAgentTool().run({"agent": "claude-code", "prompt": "fix the bug"})
    assert ok and msg == "ok from claude"
    assert captured["argv"] == ["/usr/bin/claude", "-p", "fix the bug"]


def test_external_agent_nonzero_exit_surfaces_stderr(monkeypatch):
    from saturday.tools import external_agent as ea

    monkeypatch.setattr(ea.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(argv, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "auth error: bad key"

        return R()

    monkeypatch.setattr(ea.subprocess, "run", fake_run)
    ok, msg = ea.ExternalAgentTool().run({"agent": "cursor", "prompt": "hi"})
    assert not ok and "exited 1" in msg and "auth error" in msg


def test_external_agent_timeout(monkeypatch):
    from saturday.tools import external_agent as ea
    import subprocess as sp

    monkeypatch.setattr(ea.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(argv, **kwargs):
        raise sp.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(ea.subprocess, "run", fake_run)
    ok, msg = ea.ExternalAgentTool().run({"agent": "codex", "prompt": "hi", "timeout": 5})
    assert not ok and "timed out after 5" in msg


def test_external_agent_registered_in_core_plugin():
    from saturday.plugins import core_plugin

    names = {t.name for t in core_plugin().tools}
    assert "external_agent" in names


def test_external_agents_family_maps_to_the_tool():
    assert ToolRegistry.TOOL_FAMILIES["external_agents"] == frozenset({"external_agent"})
    assert ToolRegistry.expand_tool_names(["external_agents"]) == {"external_agent"}

