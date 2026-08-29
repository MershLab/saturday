"""Offline computer-use smoke test: opens Calculator in the background without
stealing focus, drives it via UI Automation patterns (7 x 6 =), reads the
display back through the accessibility tree and verifies. No mouse, no focus,
no LLM. Skips honestly when Calculator is unavailable."""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _foreground_title() -> str:
    u = ctypes.windll.user32
    b = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(u.GetForegroundWindow(), b, 256)
    return b.value


def main() -> int:
    if not sys.platform.startswith("win"):
        print("SKIP: computer-use smoke requires Windows.")
        return 0

    from saturday.tools.spatial import AppOpenTool, UiInvokeTool, UiTreeTool, WindowTool

    fg_before = _foreground_title()
    print(f"[0] foreground : {fg_before!r}")

    try:
        ok, out = AppOpenTool().run({"target": "calc"})
        print(f"[1] app_open   : {out}")
        assert ok, f"app_open failed: {out}"
        time.sleep(1.5)

        win = WindowTool()
        ok, out = win.run({"action": "restore", "query": "calculator"})
        print(f"[2] restore    : {out}")
        assert ok, f"window restore failed: {out}"
        time.sleep(0.8)

        inv = UiInvokeTool(restore_focus_after=True)
        for btn in ("Seven", "Multiply by", "Six", "Equals"):
            ok, out = inv.run({"action": "press", "name": btn, "window": "calculator", "wait_seconds": 3})
            print(f"[3] press {btn:<10}: ok={ok} {out[:80] if not ok else ''}")
            assert ok, f"press {btn} failed: {out}"

        tree = UiTreeTool()
        ok, out = tree.run({"scope": "win:calculator", "wait_seconds": 2})
        assert ok, f"ui_tree failed: {out}"

        fg_after = _foreground_title()
        print(f"[4] display    : {'42' if '42' in out else '?'} | foreground preserved: {fg_after == fg_before!r}")

        assert "42" in out, f"display did not show 42:\n{out[:800]}"
        assert fg_after == fg_before, (
            f"FOCUS STOLEN: foreground changed {fg_before!r} -> {fg_after!r}"
        )

        print("\nCOMPUTER-USE SMOKE PASS (background launch + UIA press/read + focus preservation)")
        return 0
    except AssertionError as exc:
        print(f"\nCOMPUTER-USE SMOKE FAIL: {exc}")
        return 1
    finally:
        subprocess.run(["taskkill", "/IM", "CalculatorApp.exe", "/F"], capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
