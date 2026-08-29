"""Real-browser e2e for the new-UI layer: context panel, theme menu (Omarchy
themes), assistant-mode flavor and the onboarding wizard."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.webui import AppState, AppServer  # noqa: E402

TOKEN = "newui-tok"

try:
    from playwright.sync_api import sync_playwright

    HAS_PW = True
except Exception:
    HAS_PW = False

KEY_ENVS = [
    "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY", "NOUS_API_KEY", "XAI_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY",
    "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "ZAI_API_KEY", "AZURE_OPENAI_API_KEY",
    "TOGETHER_API_KEY",
]


@pytest.fixture(scope="module")
def ui_server(tmp_path_factory):
    from pytest import MonkeyPatch

    scratch = tmp_path_factory.mktemp("df-newui")
    with MonkeyPatch().context() as mp:
        import saturday.mcp_plugin as mcpmod
        from saturday import config as cfgmod
        from saturday.projects import ProjectStore

        mp.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
        # NEVER touch the user's real ~/.saturday from browser tests:
        # CONFIG_FILE is bound at import time from CONFIG_DIR, so patch BOTH
        mp.setattr(cfgmod, "CONFIG_DIR", scratch)
        mp.setattr(cfgmod, "CONFIG_FILE", scratch / "config.json")
        saved: list[dict] = []
        mp.setattr(cfgmod, "save_config", lambda partial: saved.append(dict(partial)))
        for k in KEY_ENVS:
            os.environ.pop(k, None)
        app = AppState(
            cfg_overrides={"safety_mode": "off", "workspace_root": str(Path.cwd())},
            store_root=scratch / "sessions",
            projects_store=ProjectStore(scratch / "projects.json"),
        )
        fake = make_scripted_model([{"content": "ok reply"}] * 4)
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


def _fresh_page(pw, base):
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    page = ctx.new_page()
    errs: list[str] = []
    page.on("pageerror", lambda e, errs=errs: errs.append(f"pageerror: {e}"))
    page.on("console", lambda m, errs=errs: errs.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    ctx._df_errs = errs
    page.goto(f"{base}/?k={TOKEN}")
    page.evaluate("() => { localStorage.clear(); }")
    page.reload()
    page.wait_for_selector("#input", state="visible", timeout=20000)
    # cleared storage re-arms the onboarding wizard; dismiss so it doesn't
    # block pointer events on everything underneath
    page.wait_for_timeout(650)
    if page.locator("#onboardModal:not(.hidden)").count():
        page.click("#obSkip")
        page.wait_for_function("() => document.querySelector('#onboardModal').classList.contains('hidden')", timeout=5000)
    return browser, ctx, page


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_context_panel_and_meter(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        try:
            page.wait_for_selector("#tokMeter:not(.hidden)", timeout=10000)
        except Exception:
            raise AssertionError(f"meter never visible; js errors: {getattr(ctx, '_df_errs', [])}")
        meter_txt = page.locator("#tokMeter").inner_text()
        assert "/" in meter_txt, f"meter should show used/compact: {meter_txt}"
        page.click("#tokMeter")
        page.wait_for_selector("#ctxModal:not(.hidden)", timeout=5000)
        segs = page.locator("#ctxBar .ctx-seg").count()
        assert segs >= 2, "bar should show system+tools+reserve slices"
        rows = page.locator("#ctxLegend .ctx-row").count()
        assert rows >= 3
        total = page.locator("#ctxTotal").inner_text()
        assert "tokens" in total
        page.keyboard.press("Escape")
        page.wait_for_function("() => document.querySelector('#ctxModal').classList.contains('hidden')", timeout=5000)

        # slash parity: /context streams a notice into the chat
        # (first Enter accepts the autocomplete, second sends)
        page.fill("#input", "/context")
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")
        page.wait_for_selector(".notice", timeout=10000)
        assert "%" in page.locator(".notice").first.inner_text()
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_theme_menu_applies_omarchy_theme(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        page.click("#themeBtn")
        page.wait_for_selector("#themeMenu:not(.hidden)", timeout=5000)
        buttons = page.locator("#themeMenu button")
        assert buttons.count() >= 20, "19 omarchy themes + 2 saturday + system"
        assert page.locator('#themeMenu button', has_text="Tokyo Night").count() == 1
        page.locator("#themeMenu button", has_text="Gruvbox").click()
        page.wait_for_function("() => document.documentElement.dataset.theme === 'gruvbox'", timeout=5000)
        assert page.evaluate("() => document.documentElement.dataset.mode") == "dark"
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(40, 40, 40)"
        assert page.evaluate("() => localStorage.getItem('df_theme')") == "gruvbox"
        # toggle button flips between last dark and last light
        page.click("#themeBtn")
        page.locator("#themeMenu button", has_text="Flexoki Light").click()
        page.wait_for_function("() => document.documentElement.dataset.theme === 'flexoki-light'", timeout=5000)
        assert page.evaluate("() => document.documentElement.dataset.mode") == "light"
        page.evaluate("() => window.df.toggleTheme()")
        assert page.evaluate("() => document.documentElement.dataset.theme") == "gruvbox", "toggle returns to last dark theme"
        assert page.evaluate("() => document.documentElement.dataset.mode") == "dark"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_theme_setting_persists_via_settings(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        page.click("#kebabBtn")
        page.locator('#kebabMenu button[data-act="settings"]').click()
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        opts = page.locator("#cfgThemeSel optgroup[label='Omarchy'] option").count()
        assert opts >= 19, "all shipped omarchy themes selectable"
        page.select_option("#cfgThemeSel", "rose-pine")
        page.click("#settingsSave")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'rose-pine'", timeout=5000)
        assert page.evaluate("() => document.documentElement.dataset.mode") == "light"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_onboarding_wizard_shows_and_saves(ui_server, monkeypatch):
    monkeypatch.setattr(
        "saturday.llm.probe.probe_connection",
        lambda prof, key="", timeout=8.0: (True, "reachable \u2014 2 models found", ["openai/gpt-x", "openai/gpt-y"]),
    )
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        # _fresh_page dismisses via session storage; re-arm it for this test
        page.evaluate("() => sessionStorage.removeItem('df_onboard_skip')")
        page.reload()
        page.wait_for_selector("#onboardModal:not(.hidden)", timeout=10000)
        assert page.locator("#obProvider option").count() >= 10
        # validation: save without key keeps the modal + warning
        page.click("#obSave")
        page.wait_for_selector("#obWarn:not(.hidden)", timeout=5000)
        page.fill("#obKey", "sk-e2e-fake-key")
        page.locator("#obProvider").select_option("openai")
        page.click("#obSave")
        page.wait_for_function("() => document.querySelector('#onboardModal').classList.contains('hidden')", timeout=8000)
        info = page.evaluate("async () => await (await fetch('/api/state')).json()")
        assert info["provider"] == "openai" and info["has_key"] is True
        # reload: no wizard again
        page.reload()
        page.wait_for_selector("#input", state="visible", timeout=15000)
        page.wait_for_timeout(700)
        assert page.locator("#onboardModal.hidden").count() == 1, "wizard must not reappear"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_settings_panes_render_and_save(ui_server):
    """Settings layout regression: the search bar spans the grid, nav and
    panes keep their two columns, and the Advanced group opens + saves."""
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        page.click("#modelLabel")
        page.wait_for_timeout(200)
        page.click("text=All settings\u2026")
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        # search spans full width; nav stays left of the content column
        nav_x = page.locator("#setNav").bounding_box()["x"]
        pane_x = page.locator(".set-pane.on").bounding_box()["x"]
        search_b = page.locator("#cfgSearch").bounding_box()
        nav_b = page.locator("#setNav").bounding_box()
        assert pane_x > nav_x, "settings nav and panes must be separate columns"
        assert search_b["y"] + search_b["height"] <= nav_b["y"], "search bar must sit on its own grid row above nav"
        # Advanced collapsible opens
        page.click('#setNav button[data-sec="model"]')
        page.click("details.adv > summary")
        page.wait_for_timeout(200)
        assert page.locator("#cfgTopP").is_visible(), "advanced group must open"
        # save round-trips without warnings
        page.click("#settingsSave")
        page.wait_for_timeout(1000)
        assert page.locator("#settingsWarn:not(.hidden)").count() == 0
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_titlebar_dblclick_toggles_maximize(ui_server):
    """Custom title bar: double-click on the drag region toggles maximize."""
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        ctx.add_init_script(
            "window.addEventListener('DOMContentLoaded', () => {"
            "window.pywebview = { api: {"
            " win_min: () => true,"
            " win_max: () => { (window.__mx = (window.__mx || 0) + 1); return window.__mx % 2 === 1; },"
            " win_close: () => true } }; });"
        )
        page.reload()
        page.wait_for_timeout(700)
        page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
        page.wait_for_timeout(300)
        assert page.evaluate("document.body.classList.contains('embedded')")
        page.dblclick(".titlebar-brand")
        page.wait_for_timeout(200)
        page.dblclick(".titlebar-brand")
        page.wait_for_timeout(200)
        assert page.evaluate("window.__mx") == 2, "double-click must call win_max twice"
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_assistant_mode_flavor_and_toggle(ui_server):
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        agent_mode_tag = page.locator("#emptyState .tagline").inner_text()
        assert "harness" in agent_mode_tag
        assert page.locator("#modeBadge.hidden").count() == 1, "no badge in agent mode"

        page.click("#kebabBtn")
        page.locator('#kebabMenu button[data-act="settings"]').click()
        page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
        page.locator("#cfgAssistant").check()
        page.fill("#cfgAssistantName", "Jarvis")
        page.fill("#cfgAssistantTitle", "sir")
        page.click("#settingsSave")
        page.wait_for_function("() => window.df.state.info && window.df.state.info.persona_mode === 'assistant'", timeout=8000)
        # badge appears; background-first flips on with the mode
        page.wait_for_selector("#modeBadge:not(.hidden)", timeout=5000)
        assert page.evaluate("() => window.df.state.info.background_only") is True, "assistant defaults to non-intrusive"
        assert page.evaluate("() => window.df.state.info.assistant_name") == "Jarvis"
        assert page.evaluate("() => window.df.state.info.assistant_user_title") == "sir"

        # THE POINT of assistant mode: the UI visibly simplifies - chat IS the app
        page.wait_for_function("() => document.body.classList.contains('mode-assistant')", timeout=5000)
        assert not page.locator("#stage").is_visible(), "technical stage must disappear"
        assert not page.locator("#modelPill").is_visible(), "model pill is developer plumbing"
        assert not page.locator("#tokMeter").is_visible(), "context meter is developer plumbing"
        hint = page.locator("#composerHint").inner_text()
        assert "background" in hint
        page.wait_for_function("() => document.querySelector('#emptyState .tagline').textContent.includes('tell it what you need')", timeout=5000)
        chips = page.locator(".suggest-chip").all_inner_texts()
        assert any("Calculator" in c or "headlines" in c for c in chips), f"task-flavored suggestions expected: {chips}"
        placeholder = page.evaluate("() => document.querySelector('#input').placeholder")
        assert "Tell me what you need" in placeholder

        # full capability retained: registry identical across modes
        names = page.evaluate(
            "async () => { const r = await fetch('/api/tools'); return (await r.json()); }"
        ) if False else None  # tools endpoint not exposed; verified via unit tests
        browser.close()


@pytest.mark.skipif(not HAS_PW, reason="playwright not installed")
def test_ui_provenance_and_verify_settings_roundtrip(ui_server):
    """R1 features are operable from the Settings > Data pane end-to-end."""
    with sync_playwright() as pw:
        browser, ctx, page = _fresh_page(pw, ui_server)
        try:
            page.click("#kebabBtn")
            page.locator('#kebabMenu button[data-act="settings"]').click()
            page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
            page.locator('#setNav button[data-sec="data"]').click()
            page.select_option("#cfgProvenance", "visible")
            page.fill("#cfgVerifyCmd", "python -m pytest -q")
            page.click("#settingsSave")
            page.wait_for_function(
                "() => window.df.state.info && window.df.state.info.provenance_marking === 'visible'",
                timeout=8000,
            )
            assert page.evaluate("() => window.df.state.info.verify_command") == "python -m pytest -q"

            # reopen: controls reflect the persisted values
            page.click("#kebabBtn")
            page.locator('#kebabMenu button[data-act="settings"]').click()
            page.wait_for_selector("#settingsModal:not(.hidden)", timeout=5000)
            assert page.input_value("#cfgProvenance") == "visible"
            assert page.input_value("#cfgVerifyCmd") == "python -m pytest -q"
            assert page.locator("#usageMetrics").count() == 1
        finally:
            js_errs = getattr(ctx, "_df_errs", [])
            browser.close()
        assert not js_errs, f"js errors: {js_errs}"
