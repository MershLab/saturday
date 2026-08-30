"""macOS + Linux computer-use backends (Cowork parity, no sandbox).

Everything here runs FOREGROUND on the user's real desktop (there is no
parallel to Windows' background delivery on these platforms yet — that stays
the Windows differentiator). Dependencies kept optional and checked before
use so the failures are clear commands, not stack traces:

  macOS:  AppleScript/System Events (built-in) + screencapture + cliclick
          (brew install cliclick) + pbcopy/pbpaste + tesseract (ui_text)
  Linux:  xdotool + wmctrl + xclip (or wl-paste) + tesseract (ui_text)
          + ImageMagick 'import' for window capture

Verified by the maintainer on real hardware — the Windows paths in
spatial.py are untouched and remain the validated reference.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys

from saturday.tools.spatial import LandmarkStore

MAC = sys.platform == "darwin"

UNSUPPORTED_HINT = (
    "requires Windows (background delivery / UIA scan); on macOS use "
    "ui_text (OCR) + pointer target=<id>, on Linux use ui_text/pointer/keyboard via xdotool"
)


def _run(argv: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"


# conservative vs. the real Linux ARG_MAX (typically ~2MB): keeps each
# xdotool invocation small so a hung/failing chunk fails fast and pinpoints
# roughly where in the text it happened, at the cost of more subprocess
# spawns for very long text.
_TYPE_CHUNK_CHARS = 256


def xdotool_type_chunk(text: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Default (non-mocked) runner for KeyboardTool's chunked type action: one
    xdotool invocation per text chunk. Kept as its own argv-building call
    (not reusing _run directly) so KeyboardTool can inject a fake in tests
    without shelling out."""
    return _run(["xdotool", "type", "--delay", "15", text], timeout=timeout)


