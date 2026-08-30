"""Spatial awareness kit: grid math, landmarks, ui_tree parsing, pointer validation, gating."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


from saturday.safety import ApprovalPolicy, check_command  # noqa: E402
from saturday.tools.spatial import (  # noqa: E402
    LandmarkStore,
    PointerTool,
    UiTreeTool,
    WindowTool,
    build_grid_legend,
    cell_name,
    collect_marks,
    marked_legend,
    render_element_tree,
)


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
