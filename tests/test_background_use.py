"""Background computer-use: ui_invoke scripting, window capture, bg-only policy, prompt, detach."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


from saturday.config import AgentConfig  # noqa: E402
from saturday.prompts.system import build_computer_use_section  # noqa: E402
from saturday.safety import ApprovalPolicy, check_command, make_approval_hook  # noqa: E402
from saturday.tools.spatial import (  # noqa: E402
    UiInvokeTool,
    capture_window_bg,
    ps_capture_window_script,
    ps_scan_script,
    ps_ui_invoke_script,
)


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
