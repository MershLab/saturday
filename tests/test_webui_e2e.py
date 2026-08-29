"""Real-browser e2e for the desktop web app (webui): Chromium drives the actual UI."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.webui import AppState, AppServer  # noqa: E402

TOKEN = "e2e-tok"

try:
    from playwright.sync_api import sync_playwright

    HAS_PW = True
except Exception:
    HAS_PW = False


@pytest.fixture(scope="module")
def ui_server():
    from pytest import MonkeyPatch

    import tempfile

    import shutil

    with MonkeyPatch().context() as mp:
        import saturday.mcp_plugin as mcpmod
        from saturday import config as cfgmod

        mp.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
        # hermetic: isolate from the user's real ~/.saturday/config.json AND
        # arm a detected key so the onboarding wizard never interferes with
        # these pre-wizard flows
        mp.setattr(cfgmod, "CONFIG_FILE", Path(scratch_holder := tempfile.mkdtemp(prefix="df-e2e-cfg-")) / "config.json")
        mp.setenv("DEEPSEEK_API_KEY", "sk-e2e-hermetic")
        from saturday.projects import ProjectStore

        scratch = tempfile.mkdtemp(prefix="df-e2e-")
        app = AppState(
            cfg_overrides={"safety_mode": "off", "workspace_root": str(Path.cwd())},
            store_root=Path(scratch) / "sessions",
            projects_store=ProjectStore(Path(scratch) / "projects.json"),
        )
        try:
            yield from _make_server(app)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


def _make_server(app):
    turns = [
        {"content": "**Forged** reply with `code` and:\n\n```python\nprint('hi')\n```"},
        {"content": "**Forged** reply with `code` and:\n\n```python\nprint('hi')\n```"},
        {"content": "**Forged** reply with `code` and:\n\n```python\nprint('hi')\n```"},
    ]
    fake = make_scripted_model(turns)
    orig = app._new_agent
    app._new_agent = lambda cfg: _with_fake(orig(cfg), fake)
    srv = AppServer(("127.0.0.1", 0), app, token=TOKEN)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield base
    srv.shutdown()
    srv.server_close()


def _with_fake(agent, fake):
    agent._ensure_client = lambda: fake
    return agent


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_send_and_streamed_reply_renders(ui_server):
    last_err = None
    for attempt in range(2):
        logs: list[str] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 860})
                page.on("console", lambda m, logs=logs: logs.append(f"console.{m.type}: {m.text}"))
                page.on("pageerror", lambda e, logs=logs: logs.append(f"pageerror: {e}"))
                page.on("requestfailed", lambda r, logs=logs: logs.append(f"reqfail: {r.url} {r.failure}"))
                page.goto(f"{ui_server}/?k={TOKEN}")
                page.wait_for_selector("#input", state="visible", timeout=20000)
                page.fill("#input", "hello forge")
                page.keyboard.press("Enter")
                page.locator(".turn-stats").first.wait_for(timeout=25000)
                html = page.locator(".msg-assistant .md").first.inner_html()
                if "<strong" not in html:
                    err_line = page.evaluate("() => { const e = document.querySelector('.sysline.error'); return e ? e.textContent : null; }")
                    raise AssertionError(f"markdown empty; sysline={err_line!r}; html={html[:120]!r}")
                assert 'class="codewrap"' in html, f"fenced code block missing: {html[:300]}"
                assert 'class="inline"' in html, f"inline code missing: {html[:300]}"
                stats = page.locator(".turn-stats").first.inner_text()
                assert "step" in stats and "tokens" in stats
                browser.close()
                return
        except Exception as exc:
            try:
                diag = page.evaluate("() => ({err: document.querySelector('.sysline.error')?.textContent || null, stats: document.querySelector('.turn-stats')?.textContent || null, thread: (document.querySelector('#thread')?.innerHTML || '').slice(0, 400)})")
                last_err = AssertionError(f"{exc} | dom={diag}")
            except Exception:
                last_err = exc
        if attempt == 0:
            import time

            time.sleep(1.0)
    raise AssertionError(f"e2e failed after retry: {last_err}\nbrowser log:\n" + "\n".join(logs[-30:]))


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_slash_popup_and_settings_modal(ui_server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.goto(f"{ui_server}/?k={TOKEN}")
        page.wait_for_selector("#input", state="visible", timeout=15000)
        page.fill("#input", "/to")
        page.wait_for_selector(".slash-item", timeout=5000)
        first = page.locator(".slash-item").first.inner_text()
        assert "/todo" in first or "/tools" in first
        page.keyboard.press("Escape")

        page.click("#modelPill")
        page.wait_for_selector("#modelMenu:not(.hidden)", timeout=5000)
        page.locator("#modelMenu button", has_text="All settings").click()
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        assert page.locator("#cfgProvider option").count() >= 10

        # proper settings panel: section nav switches panes
        assert page.locator("#setNav button").count() >= 7
        page.click('#setNav button[data-sec="safety"]')
        page.wait_for_selector('.set-pane[data-sec="safety"].on', timeout=5000)
        assert page.locator("#cfgBgOnly").count() == 1
        page.click('#setNav button[data-sec="data"]')
        page.wait_for_selector('#btnClearSessions:not(.hidden)', timeout=5000)
        page.click('#setNav button[data-sec="model"]')
        page.wait_for_selector('.set-pane[data-sec="model"].on', timeout=5000)

        page.click("#settingsClose")
        time.sleep(0.1)
        assert page.locator("#settingsModal.hidden").count() == 1
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_projects_flow(ui_server):
    import tempfile

    kb = tempfile.NamedTemporaryFile("w", suffix="-kb.txt", delete=False, encoding="utf-8")
    kb.write("E2E-KNOWLEDGE-MARKER style rules")
    kb.close()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.goto(f"{ui_server}/?k={TOKEN}")
        page.wait_for_selector("#input", state="visible", timeout=20000)

        # create a project through the modal: color + knowledge file included
        page.click("#newProjBtn")
        page.wait_for_selector("#projModal:not(.hidden)", timeout=5000)
        page.fill("#projName", "E2E Repo")
        page.click('#projColors .swatch[data-c="green"]')
        page.fill("#projFileInput", kb.name)
        page.click("#projFileAdd")
        page.wait_for_selector(".kfile-chip", timeout=5000)
        page.click("#projSave")
        page.wait_for_selector(".proj-item", timeout=5000)
        assert "E2E Repo" in page.locator(".proj-item .proj-name").first.inner_text()
        assert page.locator(".proj-item.pc-green").count() == 1, "color accent class must render"

        # creating selects it: chip + scoped view + project head row
        page.wait_for_selector("#projChip:not(.hidden)", timeout=5000)
        assert "E2E Repo" in page.locator("#projChipName").inner_text()
        page.wait_for_selector(".proj-open-head", timeout=5000)

        # a chat sent now lands inside the project
        page.fill("#input", "project hello")
        page.keyboard.press("Enter")
        page.locator(".turn-stats").first.wait_for(timeout=25000)
        sid = page.evaluate("() => window.df.state.sid")
        assert sid, "session must be adopted"
        proj_id = page.evaluate("() => window.df.state.proj")
        assert proj_id, "adopted session must carry the active project"

        # server-side truth: session tagged; color + knowledge persisted (cookie carries auth)
        data = page.evaluate("async () => { const s = await fetch('/api/sessions'); const p = await fetch('/api/projects'); return { sessions: await s.json(), projects: await p.json() }; }")
        rows = {r["id"]: r for r in data["sessions"]["sessions"]}
        assert rows[sid]["project"] == proj_id
        proj = next(p for p in data["projects"]["projects"] if p["id"] == proj_id)
        assert proj["color"] == "green"
        assert len(proj["files"]) == 1 and proj["files"][0].endswith("-kb.txt")

        # back to all chats: unprojected view only
        page.click(".all-chats")
        page.wait_for_selector("#projChip.hidden", state="attached", timeout=5000)

        # move-to-project menu lists the project
        page.evaluate(f"() => window.df.openProjPick({sid!r})")
        page.wait_for_selector("#projPickMenu:not(.hidden)", timeout=5000)
        assert page.locator("#projPickMenu button", has_text="E2E Repo").count() == 1
        page.keyboard.press("Escape")

        # star the project: pinned marker renders in sidebar
        page.hover(".proj-item")
        page.locator('.proj-item .proj-acts button[title="Star project"]').click()
        page.wait_for_selector(".proj-item.pinned", timeout=5000)
        browser.close()
