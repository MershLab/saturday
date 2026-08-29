"""Background input delivery (hermes-style): pointer/keyboard accept window=<title>
and deliver via window messages — the user's cursor/keyboard/focus are untouched.
Covers script generation, safety gating in background-only mode, and a live
WinForms end-to-end on Windows."""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from saturday.safety import ApprovalPolicy, check_command  # noqa: E402
from saturday.tools.spatial import KeyboardTool, PointerTool  # noqa: E402

WIN_LIST = "4321|DF BG Target|100,200,800,600\n"


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


def test_pointer_background_drag_interpolates():
    runner = fake_runner([WIN_LIST, "ok"])
    tool = PointerTool(runner=runner)
    ok, _ = tool.run({"action": "drag", "x": 100, "y": 100, "x2": 220, "y2": 160, "window": "DF BG"})
    assert ok
    script = runner.calls[1]
    assert "0x201" in script and "0x200" in script and "0x202" in script
    assert script.count("TargetAt") >= 13, "down + interpolated moves + up"


def test_pointer_background_unknown_window_and_move_rejected():
    tool = PointerTool(runner=fake_runner([""]))
    ok, msg = tool.run({"action": "click", "x": 1, "y": 1, "window": "ghost app"})
    assert not ok and "no visible window matching" in msg
    tool2 = PointerTool(runner=fake_runner([WIN_LIST, "ok"]))
    ok, msg = tool2.run({"action": "move", "x": 1, "y": 1, "window": "DF BG"})
    assert not ok and "no meaning in background" in msg


def test_pointer_foreground_unchanged_without_window():
    runner = fake_runner(["ok"])
    tool = PointerTool(runner=runner)
    ok, msg = tool.run({"action": "click", "x": 10, "y": 20})
    assert ok and "ok" in msg
    assert "SetCursorPos" in runner.calls[0] and "PostMessageW" not in runner.calls[0]


def test_pointer_background_uses_landmarks():
    store = LandmarkStore()
    store.add("save", 250, 350, "Button")
    runner = fake_runner([WIN_LIST, "ok"])
    tool = PointerTool(landmarks=store, runner=runner)
    ok, _ = tool.run({"action": "click", "target": "save", "window": "DF BG"})
    assert ok
    assert "TargetAt([IntPtr]4321,250,350" in runner.calls[1]


from saturday.tools.spatial import LandmarkStore  # noqa: E402


# ----------------------------------------------------------------- keyboard


def test_keyboard_background_type_posts_wm_char():
    runner = fake_runner([WIN_LIST, "ok"])
    tool = KeyboardTool(runner=runner)
    ok, msg = tool.run({"action": "type", "text": "hi\n", "window": "DF BG"})
    assert ok, msg
    script = runner.calls[1]
    assert "[BgIn]::EditChild([IntPtr]4321)" in script
    assert f"[IntPtr]{ord('h')}" in script and f"[IntPtr]{ord('i')}" in script
    assert "[IntPtr]13" in script, "newline becomes Enter via WM_CHAR"


def test_keyboard_background_key_combo():
    runner = fake_runner([WIN_LIST, "ok"])
    tool = KeyboardTool(runner=runner)
    ok, msg = tool.run({"action": "key", "key": "Enter", "window": "DF BG"})
    assert ok, msg
    script = runner.calls[1]
    assert "0x100" in script and "0x101" in script and "[IntPtr]13" in script


def test_keyboard_background_unknown_window():
    tool = KeyboardTool(runner=fake_runner([""]))
    ok, msg = tool.run({"action": "type", "text": "x", "window": "nope"})
    assert not ok and "no visible window matching" in msg


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
