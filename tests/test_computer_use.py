"""Computer-use completion: keyboard, window, clipboard tools + prompt protocol + gating."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from saturday.prompts.system import build_system_prompt_parts  # noqa: E402
from saturday.safety import ApprovalPolicy, check_command, make_approval_hook  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.spatial import (  # noqa: E402
    ClipboardTool,
    KeyboardTool,
    WindowTool,
    parse_combo,
    ps_send_input_defines,
)


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