def osascript_type_chunk(text: str, timeout: float = 60.0) -> tuple[int, str, str]:
    """Default (non-mocked) runner for KeyboardTool's chunked type action on
    macOS: one osascript keystroke invocation per text chunk, mirroring
    xdotool_type_chunk's role for Linux."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "System Events" to keystroke "{escaped}"'
    return _run(["osascript", "-e", script], timeout=timeout)


def _notes() -> str:
    return " (macOS/Linux backend — verify on real hardware)"


# ---------------------------------------------------------------------------
# window scanning (shared by window tool + fallback capture)


def mac_window_scan() -> tuple[bool, str, list[dict]]:
    script = (
        'tell application "System Events"\n'
        "  set out to \"\"\n"
        "  repeat with p in (every process)\n"
        "    try\n"
        "      repeat with w in (every window of p)\n"
        "        try\n"
        "          set t to name of w\n"
        "          set pos to position of w\n"
        "          set siz to size of w\n"
        "          set out to out & (name of p) & \"|\" & t & \"|\" & (item 1 of pos) & \"|\" & "
        "(item 2 of pos) & \"|\" & (item 1 of siz) & \"|\" & (item 2 of siz) & \"|\" & (id of w) & \"\\n\"\n"
        "        end try\n"
        "      end repeat\n"
        "    end try\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )
    rc, out, err = _run(["osascript", "-e", script], timeout=30.0)
    if rc != 0:
        return False, (err or "osascript failed (grant Accessibility permission in System Settings)")[:300], []
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 7:
            continue
        try:
            x, y, w, h = (int(parts[i]) for i in (2, 3, 4, 5))
        except ValueError:
            continue
        rows.append({"proc": parts[0], "title": parts[1], "left": x, "top": y, "width": w, "height": h, "winid": parts[6]})
    return True, "", rows


def linux_window_scan() -> tuple[bool, str, list[dict]]:
    if shutil.which("wmctrl") is None:
        return False, "wmctrl not found — install it (sudo apt install wmctrl / dnf install wmctrl)", []
    rc, out, err = _run(["wmctrl", "-lG"], timeout=15.0)
    if rc != 0:
        return False, (err or "wmctrl failed")[:300], []
    rows = []
    for line in out.splitlines():
        m = re.match(r"^(0x[0-9a-fA-F]+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        rows.append({
            "winid": m.group(1), "left": int(m.group(2)), "top": int(m.group(3)),
            "width": int(m.group(4)), "height": int(m.group(5)), "desk": m.group(6),
            "title": m.group(7),
        })
    return True, "", rows


def scan_windows() -> tuple[bool, str, list[dict]]:
    if MAC:
        return mac_window_scan()
    return linux_window_scan()


def _resolve_window(query: str) -> tuple[bool, str, dict]:
    ok, err, rows = scan_windows()
    if not ok:
        return False, err, {}
    q = (query or "").strip().lower()
    if not q:
        return False, "query= is required", {}
    row = next((r for r in rows if r.get("title", "").lower() == q), None)
    if row is None:
        row = next((r for r in rows if r.get("title", "").lower().startswith(q)), None)
    if row is None:
        row = next((r for r in rows if q in r.get("title", "").lower()), None)
    if row is None:
        sample = "; ".join(r.get("title", "") for r in rows[:10])
        return False, f"no window matching {query!r}. Visible: {sample or '(none: grant Accessibility on macOS, use wmctrl on Linux)'}", {}
    return True, "", row


# ---------------------------------------------------------------------------
# window tool


def window_tool(self, args: dict) -> tuple[bool, str]:
    action = args.get("action")
    if action not in ("list", "focus", "minimize", "maximize", "restore", "close"):
        return False, f"unknown window action {action!r}"
    if action == "list":
        ok, err, rows = scan_windows()
        if not ok:
            return False, err
        lines = [f"pid/title={r.get('title')!r} rect=({r.get('left')},{r.get('top')},{r.get('width')},{r.get('height')})" for r in rows][:40]
        return True, ("\n".join(lines) or "(no visible windows)") + _notes()
    ok, err, row = _resolve_window(str(args.get("query") or ""))
    if not ok:
        return False, err
    title = row["title"]
    if MAC:
        proc = row["proc"]
        winname = title.replace('"', '\\"')
        if action == "focus":
            script = f'tell application "System Events" to set frontmost of (first process whose name is "{proc}") to true'
        elif action in ("minimize", "restore"):
            script = (
                f'tell application "System Events" to tell process "{proc}" to set miniaturized '
                f'of window "{winname}" to {"true" if action == "minimize" else "false"}'
            )
        elif action == "maximize":
            script = f'tell application "System Events" to tell process "{proc}" to perform action "AXZoom" of window "{winname}"'
        else:  # close
            script = f'tell application "System Events" to tell process "{proc}" to perform action "AXClose" of window "{winname}"'
        rc, _, err = _run(["osascript", "-e", script], timeout=20.0)
    else:
        wid = row["winid"]
        if action == "focus":
            rc, _, err = _run(["xdotool", "windowactivate", wid, "windowfocus", wid], timeout=10.0)
        elif action == "minimize":
            rc, _, err = _run(["xdotool", "windowminimize", wid], timeout=10.0)
        elif action == "restore":
            rc, _, err = _run(["xdotool", "windowmap", wid], timeout=10.0)
        elif action == "maximize":
            rc, _, err = _run(["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"], timeout=10.0)
        else:
            rc, _, err = _run(["wmctrl", "-i", "-c", wid], timeout=10.0)
    if rc != 0:
        return False, f"{action} failed: {(err or '').strip()[-300:]}"
    return True, f"{action} {title!r} ok" + _notes()


# ---------------------------------------------------------------------------
# pointer


def _cliclick_or_fail() -> tuple[bool, str]:
    if MAC and shutil.which("cliclick") is None:
        return False, "cliclick not found — install it first: brew install cliclick"
    return True, ""


def pointer_tool(self, args: dict) -> tuple[bool, str]:
    action = args.get("action")
    window_q = str(args.get("window") or "").strip()
    delivery = str(args.get("delivery") or ("background" if window_q else "foreground")).lower()
    if delivery == "background" and window_q:
        return False, "background delivery requires Windows (the macOS/Linux backends are foreground-only)"
    if action == "move":
        x, y = int(args.get("x") or 0), int(args.get("y") or 0)
        if MAC:
            ok, err = _cliclick_or_fail()
            if not ok:
                return False, err
            rc, _, err = _run(["cliclick", f"m:{x},{y}"], timeout=10.0)
            return (True, f"move at ({x},{y}) ok") if rc == 0 else (False, (err or "move failed")[:200])
        rc, _, err = _run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=10.0)
        return (True, f"move at ({x},{y}) ok") if rc == 0 else (False, (err or "move failed")[:200])
    if action not in ("click", "double_click", "right_click", "middle_click"):
        return False, f"action {action!r} not implemented on macOS/Linux (use click/double/right/move)"
    x, y = int(args.get("x") or 0), int(args.get("y") or 0)
    if MAC:
        ok, err = _cliclick_or_fail()
        if not ok:
            return False, err
        seq = {"click": ["c", 1], "double_click": ["dc", 1], "right_click": ["rc", 1], "middle_click": ["mc", 1]}
        if action == "middle_click":
            return False, "middle_click not supported by cliclick — use x,y of the element and a two-button fallback"
        cmd = ["cliclick", f"{seq[action][0]}:{x},{y}"]
        rc, _, err = _run(cmd, timeout=10.0)
        return (True, f"{action} at ({x},{y}) ok") if rc == 0 else (False, (err or "click failed")[:200])
    btn = {"click": 1, "double_click": "2", "right_click": 3, "middle_click": 2}[action]
    cmd = ["xdotool", "mousemove", "--sync", str(x), str(y), "click"]
    if btn == "2":
        cmd += ["--repeat", "2", "1"]
    else:
        cmd += [str(btn)]
    rc, _, err = _run(cmd, timeout=10.0)
    return (True, f"{action} at ({x},{y}) ok") if rc == 0 else (False, (err or "click failed")[:200])


# ---------------------------------------------------------------------------
# keyboard


# Windows virtual-key -> macOS kVK. Letters/digits/F-keys/arrows are correct
# from the canonical kVK tables; punctuation uses the Windows OEM VKs.
_MAC_KVK: dict[int, int] = {
    65: 0, 66: 11, 67: 8, 68: 2, 69: 14, 70: 3, 71: 5, 72: 4, 73: 34, 74: 38,
    75: 40, 76: 37, 77: 46, 78: 45, 79: 31, 80: 35, 81: 12, 82: 15, 83: 1,
    84: 17, 85: 32, 86: 9, 87: 13, 88: 7, 89: 16, 90: 6,
    48: 29, 49: 18, 50: 19, 51: 20, 52: 21, 53: 23, 54: 22, 55: 26, 56: 28, 57: 25,
    187: 24, 189: 27, 190: 47, 188: 43, 191: 44, 192: 50,
    219: 33, 220: 42, 221: 30, 222: 39,
    8: 51, 9: 48, 13: 36, 20: 57, 27: 53, 32: 49, 33: 116, 34: 121, 35: 119,
    36: 115, 37: 123, 38: 126, 39: 124, 40: 125, 46: 117,
    112: 122, 113: 120, 114: 99, 115: 118, 116: 96, 117: 97, 118: 98, 119: 100,
    120: 101, 121: 109, 122: 103, 123: 111,
}

_MAC_MODS = {"ctrl": "control down", "alt": "option down", "shift": "shift down", "cmd": "command down", "win": "command down"}


def keyboard_tool(self, args: dict) -> tuple[bool, str]:
    action = args.get("action")
    window_q = str(args.get("window") or "").strip()
    if window_q and str(args.get("delivery") or "background").lower() == "background":
        return False, "background keyboard delivery requires Windows (macOS/Linux backends are foreground-only)"
    if action not in ("type", "key"):
        return False, f"unknown keyboard action {action!r}"
    if MAC:
        # AppleScript keystroke is TEXT and key-code based; type works with
        # unicode in most scopes, but always runs against the frontmost app.
        if action == "type":
            text = str(args.get("text") or "")
            if not text:
                return False, "action=type needs text="
            # chunked through self._run_ps for the same reason as the Linux
            # path: a long string embedded in one AppleScript command risks
            # real reliability limits and gives no partial-progress error
            # location. self._run_ps is the injection point (real
            # osascript_type_chunk by default) so this stays mockable in
            # tests without a real osascript/System Events round-trip.
            sent = 0
            for start in range(0, len(text), _TYPE_CHUNK_CHARS):
                chunk = text[start : start + _TYPE_CHUNK_CHARS]
                rc, _, err = self._run_ps(chunk, timeout=60.0)
                if rc != 0:
                    return False, f"keyboard failed at char {sent}: {(err or 'osascript not found or failed').strip()[-300:]}"
                sent += len(chunk)
            return True, f"typed {len(text)} chars ok"
        spec = str(args.get("key") or "")
        combo = parse_combo_mac(spec)
        if combo is None:
            return False, f"unsupported key-combo on macOS: {spec!r}"
        code, mods = combo
        using = " using {" + ", ".join(mods) + "}" if mods else ""
        script = f'tell application "System Events" to key code {code}{using}'
        rc, _, err = _run(["osascript", "-e", script], timeout=20.0)
        return (True, f"{action} ok") if rc == 0 else (False, (err or "keyboard failed")[:300])
    if action == "type":
        text = str(args.get("text") or "")
        if not text:
            return False, "action=type needs text="
        # chunked (not one shell-out for the whole string): a long paste as a
        # single xdotool argv element risks the real ARG_MAX and gives no
        # partial-progress error location; mirrors the Windows path's
        # statement-batching for the same reason. self._run_ps is the
        # injection point (real xdotool_type_chunk by default) so this stays
        # mockable in tests without a real xdotool on PATH.
        sent = 0
        for start in range(0, len(text), _TYPE_CHUNK_CHARS):
            chunk = text[start : start + _TYPE_CHUNK_CHARS]
            rc, _, err = self._run_ps(chunk, timeout=60.0)
            if rc != 0:
                return False, f"keyboard failed at char {sent}: {(err or 'xdotool not found or failed').strip()[-300:]}"
            sent += len(chunk)
        return True, f"typed {len(text)} chars ok"
    if shutil.which("xdotool") is None:
        return False, "xdotool not found (required for keyboard on Linux)"
    spec = str(args.get("key") or "")
    sym = translate_linux_key(spec)
    if sym is None:
        return False, f"unsupported key-combo on Linux: {spec!r}"
    rc, _, err = _run(["xdotool", "key", sym], timeout=10.0)
    return (True, "key ok") if rc == 0 else (False, (err or "keyboard failed")[:300])


def parse_combo_mac(spec: str) -> tuple[int, list[str]] | None:
    """'(Cmd/Ctrl+Shift+)Key' -> (mac kVK, [modifiers]) or None when unmapped."""
    from saturday.tools.spatial import parse_combo

    spec = re.sub(r"(^|\+)cmd(\+|$)", r"\1win\2", (spec or ""), flags=re.IGNORECASE)  # cmd == win == macOS command key
    _MOD_VKS = (17, 16, 18, 91, 92)  # ctrl/shift/alt/win
    try:
        events = parse_combo(spec)
    except ValueError:
        return None
    main: int | None = None
    for vk, down in events:
        if vk not in _MOD_VKS and down:
            main = vk
    if main is None:
        return None
    kvk = _MAC_KVK.get(main)
    if kvk is None:
        return None
    mods = sorted({_MAC_MODS.get(name, "") for name, _ in parse_combo2(spec) if name in _MAC_MODS} - {""})
    return kvk, mods


def parse_combo2(spec: str) -> list[tuple[str, bool]]:
    """Modifier-name pass over the (already normalized) combo."""
    out: list[tuple[str, bool]] = []
    for part in spec.split("+"):
        part = part.strip().lower()
        down = not part.startswith("-")
        out.append((part.lstrip("-"), down))
    return out


def translate_linux_key(spec: str) -> str | None:
    from saturday.tools.spatial import parse_combo

    try:
        events = parse_combo(spec)
    except ValueError:
        return None
    parts = []
    for vk, down in events:
        if down:
            parts.append(_LINUX_MOD_VK.get(vk, _LINUX_KEYS.get(vk, _LINUX_LETTER.get(vk))))
    if any(p is None for p in parts):
        return None
    return "+".join(parts)


_LINUX_MOD_VK = {17: "ctrl", 16: "shift", 18: "alt", 91: "super", 92: "super"}
_LINUX_KEYS = {13: "Return", 9: "Tab", 27: "Escape", 32: "space", 8: "BackSpace", 112: "F1", 113: "F2", 114: "F3", 115: "F4", 116: "F5", 117: "F6", 118: "F7", 119: "F8", 120: "F9", 121: "F10", 122: "F11", 123: "F12", 38: "Up", 40: "Down", 37: "Left", 39: "Right", 33: "Page_Up", 34: "Page_Down", 36: "Home", 35: "End"}
_LINUX_LETTER = {vk: chr(vk).lower() for vk in range(65, 91)} | {vk: chr(vk) for vk in range(48, 58)}


# ---------------------------------------------------------------------------
# clipboard


def clipboard_tool(self, args: dict) -> tuple[bool, str]:
    action = args.get("action")
    if MAC:
        if action == "get":
            rc, out, err = _run(["pbpaste"], timeout=10.0)
            return (True, out.strip() or "(clipboard empty)") if rc == 0 else (False, err[:200])
        text = str(args.get("text") or "")
        rc, _, err = subprocess.run(["pbcopy"], input=text, capture_output=True, text=True, errors="replace", timeout=10.0).returncode, "", ""
        return (True, "clipboard set") if rc == 0 else (False, err[:200])
    if shutil.which("xclip") is None and shutil.which("wl-paste") is None:
        return False, "xclip or wl-paste not found (install xclip; Wayland: install wl-clipboard)"
    if action == "get":
        exe = shutil.which("wl-paste") or shutil.which("xclip")
        argv = [exe, "-selection", "clipboard"] if exe.endswith("xclip") else [exe]
        rc, out, err = _run(argv, timeout=10.0)
        return (True, out.strip()[:2000] or "(clipboard empty)") if rc == 0 else (False, err[:200])
    text = str(args.get("text") or "")
    exe = shutil.which("xclip")
    if exe:
        proc = subprocess.run([exe, "-selection", "clipboard"], input=text, capture_output=True, text=True, errors="replace", timeout=10.0)
        return (True, "clipboard set") if proc.returncode == 0 else (False, proc.stderr[:200])
    proc = subprocess.run(["wl-copy"], input=text, capture_output=True, text=True, errors="replace", timeout=10.0)
    return (True, "clipboard set") if proc.returncode == 0 else (False, proc.stderr[:200])


# ---------------------------------------------------------------------------
# app open


def app_open_tool(self, args: dict) -> tuple[bool, str]:
    target = str(args.get("target") or "").strip()
    extra = str(args.get("args") or "").strip()
    import shlex

    args_parts = shlex.split(extra) if extra else []
    if MAC:
        argv = ["open", "-a", target] + (["--args"] + args_parts if args_parts else [])
        rc, _, err = _run(argv, timeout=15.0)
        return (True, f"opened {target!r}") if rc == 0 else (False, (err or "open failed")[:300])
    exe = shutil.which(target)
    if exe is None:
        return False, f"cannot find {target!r} on PATH"
    try:
        subprocess.Popen([exe, *args_parts], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, f"launch failed: {exc}"
    return True, f"launched {target!r} (foreground window appearance is app-defined)"


# ---------------------------------------------------------------------------
# ui_tree / ui_invoke — macOS best-effort, Linux explicit OCR suggestion


def _mac_elements(proc: str, winname: str) -> tuple[bool, str]:
    script = (
        f'tell application "System Events" to tell process "{proc}" to tell window "{winname}"\n'
        "  set out to \"\"\n"
        "  repeat with e in (every UI element)\n"
        "    try\n"
        "      set nm to (description of e)\n"
        "      if nm is missing value then set nm to (name of e)\n"
        "      set out to out & (role of e) & \"|\" & nm & \"\\n\"\n"
        "    end try\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )
    rc, out, err = _run(["osascript", "-e", script], timeout=45.0)
    if rc != 0:
        return False, (err or "osascript failed (grant Accessibility permission)")[:300]
    return True, out


def ui_tree_tool(self, args: dict, landmarks: LandmarkStore | None = None) -> tuple[bool, str]:
    query = str(args.get("window") or args.get("query") or "").strip()
    if not query:
        return False, "mac/Linux ui_tree needs window=<title substring> (or use ui_text for OCR grounding)"
    ok, err, row = _resolve_window(query)
    if not ok:
        return False, err
    if not MAC:
        return False, "ui_tree on Linux uses AT-SPI which is not wired yet — use ui_text (OCR) + pointer target=<id>"
    ok, text = _mac_elements(row["proc"], row["title"].replace('"', '\\"'))
    if not ok:
        return False, text
    lines = ["macOS element tree (System Events):"]
    for line in text.splitlines()[:120]:
        if "|" in line:
            role, nm = line.split("|", 1)
            lines.append(f"- {role} {nm[:80]}")
    return True, "\n".join(lines) or "(no elements reported)"


def ui_invoke_tool(self, args: dict) -> tuple[bool, str]:
    return False, "ui_invoke requires Windows (UIA); on macOS use ui_tree + ui_text, on Linux use ui_text + pointer target=<id>"
