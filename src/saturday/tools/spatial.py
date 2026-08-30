"""Spatial awareness kit: UI Automation scanning, annotated screenshots,
pointer actuation, and landmark memory. Windows-first via PowerShell/.NET,
stdlib-only; other platforms degrade gracefully."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

from saturday.tools.base import Tool

DPI_PREAMBLE = (
    "Add-Type -TypeDefinition 'using System.Runtime.InteropServices;"
    "public class DpiFix{[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();}';"
    "[DpiFix]::SetProcessDPIAware()|Out-Null;"
)

INTERACTIVE_TYPES = (
    "Button",
    "MenuItem",
    "Edit",
    "Hyperlink",
    "TabItem",
    "ListItem",
    "CheckBox",
    "RadioButton",
    "ComboBox",
    "Slider",
    "SplitButton",
)

MAX_ELEMENTS = 250
MAX_DEPTH = 9

WINDOW_LIST_SCRIPT = (
    DPI_PREAMBLE
    + "Add-Type -TypeDefinition 'using System;using System.Text;using System.Runtime.InteropServices;"
    "public class WinEnum{public delegate bool CB(IntPtr h,IntPtr l);"
    "[DllImport(\"user32.dll\")]public static extern bool EnumWindows(CB cb,IntPtr l);"
    "[DllImport(\"user32.dll\")]public static extern bool IsWindowVisible(IntPtr h);"
    "[DllImport(\"user32.dll\",CharSet=CharSet.Unicode)]public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);"
    "[DllImport(\"user32.dll\")]public static extern bool GetWindowRect(IntPtr h,out RECT r);"
    "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();"
    "public struct RECT{public int L,T,R,B;}}';"
    "$out=New-Object System.Collections.Generic.List[string];"
    "$cb=[WinEnum+CB]{param($h,$l) if([WinEnum]::IsWindowVisible($h)){$sb=New-Object System.Text.StringBuilder 256;"
    "[WinEnum]::GetWindowText($h,$sb,256)|Out-Null;$t=$sb.ToString();$r=New-Object WinEnum+RECT;"
    "if($t -and [WinEnum]::GetWindowRect($h,[ref]$r)){$out.Add(($h.ToInt64()).ToString()+'|'+$t.Replace('|',' ')+\"|$($r.L),$($r.T),$($r.R-$r.L),$($r.B-$r.T)\")}}; $true};"
    "[WinEnum]::EnumWindows($cb,[IntPtr]::Zero)|Out-Null;$out"
)


def parse_window_list(stdout: str) -> list[dict]:
    rows = []
    for line in (stdout or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        try:
            hwnd = int(parts[0])
            left, top, w, h = (int(v) for v in parts[2].split(","))
        except ValueError:
            continue
        rows.append({"hwnd": hwnd, "title": parts[1], "left": left, "top": top, "width": w, "height": h})
    return rows


def resolve_window(query: str, runner=None) -> dict | None:
    """Find a visible top-level window by title substring -> {hwnd,title,left,top,width,height}."""
    runner = runner or run_ps
    rc, out, _ = runner(WINDOW_LIST_SCRIPT, timeout=15.0)
    if rc != 0:
        return None
    rows = parse_window_list(out)
    if not rows:
        return None
    title = WindowTool.pick([r["title"] for r in rows], query)
    if title is None:
        return None
    return next(r for r in rows if r["title"] == title)


# C# helper for background (window-targeted) input delivery: posts messages to
# the deepest child window under a point / the primary text control, so the
# user's cursor and keyboard focus are never touched.
BG_INPUT_DEFINES = (
    "Add-Type -TypeDefinition 'using System;using System.Text;using System.Runtime.InteropServices;"
    "public class BgIn{public delegate bool CB(IntPtr h,IntPtr l);"
    "[StructLayout(LayoutKind.Sequential)]public struct PT{public int X;public int Y;public PT(int x,int y){X=x;Y=y;}}"
    "[StructLayout(LayoutKind.Sequential)]public struct RC{public int L;public int T;public int R;public int B;}"
    "[DllImport(\"user32.dll\")]public static extern bool PostMessageW(IntPtr h,uint m,IntPtr w,IntPtr l);"
    "[DllImport(\"user32.dll\")]public static extern IntPtr RealChildWindowFromPoint(IntPtr h,PT p);"
    "[DllImport(\"user32.dll\")]public static extern bool ClientToScreen(IntPtr h,ref PT p);"
    "[DllImport(\"user32.dll\")]public static extern bool EnumChildWindows(IntPtr h,CB cb,IntPtr l);"
    "[DllImport(\"user32.dll\")]public static extern bool IsWindowVisible(IntPtr h);"
    "[DllImport(\"user32.dll\",CharSet=CharSet.Unicode)]public static extern int GetClassName(IntPtr h,StringBuilder s,int n);"
    "[DllImport(\"user32.dll\")]public static extern bool GetWindowRect(IntPtr h,out RC r);"
    "public static IntPtr TargetAt(IntPtr h,int sx,int sy,out int lx,out int ly){"
    "PT o=new PT(0,0);ClientToScreen(h,ref o);int cx=sx-o.X,cy=sy-o.Y;"
    "IntPtr t=RealChildWindowFromPoint(h,new PT(cx,cy));"
    "if(t==IntPtr.Zero||t==h){lx=cx;ly=cy;return h;}"
    "PT c0=new PT(0,0);ClientToScreen(t,ref c0);lx=sx-c0.X;ly=sy-c0.Y;return t;}"
    "public static IntPtr EditChild(IntPtr h){IntPtr best=IntPtr.Zero;int area=0;"
    "EnumChildWindows(h,(c,l)=>{if(!IsWindowVisible(c))return true;"
    "StringBuilder sb=new StringBuilder(64);GetClassName(c,sb,64);string cn=sb.ToString().ToLowerInvariant();"
    "if(cn.Contains(\"edit\")||cn==\"scintilla\"){RC r;GetWindowRect(c,out r);"
    "int a=(r.R-r.L)*(r.B-r.T);if(a>area){area=a;best=c;}}return true;},IntPtr.Zero);return best;}"
    "public static bool Post(IntPtr h,uint m,IntPtr w,IntPtr l){return PostMessageW(h,m,w,l);}}';"
)


def _pack_lparam(x: int, y: int) -> str:
    return f"[IntPtr]((({y} -band 0xFFFF) -shl 16) -bor ({x} -band 0xFFFF))"


# PS 5.1 writes redirected stdout in the OEM codepage while Python's default
# text decode is cp1252 -> mojibake for any non-ASCII title/value. Forcing
# UTF8 on the console side + decoding utf-8/replace on ours keeps them aligned.
PS_UTF8_PREFIX = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"


def run_ps(script: str, timeout: float = 25.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", PS_UTF8_PREFIX + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def cell_name(col: int, row: int) -> str:
    letters = ""
    c = col
    while True:
        letters = chr(ord("A") + c % 26) + letters
        c = c // 26 - 1
        if c < 0:
            break
    return f"{letters}{row + 1}"


class LandmarkStore:
    """Named screen positions discovered during scans; shared by scanner and pointer."""

    def __init__(self) -> None:
        self._points: dict[str, dict] = {}

    @staticmethod
    def _key(name: str) -> str:
        k = "".join(c if c.isalnum() else "_" for c in (name or "").strip().lower())[:40]
        return k.strip("_") or "element"

    def add(self, name: str, x: int, y: int, role: str = "") -> str:
        base = self._key(name)
        key = base
        n = 2
        while key in self._points and self._points[key].get("x") != x:
            key = f"{base}_{n}"
            n += 1
        self._points[key] = {"x": int(x), "y": int(y), "role": role, "name": name}
        return key

    def resolve(self, target: str):
        t = (target or "").strip()
        if t in self._points:
            return self._points[t]
        low = t.lower()
        for key, pt in self._points.items():
            if key.lower() == low:
                return pt
        needle = self._key(t)
        starts = [(k, v) for k, v in self._points.items() if k.startswith(needle)]
        if starts:
            return min(starts, key=lambda kv: len(kv[0]))[1]
        contains = [(k, v) for k, v in self._points.items() if needle in k]
        if contains:
            return min(contains, key=lambda kv: len(kv[0]))[1]
        return None

    def render(self, limit: int = 30) -> str:
        items = list(self._points.items())[-limit:]
        return "\n".join(f"{k}: {v['role'] or 'element'} at ({v['x']},{v['y']})" for k, v in items)


def ps_scan_script(scope: str) -> str:
    if scope.startswith("win:"):
        needle = scope[4:].replace("'", "''")
        focus = (
            "$cand=$null;"
            "[System.Windows.Automation.AutomationElement]::RootElement.FindAll("
            "[System.Windows.Automation.TreeScope]::Children,"
            "[System.Windows.Automation.Condition]::TrueCondition)"
            "| ForEach-Object { if(-not $cand -and $_.Current.Name -and $_.Current.Name.ToLower().Contains('" + needle.lower() + "')){$cand=$_} };"
            "$start=$cand;"
            "if(-not $start){Write-Output '[]';exit}"
        )
    elif scope == "foreground":
        focus = (
            "$start=[System.Windows.Automation.AutomationElement]::FocusedElement;"
            "while($start -and $start.Current.NativeWindowHandle -eq 0){$start=[System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($start)};"
        )
    else:
        focus = "$start=[System.Windows.Automation.AutomationElement]::RootElement;"
    return (
        DPI_PREAMBLE
        + "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
        + focus
        + "$all=$start.FindAll([System.Windows.Automation.TreeScope]::Descendants,"
        "[System.Windows.Automation.Condition]::TrueCondition);"
        "$out=New-Object System.Collections.Generic.List[object];"
        "$n=0;"
        "foreach($el in $all){"
        "if($n -ge " + str(MAX_ELEMENTS) + "){break}"
        "$c=$el.Current;$r=$c.BoundingRectangle;"
        "$out.Add([pscustomobject]@{n=$c.Name;t=$c.ControlType.ProgrammaticName;x=[int]$r.X;y=[int]$r.Y;w=[int]$r.Width;h=[int]$r.Height;off=$c.IsOffscreen});"
        "$n++};"
        "$out|ConvertTo-Json -Compress -Depth 3"
    )


def parse_scan(stdout: str) -> list[dict]:
    txt = (stdout or "").strip()
    if not txt:
        return []
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        start = txt.find("[")
        end = txt.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            data = json.loads(txt[start : end + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)]


def render_element_tree(elements: list[dict], store: LandmarkStore | None = None) -> tuple[str, list[str]]:
    lines: list[str] = []
    landmarks: list[str] = []
    for i, e in enumerate(elements):
        if e.get("off"):
            continue
        role = str(e.get("t") or "ControlType.Custom").replace("ControlType.", "")
        name = str(e.get("n") or "")
        x, y, w, h = int(e.get("x") or 0), int(e.get("y") or 0), int(e.get("w") or 0), int(e.get("h") or 0)
        if w <= 0 or h <= 0:
            continue
        marker = ""
        if store is not None and role in INTERACTIVE_TYPES and name:
            key = store.add(name, x + w // 2, y + h // 2, role)
            landmarks.append(key)
            marker = f" [{key}]"
        label = f"{name!r} " if name else ""
        lines.append(f"{role} {label}rect=({x},{y},{w},{h}) center=({x + w // 2},{y + h // 2}){marker}")
        if len(lines) >= 150:
            lines.append(f"... {len(elements) - i - 1} more elements hidden")
            break
    return "\n".join(lines), landmarks


class UiTreeTool(Tool):
    """Dump visible UI elements with exact bounding boxes as text."""

    name = "ui_tree"
    description = (
        "Scan on-screen UI elements (accessibility tree) and return each one's type, name, "
        "bounding box and center coordinate as exact text. Interactive elements get landmark "
        "ids like [save] you can pass to the pointer tool as target. scope='foreground' scans "
        "the focused window, scope='desktop' scans all top-level windows."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "'foreground' (default), 'desktop', or 'win:<title substring>' to scan a background window non-intrusively",
            },
            "wait_seconds": {"type": "number", "description": "poll until elements appear; useful right after app_open"},
            "mode": {
                "type": "string",
                "enum": ["delta", "full"],
                "description": "delta (default): report what changed vs the cached scan (cheap after the first view); full: everything again",
            },
        },
        "required": [],
    }

    def __init__(self, landmarks: LandmarkStore | None = None, runner=run_ps, cache=None) -> None:
        self.landmarks = landmarks or LandmarkStore()
        self._run_ps = runner
        from saturday.statemap import StateCache

        self.cache = cache if cache is not None else StateCache()

    def run(self, args: dict) -> tuple[bool, str]:
        if not sys.platform.startswith("win"):
            from saturday.tools import spatial_unix

            return spatial_unix.ui_tree_tool(self, args, landmarks=self.landmarks)
        scope = args.get("scope") or "foreground"
        if scope.startswith("win:"):
            pass
        elif scope not in ("foreground", "desktop"):
            scope = "foreground"
        try:
            wait = float(args.get("wait_seconds") or 0)
        except (TypeError, ValueError):
            wait = 0.0
        attempts = max(1, min(12, int(wait * 2) + 1))
        elements = []
        last_err = ""
        for _ in range(attempts):
            rc, out, err = self._run_ps(ps_scan_script(scope))
            elements = parse_scan(out) if rc == 0 else []
            if elements:
                break
            last_err = (err or "").strip()[-300:]
            if attempts > 1:
                time.sleep(0.5)
        if not elements:
            return False, f"ui_tree scan returned nothing: {last_err}"
        scope_key = str(scope)
        prev = self.cache.last_scan(scope_key) if self.cache else None
        delta = self.cache.put_scan(scope_key, elements) if self.cache else None
        if delta is not None and prev and args.get("mode", "delta") != "full":
            added, removed, changed = delta["added"], delta["removed"], delta["changed"]
            if not added and not removed and not changed:
                return True, f"ui_tree delta (scope={scope}): NO CHANGE - identical to the cached scan ({len(elements)} elements)"
            tree, found = render_element_tree(added + changed, self.landmarks)
            header = (
                f"ui_tree delta (scope={scope}): +{len(added)} new, ~{len(changed)} moved/resized, "
                f"-{len(removed)} gone (cached total {len(elements)})"
            )
            tail = "\nlandmark ids usable as pointer targets:\n" + self.landmarks.render() if found else ""
            return True, f"{header}\n{tree}{tail}"
        tree, found = render_element_tree(elements, self.landmarks)
        header = f"scope={scope} elements={len([e for e in elements if not e.get('off')])}"
        tail = "\nlandmark ids usable as pointer targets:\n" + self.landmarks.render() if found else ""
        return True, f"{header}\n{tree}{tail}"


class PointerTool(Tool):
    """Move, click, drag and scroll at exact screen coordinates or named landmarks."""

    name = "pointer"
    description = (
        "Control the mouse. action='click'|'double_click'|'right_click' at x,y or target=<landmark id "
        "from ui_tree>; action='move' to position; action='drag' from x,y to x2,y2; action='scroll' "
        "by dy (positive up) optionally at x,y. Coordinates are physical screen pixels. "
        "NON-INTRUSIVE: pass window=<title substring> (optionally delivery='background', the default "
        "when window= is set) to deliver the click into that window WITHOUT moving the user's cursor "
        "or stealing focus â€” works on background/occluded apps."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["move", "click", "double_click", "right_click", "middle_click", "drag", "scroll"],
            },
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "x2": {"type": "integer"},
            "y2": {"type": "integer"},
            "dy": {"type": "integer", "description": "scroll amount; positive scrolls up"},
            "target": {"type": "string", "description": "landmark id from ui_tree, e.g. 'save'"},
            "window": {"type": "string", "description": "target window title substring for non-intrusive background delivery"},
            "delivery": {"type": "string", "enum": ["background", "foreground"], "description": "default: background when window= is set"},
            "expect": {"type": "string", "description": "optional: after the click, verify this text appears (window title or on-screen); lets the harness confirm the action worked"},
        },
        "required": ["action"],
    }

    ACTIONS = ("move", "click", "double_click", "right_click", "middle_click", "drag", "scroll")
    BG_ACTIONS = ("click", "double_click", "right_click", "middle_click", "drag", "scroll")

    def __init__(self, landmarks: LandmarkStore | None = None, runner=run_ps) -> None:
        self.landmarks = landmarks or LandmarkStore()
        self._run_ps = runner

    def _resolve_xy(self, args: dict) -> tuple[int, int] | None:
        if args.get("target"):
            pt = self.landmarks.resolve(str(args["target"]))
            if pt is None:
                return None
            return int(pt["x"]), int(pt["y"])
        if args.get("x") is None or args.get("y") is None:
            return None
        return int(args["x"]), int(args["y"])

    def run(self, args: dict) -> tuple[bool, str]:
        action = args.get("action")
        if action not in self.ACTIONS:
            return False, f"unknown pointer action {action!r}; expected one of {', '.join(self.ACTIONS)}"
        if not sys.platform.startswith("win"):
            from saturday.tools import spatial_unix

            return spatial_unix.pointer_tool(self, args)
        window_q = str(args.get("window") or "").strip()
        delivery = (str(args.get("delivery") or ("background" if window_q else "foreground"))).lower()
        if window_q and delivery == "background":
            if action == "move":
                return False, "action='move' has no meaning in background delivery (no cursor is moved); use click/drag/scroll with window="
            if action not in self.BG_ACTIONS:
                return False, f"action {action!r} unsupported in background delivery"
            if not sys.platform.startswith("win"):
                return False, "pointer requires Windows"
            return self._run_background(action, window_q, args)
        if action == "scroll":
            xy = self._resolve_xy(args) if (args.get("x") is not None or args.get("target")) else None
            if args.get("x") is not None and xy is None:
                return False, "invalid scroll position"
        else:
            xy = self._resolve_xy(args)
            if xy is None:
                if args.get("target"):
                    known = self.landmarks.render(10)
                    return False, f"unknown target {args.get('target')!r}; run ui_tree first. Known:\n{known}"
                return False, f"action {action!r} needs x,y or target="
        if xy is not None:
            x, y = xy
            if not (0 <= x <= 100000 and 0 <= y <= 100000):
                return False, f"coordinates out of range: ({x},{y})"
        if not sys.platform.startswith("win"):
            return False, "pointer requires Windows"
        defines = (
            "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
            "public class Pointer{[DllImport(\"user32.dll\")]public static extern bool SetCursorPos(int X,int Y);"
            "[DllImport(\"user32.dll\")]public static extern void mouse_event(uint f,uint dx,uint dy,uint d,int e);"
            "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();}';"
        )
        script = defines + "[Pointer]::SetProcessDPIAware()|Out-Null;"
        if xy is not None:
            script += f"[Pointer]::SetCursorPos({x},{y})|Out-Null;"
        script += self.ps_action(args, *(xy if xy is not None else (0, 0)))
        rc, _, err = self._run_ps(script, timeout=20.0)
        if rc != 0:
            return False, f"pointer action failed: {(err or '').strip()[-300:]}"
        extra = f" -> ({int(args.get('x2') or 0)},{int(args.get('y2') or 0)})" if action == "drag" else ""
        at = f" at ({x},{y})" if xy is not None else ""
        vnote = verify_expect(self._run_ps, str(args.get("expect") or ""), str(args.get("window") or "")) if args.get("expect") else ""
        return True, f"{action}{at}{extra} ok{vnote}"

    def ps_action(self, args: dict, x: int, y: int) -> str:
        action = args.get("action")
        if action in ("click", "double_click", "right_click", "middle_click"):
            if action == "right_click":
                down, up = (8, 16)
            elif action == "middle_click":
                down, up = (0x20, 0x40)
            else:
                down, up = (2, 4)
            clicks = 2 if action == "double_click" else 1
            seq = ""
            for i in range(clicks):
                seq += f"[Pointer]::mouse_event({down},0,0,0,0);Start-Sleep -Milliseconds 25;[Pointer]::mouse_event({up},0,0,0,0)"
                if i == 0 and clicks > 1:
                    seq += ";Start-Sleep -Milliseconds 60;"
                elif i < clicks - 1:
                    seq += ";"
            return seq
        if action == "drag":
            x2 = int(args.get("x2") or x)
            y2 = int(args.get("y2") or y)
            seq = "[Pointer]::mouse_event(2,0,0,0,0)"
            steps = 12
            for i in range(1, steps + 1):
                xi = x + (x2 - x) * i // steps
                yi = y + (y2 - y) * i // steps
                seq += f";Start-Sleep -Milliseconds 15;[Pointer]::SetCursorPos({xi},{yi})|Out-Null"
            seq += ";Start-Sleep -Milliseconds 40;[Pointer]::mouse_event(4,0,0,0,0)"
            return seq
        if action == "scroll":
            dy = int(args.get("dy") or 0)
            delta = max(-30000, min(30000, dy * 120))
            return f"[Pointer]::mouse_event(2048,0,0,{delta},0)"
        return ""

    def _run_background(self, action: str, window_q: str, args: dict) -> tuple[bool, str]:
        win = resolve_window(window_q, self._run_ps)
        if win is None:
            return False, f"no visible window matching {window_q!r}; run window action=list to see titles"
        xy = self._resolve_xy(args)
        if action == "scroll" and xy is None:
            xy = (win["left"] + win["width"] // 2, win["top"] + win["height"] // 2)
        if xy is None:
            return False, f"action {action!r} needs x,y or target= (screen coordinates)"
        x, y = xy
        hwnd = win["hwnd"]
        posts = f"$t=[BgIn]::TargetAt([IntPtr]{hwnd},{x},{y},[ref]$lx,[ref]$ly);"
        # precomputed: PEP 701 nested-quote f-strings are 3.12+ only
        lxly = _pack_lparam("$lx", "$ly")
        if action == "scroll":
            dy = int(args.get("dy") or 0)
            delta = max(-30000, min(30000, dy * 120))
            posts += f"[BgIn]::Post($t,0x20A,[IntPtr](({delta} -shl 16) -band 0xFFFFFFFF),{_pack_lparam(x, y)})|Out-Null;"
        elif action == "drag":
            x2 = int(args.get("x2") or x)
            y2 = int(args.get("y2") or y)
            posts += f"$t=[BgIn]::TargetAt([IntPtr]{hwnd},{x},{y},[ref]$lx,[ref]$ly);"
            posts += f"[BgIn]::Post($t,0x201,[IntPtr]1,{lxly})|Out-Null;"
            steps = 12
            for i in range(1, steps + 1):
                xi = x + (x2 - x) * i // steps
                yi = y + (y2 - y) * i // steps
                posts += f"$t=[BgIn]::TargetAt([IntPtr]{hwnd},{xi},{yi},[ref]$lx,[ref]$ly);"
                posts += f"[BgIn]::Post($t,0x200,[IntPtr]1,{lxly})|Out-Null;"
            posts += f"[BgIn]::Post($t,0x202,[IntPtr]0,{lxly})|Out-Null;"
        else:
            if action == "right_click":
                down, up = (0x0204, 0x0205)  # WM_RBUTTON
            elif action == "middle_click":
                down, up = (0x0207, 0x0208)  # WM_MBUTTON
            else:
                down, up = (0x0201, 0x0202)  # WM_LBUTTON
            clicks = 2 if action == "double_click" else 1
            for i in range(clicks):
                if i == 1:
                    posts += f"$t=[BgIn]::TargetAt([IntPtr]{hwnd},{x},{y},[ref]$lx,[ref]$ly);"
                    posts += f"[BgIn]::Post($t,0x203,[IntPtr]1,{lxly})|Out-Null;"
                else:
                    posts += f"[BgIn]::Post($t,0x{down:X},[IntPtr]1,{lxly})|Out-Null;"
                posts += "Start-Sleep -Milliseconds 25;"
                posts += f"[BgIn]::Post($t,0x{up:X},[IntPtr]0,{lxly})|Out-Null;"
                if i < clicks - 1:
                    posts += "Start-Sleep -Milliseconds 60;"
        script = DPI_PREAMBLE + BG_INPUT_DEFINES + posts + "'ok'"
        rc, _, err = self._run_ps(script, timeout=25.0)
        if rc != 0:
            return False, f"background pointer failed: {(err or '').strip()[-300:]}"
        at = f" at ({x},{y})"
        vnote = verify_expect(self._run_ps, str(args.get("expect") or ""), win["title"]) if args.get("expect") else ""
        return True, f"{action}{at} delivered to '{win['title']}' (background: cursor/focus untouched) ok{vnote}"


GRID_CELL = 96


def build_grid_legend(width: int, height: int, cell: int = GRID_CELL) -> str:
    cols = (width + cell - 1) // cell
    rows = (height + cell - 1) // cell
    return (
        f"screen {width}x{height}px overlaid with a {cell}px grid: "
        f"{cols} columns (A..{cell_name(cols - 1, 0)[:-1]}) x {rows} rows (1..{rows}). "
        "Cells are labeled like A1, B3; each label sits top-left inside its cell."
    )


def ps_grid_overlay(out_path: Path, width: int, height: int, cell: int = GRID_CELL) -> str:
    q = str(out_path.as_posix()).replace("'", "''")
    parts = [
        DPI_PREAMBLE.rstrip(";"),
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing",
        "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds",
        "$bmp=New-Object Drawing.Bitmap $b.Width,$b.Height",
        "$g=[Drawing.Graphics]::FromImage($bmp)",
        "$g.CopyFromScreen(0,0,0,0,$bmp.Size)",
        "$pen=New-Object Drawing.Pen ([Drawing.Color]::FromArgb(140,255,0,0)),1",
        "$font=New-Object Drawing.Font 'Consolas',10",
        "$brush=[Drawing.Brushes]::Red",
    ]
    col = 0
    while col * cell <= width:
        parts.append(f"$g.DrawLine($pen,{col * cell},0,{col * cell},$b.Height)")
        col += 1
    row = 0
    while row * cell <= height:
        parts.append(f"$g.DrawLine($pen,0,{row * cell},$b.Width,{row * cell})")
        row += 1
    r = 0
    while r * cell < height:
        c = 0
        while c * cell < width:
            parts.append(
                f"$g.DrawString('{cell_name(c, r)}',$font,$brush,{c * cell + 3},{r * cell + 2})"
            )
            c += 1
        r += 1
    parts.append(f"$bmp.Save('{q}')")
    parts.append("$g.Dispose();$bmp.Dispose()")
    return ";".join(parts)


def ps_marked_overlay(out_path: Path, marks: list[dict]) -> str:
    q = str(out_path.as_posix()).replace("'", "''")
    parts = [
        DPI_PREAMBLE.rstrip(";"),
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing",
        "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds",
        "$bmp=New-Object Drawing.Bitmap $b.Width,$b.Height",
        "$g=[Drawing.Graphics]::FromImage($bmp)",
        "$g.CopyFromScreen(0,0,0,0,$bmp.Size)",
        "$pen=New-Object Drawing.Pen ([Drawing.Color]::FromArgb(220,255,0,0)),2",
        "$font=New-Object Drawing.Font 'Arial',14,[Drawing.FontStyle]::Bold",
        "$bg=[Drawing.Brushes]::Yellow",
        "$fg=[Drawing.Brushes]::Black",
    ]
    for m in marks:
        x, y, w, h = m["x"], m["y"], m["w"], m["h"]
        label = str(m["label"]).replace("'", "''")
        color = m.get("color", "255,0,0")
        parts.append(f"$pen.Color=[Drawing.Color]::FromArgb(220,{color})")
        parts.append(f"$g.DrawRectangle($pen,{x},{y},{w},{h})")
        parts.append(f"$g.FillRectangle($bg,{x},{max(0, y - 20)},28,20)")
        parts.append(f"$g.DrawString('{label}',$font,$fg,{x + 2},{max(0, y - 21)})")
    parts.append(f"$bmp.Save('{q}')")
    parts.append("$g.Dispose();$bmp.Dispose()")
    return ";".join(parts)


UI_INVOKE_ACTIONS = ("press", "toggle", "expand", "collapse", "select", "set_text", "scroll", "focus", "read")


def ps_ui_invoke_script(
    window_q: str, name: str, ctype: str, index: int, action: str, value: str,
    *, keep_focus: bool = False,
) -> str:
    wq = window_q.replace("'", "''").lower()
    nm = name.replace("'", "''")
    ct = ctype.replace("'", "''")
    val = value.replace("'", "''")
    if window_q:
        start = (
            "$cand=$null;"
            "[System.Windows.Automation.AutomationElement]::RootElement.FindAll("
            "[System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)"
            "| ForEach-Object { if(-not $cand -and $_.Current.Name -and $_.Current.Name.ToLower().Contains('" + wq + "')){$cand=$_} };"
            "$root=$cand"
        )
    else:
        start = "$root=[System.Windows.Automation.AutomationElement]::FocusedElement"
    guard_prefix = (
        "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
        "public class User32{[DllImport(\"user32.dll\")]public static extern IntPtr GetForegroundWindow();"
        "[DllImport(\"user32.dll\")]public static extern bool SetForegroundWindow(IntPtr h);"
        "[DllImport(\"user32.dll\")]public static extern bool ShowWindow(IntPtr h,int cmd);"
        "[DllImport(\"user32.dll\")]public static extern void keybd_event(byte vk,byte sc,uint f,int e);}';"
        "$prev=[User32]::GetForegroundWindow();"
        if keep_focus
        else ""
    )
    guard_suffix = (
        "Start-Sleep -Milliseconds 250;"
        "$now=[User32]::GetForegroundWindow();"
        "if($now -ne $prev -and $prev -ne [IntPtr]::Zero){"
        # restore focus WITHOUT the bare-Alt tap first: a synthetic Alt press
        # activates the menu bar of whatever currently has focus. Only when
        # SetForegroundWindow is refused (foreground lock) fall back to Alt.
        "if(-not [User32]::SetForegroundWindow($prev)){"
        "[User32]::keybd_event(18,0,0,0);[User32]::keybd_event(18,0,2,0);"
        "[User32]::SetForegroundWindow($prev)|Out-Null};"
        "[User32]::ShowWindow($prev,5)|Out-Null};"
        if keep_focus
        else ""
    )
    pattern_map = {
        "press": "[System.Windows.Automation.InvokePattern]::Pattern",
        "toggle": "[System.Windows.Automation.TogglePattern]::Pattern",
        "expand": "[System.Windows.Automation.ExpandCollapsePattern]::Pattern",
        "collapse": "[System.Windows.Automation.ExpandCollapsePattern]::Pattern",
        "select": "[System.Windows.Automation.SelectionItemPattern]::Pattern",
        "set_text": "[System.Windows.Automation.ValuePattern]::Pattern",
        "scroll": "[System.Windows.Automation.ScrollPattern]::Pattern",
        "focus": None,
        "read": "[System.Windows.Automation.ValuePattern]::Pattern",
    }
    pat = pattern_map[action]
    body = ""
    if pat:
        body = f"$p=$el.GetCurrentPattern({pat});"
    if action == "press":
        body += "$p.Invoke()"
    elif action == "toggle":
        body += "$p.Toggle()"
    elif action == "expand":
        body += "$p.Expand()"
    elif action == "collapse":
        body += "$p.Collapse()"
    elif action == "select":
        body += "$p.Select()"
    elif action == "set_text":
        body += f"$p.SetValue('{val}')"
    elif action == "read":
        body += "Write-Output ('VALUE '+$p.Value)"
    elif action == "scroll":
        body += "if($p.VerticallyScrollable){if([int]" + str(index or 0) + " -lt 0){$p.ScrollVerticalUp()}else{$p.ScrollVerticalDown()}}else{'no-v-scroll'}"
    elif action == "focus":
        body += "$el.SetFocus()"
    return (
        guard_prefix
        + DPI_PREAMBLE
        + "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes;"
        + start
        + ";if(-not $root){Write-Output 'ERR window not found';exit};"
        "$walker=[System.Windows.Automation.TreeWalker]::ControlViewWalker;"
        "$hits=New-Object System.Collections.Generic.List[object];"
        "function Walk($el,$depth){"
        "if($null -eq $el -or $depth -gt 10 -or $hits.Count -ge 60){return}"
        "$c=$el.Current;"
        "if($c.Name -and $c.Name.ToLower().Contains('" + nm.lower() + "')"
        + (f" -and $c.ControlType.ProgrammaticName -eq 'ControlType.{ct}'" if ct else "")
        + "){$r=$c.BoundingRectangle;$hits.Add([pscustomobject]@{e=$el;n=$c.Name;t=$c.ControlType.ProgrammaticName;x=[int]$r.X;y=[int]$r.Y;w=[int]$r.Width;h=[int]$r.Height})}"
        "$child=$walker.GetFirstChild($el);"
        "while($child){Walk $child ($depth+1);$child=$walker.GetNextSibling($child)}}"
        f"Walk $root 0;"
        "if($hits.Count -eq 0){Write-Output 'ERR element not found';exit}"
        f"$el=$hits[[Math]::Min({max(0, index)}, $hits.Count-1)].e;"
        "$i=$hits[[Math]::Min(" + str(max(0, index)) + ",$hits.Count-1)];"
        "Write-Output ('MATCH '+$i.n+' | '+$i.t+' | center='+[int]($i.x+$i.w/2)+','+[int]($i.y+$i.h/2));"
        "try{" + body + "}catch{Write-Output ('ERR pattern: '+$_.Exception.Message)};"
        + guard_suffix
    )


def verify_expect(runner, expect: str, window_q: str = "", attempts: int = 3) -> str:
    """Prediction-verify (world-model principle): after acting, confirm the
    expected change actually arrived - window title or on-screen element text.

    With ``window_q`` the scan targets THAT window (scope=win:<title>) instead
    of the foreground: the agent may run from a console whose foreground is
    the terminal itself, so verification must anchor to the window the action
    was delivered to."""
    e = (expect or "").strip()
    if not e:
        return ""
    low = e.lower()
    scopes = [f"win:{window_q}"] if window_q else ["foreground", "desktop"]
    for _ in range(max(1, attempts)):
        time.sleep(0.5)
        try:
            rc, out, _ = runner(WINDOW_LIST_SCRIPT, timeout=15.0)
            if rc == 0 and low in (out or "").lower():
                return f" [verify: expect {e!r} observed in window title]"
            for scope in scopes:
                rc2, out2, _ = runner(ps_scan_script(scope), timeout=20.0)
                if rc2 == 0 and low in (out2 or "").lower():
                    return f" [verify: expect {e!r} observed on screen]"
        except Exception:
            continue
    return f" [verify: expect {e!r} NOT observed after {attempts} attempts - re-scan or check visually]"


class UiInvokeTool(Tool):
    """Act on UI elements directly via accessibility patterns â€” no mouse, no focus steal.

    Works on BACKGROUND windows: the target app never needs to be focused and the
    user's cursor/keyboard are untouched. This is what makes unattended computer
    use possible while a human keeps working."""

    name = "ui_invoke"
    description = (
        "Interact with a UI element WITHOUT moving the mouse or stealing focus (works on "
        "background windows). Find by element name substring (+ optional control_type and "
        "index for duplicates) inside window=<title substring> (omit window for foreground app). "
        "actions: press (buttons), set_text (type into edit fields), toggle, expand, collapse, "
        "select, scroll, focus. Use ui_tree first to discover names/types."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(UI_INVOKE_ACTIONS)},
            "name": {"type": "string", "description": "element name substring, e.g. 'Save'"},
            "window": {"type": "string", "description": "target window title substring; default foreground app"},
            "control_type": {"type": "string", "description": "e.g. Button, Edit, MenuItem, TabItem"},
            "index": {"type": "integer", "description": "0-based match index when several elements share the name"},
            "value": {"type": "string", "description": "for set_text"},
            "expect": {"type": "string", "description": "optional post-action verification: after acting, confirm this text appears (window title or on-screen elements)"},
            "wait_seconds": {
                "type": "number",
                "description": "poll until the element appears (use after launching an app); default 0",
            },
        },
        "required": ["action", "name"],
    }

    def __init__(self, runner=run_ps, restore_focus_after: bool = False) -> None:
        self._run_ps = runner
        self.restore_focus_after = restore_focus_after

    def run(self, args: dict) -> tuple[bool, str]:
        if not sys.platform.startswith("win"):
            from saturday.tools import spatial_unix

            return spatial_unix.ui_invoke_tool(self, args)
        action = args.get("action")
        if action not in UI_INVOKE_ACTIONS:
            return False, f"unknown ui_invoke action {action!r}"
        if not sys.platform.startswith("win"):
            return False, "ui_invoke requires Windows (UI Automation)"
        name = str(args.get("name") or "")
        if not name:
            return False, "name= is required (substring of the element's accessible name)"
        window_q = str(args.get("window") or "")
        ctype = str(args.get("control_type") or "")
        index = int(args.get("index") or 0)
        value = str(args.get("value") or "") if action == "set_text" else ""
        keep_focus = bool(self.restore_focus_after and window_q)
        try:
            wait = float(args.get("wait_seconds") or 0)
        except (TypeError, ValueError):
            wait = 0.0
        attempts = max(1, min(12, int(wait * 2) + 1))
        script = ps_ui_invoke_script(window_q, name, ctype, index, action, value, keep_focus=keep_focus)
        last = ""
        expect = str(args.get("expect") or "")
        for i in range(attempts):
            rc, out, err = self._run_ps(script, timeout=30.0)
            text = (out or "").strip()
            if text.startswith("VALUE"):
                vnote = verify_expect(self._run_ps, expect, window_q) if expect and i == 0 else ""
                return True, text + vnote
            if rc != 0 and not text:
                return False, f"ui_invoke failed: {(err or '').strip()[-400:]}"
            if not text.startswith("ERR"):
                vnote = verify_expect(self._run_ps, expect, window_q) if expect and i == 0 else ""
                return True, text + vnote
            last = text
            if attempts > 1:
                time.sleep(0.5)
        return False, last


def ps_app_open_script(target: str, arglist: str, show_cmd: int, restore_focus: bool) -> str:
    t = target.replace("'", "''")
    a = arglist.replace("'", "''")
    cmdline = t + (" " + a if a else "")
    cmdline_ps = cmdline.replace("'", "''")
    restore = "$prev=[User32]::GetForegroundWindow();" if restore_focus else ""
    tail = (
        "$deadline=(Get-Date).AddSeconds(4);"
        "$stolen=$false;"
        "do{"
        "Start-Sleep -Milliseconds 600;"
        "$now=[User32]::GetForegroundWindow();"
        "if($now -ne $prev -and $prev -ne [IntPtr]::Zero){"
        # SetForegroundWindow first; the bare-Alt unlock (which opens menu bars
        # in the focused app) only fires when the direct restore is refused.
        "if(-not [User32]::SetForegroundWindow($prev)){"
        "[User32]::keybd_event(18,0,0,0);[User32]::keybd_event(18,0,2,0);"
        "[User32]::SetForegroundWindow($prev)|Out-Null};"
        "[User32]::ShowWindow($prev,5)|Out-Null;"
        "$stolen=$true}"
        "}while((Get-Date) -lt $deadline);"
        "if($stolen){Write-Output 'focus-restored'}else{Write-Output 'focus-untouched'};"
        if restore_focus
        else ""
    )
    return (
        DPI_PREAMBLE
        + "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
        "public class User32{[DllImport(\"user32.dll\")]public static extern IntPtr GetForegroundWindow();"
        "[DllImport(\"user32.dll\")]public static extern bool SetForegroundWindow(IntPtr h);"
        "[DllImport(\"user32.dll\")]public static extern bool ShowWindow(IntPtr h,int cmd);"
        "[DllImport(\"user32.dll\")]public static extern void keybd_event(byte vk,byte sc,uint f,int e);"
        "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();}';"
        "Add-Type -TypeDefinition 'using System;using System.Text;using System.Runtime.InteropServices;"
        "public class CP{"
        "[StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]public struct STARTUPINFO{"
        "public int cb;public string lpReserved;public string lpDesktop;public string lpTitle;"
        "public int dwX,dwY,dwXSize,dwYSize,dwXCountChars,dwYCountChars,dwFillAttribute,dwFlags;"
        "public short wShowWindow,cbReserved2;public IntPtr lpReserved2,hStdInput,hStdOutput,hStdError;};"
        "[StructLayout(LayoutKind.Sequential)]public struct PROCESS_INFORMATION{"
        "public IntPtr hProcess,hThread;public int dwProcessId,dwThreadId;};"
        "[DllImport(\"kernel32.dll\",CharSet=CharSet.Unicode,SetLastError=true)]"
        "public static extern bool CreateProcessW(IntPtr app,StringBuilder cmd,IntPtr pa,IntPtr ta,bool inh,"
        "int flags,IntPtr env,IntPtr dir,ref STARTUPINFO si,out PROCESS_INFORMATION pi);"
        "[DllImport(\"kernel32.dll\")]public static extern bool CloseHandle(IntPtr h);}';"
        ";[User32]::SetProcessDPIAware()|Out-Null;"
        + restore
        + "$si=New-Object CP+STARTUPINFO;$si.cb=[System.Runtime.InteropServices.Marshal]::SizeOf($si);"
        f"$si.dwFlags=1;$si.wShowWindow={show_cmd};"
        "$pi=New-Object CP+PROCESS_INFORMATION;"
        "$sb=New-Object System.Text.StringBuilder('" + cmdline_ps + "',2048);"
        "$ok=[CP]::CreateProcessW([IntPtr]::Zero,$sb,[IntPtr]::Zero,[IntPtr]::Zero,$false,0,[IntPtr]::Zero,[IntPtr]::Zero,[ref]$si,[ref]$pi);"
        "if(-not $ok){Write-Output ('ERR launch failed: '+[System.Runtime.InteropServices.Marshal]::GetLastWin32Error());exit};"
        "[CP]::CloseHandle($pi.hThread)|Out-Null;[CP]::CloseHandle($pi.hProcess)|Out-Null;"
        "Write-Output ('PID '+$pi.dwProcessId);"
        + tail
    )


class AppOpenTool(Tool):
    """Launch apps WITHOUT stealing the user's focus: minimized, never activated,
    and the previous foreground window is restored if the OS forces focus anyway."""

    name = "app_open"
    description = (
        "Open an application or document in the background: starts minimized WITHOUT "
        "activation, and if the OS tries to force-focus it anyway the user's previous "
        "window is restored automatically. Designed for background computer-use so the "
        "human is never interrupted. mode='normal' skips the protection."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "app name or path, e.g. notepad, calc, mspaint, C:\\path\\app.exe"},
            "args": {"type": "string", "description": "optional arguments"},
            "mode": {
                "type": "string",
                "enum": ["background", "normal"],
                "description": "background (default): minimized + never activated + focus restoration. normal: regular launch",
            },
        },
        "required": ["target"],
    }

    def __init__(self, runner=run_ps) -> None:
        self._run_ps = runner

    def run(self, args: dict) -> tuple[bool, str]:
        target = str(args.get("target") or "").strip()
        if not target:
            return False, "target= is required"
        if not sys.platform.startswith("win"):
            from saturday.tools import spatial_unix

            return spatial_unix.app_open_tool(self, args)
        mode = args.get("mode") if args.get("mode") in ("background", "normal") else "background"
        show_cmd = {"background": 7, "normal": 1}[mode]  # 7 = SW_SHOWMINNOACTIVE, 1 = SW_SHOWNORMAL
        restore = mode == "background"
        script = ps_app_open_script(target, str(args.get("args") or ""), show_cmd, restore)
        rc, out, err = self._run_ps(script, timeout=20.0)
        text = (out or "").strip()
        if text.startswith("ERR"):
            return False, text
        if rc != 0:
            return False, f"app_open failed: {(err or '').strip()[-300:]}"
        pid = ""
        focus_note = ""
        for token in text.splitlines():
            if token.startswith("PID "):
                pid = token[4:].strip()
            elif token == "focus-restored":
                focus_note = " (OS forced focus; user's window restored)"
            elif token == "focus-untouched":
                focus_note = " (user focus untouched)"
        return True, f"launched {target!r} in {'background' if mode == 'background' else 'normal'} mode{(' pid=' + pid) if pid else ''}{focus_note}"


def ps_capture_window_script(query: str, out_path: Path) -> str:
    q = query.replace("'", "''").lower()
    q2 = str(out_path.as_posix()).replace("'", "''")
    return (
        DPI_PREAMBLE
        + "Add-Type -AssemblyName System.Drawing;"
        "Add-Type -TypeDefinition 'using System;using System.Text;using System.Runtime.InteropServices;"
        "public class WCap{public delegate bool CB(IntPtr h,IntPtr l);"
        "[DllImport(\"user32.dll\")]public static extern bool EnumWindows(CB cb,IntPtr l);"
        "[DllImport(\"user32.dll\")]public static extern bool IsWindowVisible(IntPtr h);"
        "[DllImport(\"user32.dll\",CharSet=CharSet.Unicode)]public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);"
        "[DllImport(\"user32.dll\")]public static extern bool PrintWindow(IntPtr h,System.IntPtr hdc,uint f);"
        "[DllImport(\"user32.dll\")]public static extern bool GetWindowRect(IntPtr h,out RECT r);"
        "[DllImport(\"user32.dll\")]public static extern bool ShowWindow(IntPtr h,int cmd);"
        "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();"
        "public struct RECT{public int L,T,R,B;}}';"
        "[WCap]::SetProcessDPIAware()|Out-Null;"
        "$cands=New-Object System.Collections.Generic.List[string];"
        "$sb=New-Object System.Text.StringBuilder 512;"
        "$cb=[WCap+CB]{param($h,$l)"
        "if([WCap]::IsWindowVisible($h)){"
        "[WCap]::GetWindowText($h,$sb,512)|Out-Null;$t=$sb.ToString();$sb.Clear()|Out-Null;"
        "if($t -and $t.ToLower().Contains('" + q + "')){$cands.Add($h.ToInt64().ToString()+'|'+$t)}};$true};"
        "[WCap]::EnumWindows($cb,[IntPtr]::Zero)|Out-Null;"
        "if($cands.Count -eq 0){Write-Output 'ERR window not found';exit};"
        "$parts=$cands[0].Split('|',2);"
        "$found=[IntPtr][long]$parts[0];"
        "$r=New-Object WCap+RECT;[WCap]::GetWindowRect($found,[ref]$r)|Out-Null;"
        "$wasMin=$r.L -le -30000;"
        "$w=$r.R-$r.L;$h=$r.B-$r.T;"
        "if($wasMin -or $w -le 0 -or $h -le 0){"
        "[WCap]::ShowWindow($found,4)|Out-Null;Start-Sleep -Milliseconds 450;"
        "[WCap]::GetWindowRect($found,[ref]$r)|Out-Null;$w=$r.R-$r.L;$h=$r.B-$r.T};"
        "if($w -le 0 -or $h -le 0){Write-Output 'ERR empty rect';exit};"
        "$bmp=New-Object Drawing.Bitmap $w,$h;"
        "$g=[Drawing.Graphics]::FromImage($bmp);"
        "$hdc=$g.GetHdc();"
        # flag 2 = PW_RENDERFULLCONTENT only. The bitmap is GetWindowRect-sized;
        # PW_CLIENTONLY(1) would paint client content at the origin of that
        # full-window bitmap, leaving black bands where the chrome should be.
        f"$ok=[WCap]::PrintWindow($found,$hdc,2);"
        "$g.ReleaseHdc($hdc);"
        "if($wasMin){[WCap]::ShowWindow($found,6)|Out-Null};"
        "if(-not $ok){$g.Dispose();$bmp.Dispose();Write-Output 'ERR PrintWindow refused';exit};"
        f"$bmp.Save('{q2}');"
        "$g.Dispose();$bmp.Dispose();"
        "Write-Output ('OK '+$w+'x'+$h)"
    )


def capture_window_bg(query: str, out: Path, runner=run_ps) -> tuple[bool, str]:
    rc, out_s, err = runner(ps_capture_window_script(query, out), timeout=25.0)
    text = (out_s or "").strip()
    if text.startswith("ERR"):
        return False, text
    if rc != 0:
        return False, f"capture_window failed: {(err or '').strip()[-300:]}"
    return True, text


MARK_COLORS = ["255,0,0", "0,160,255", "0,180,0", "255,140,0", "170,0,255"]


KEY_VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "space": 0x20, "win": 0x5B, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "shift": 0x10, "apps": 0x5D, "capslock": 0x14, "printscreen": 0x2C,
}
for _i in range(1, 13):
    KEY_VK[f"f{_i}"] = 0x6F + _i


def parse_combo(spec: str) -> list[tuple[int, bool]]:
    """'ctrl+shift+s' -> [(vk, down?), ...] with modifiers pressed first, released last."""
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    mods: list[int] = []
    main: int | None = None
    for p in parts:
        if p in ("ctrl", "control", "alt", "shift", "win"):
            mods.append(KEY_VK[p])
        elif p in KEY_VK:
            main = KEY_VK[p]
        elif len(p) == 1:
            ch = p.upper()
            main = ord(ch)
        else:
            raise ValueError(f"unknown key {p!r}")
    if main is None and mods:
        main = mods.pop()
    if main is None:
        raise ValueError(f"no main key in {spec!r}")
    seq = [(vk, True) for vk in mods] + [(main, True), (main, False)] + [(vk, False) for vk in reversed(mods)]
    return seq


def ps_send_input_defines() -> str:
    return (
        "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
        "public class Kb{[DllImport(\"user32.dll\")]public static extern uint SendInput(uint n,INPUT[] i,int s);"
        "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();"
        "[StructLayout(LayoutKind.Sequential)]public struct MOUSEINPUT{public int dx,dy;public uint mouseData,flags,time;public IntPtr extra;};"
        "[StructLayout(LayoutKind.Sequential)]public struct KEYBDINPUT{public ushort wVk,wScan;public uint flags,time;public IntPtr extra;};"
        "[StructLayout(LayoutKind.Explicit)]public struct IU{[FieldOffset(0)]public MOUSEINPUT m;[FieldOffset(0)]public KEYBDINPUT k;};"
        "[StructLayout(LayoutKind.Sequential)]public struct INPUT{public uint type;public IU u;};"
        "public static void Key(ushort vk,bool down){INPUT[] x=new INPUT[1];x[0].type=1;x[0].u.k.wVk=vk;"
        "x[0].u.k.wScan=0;x[0].u.k.flags=down?(uint)0:2;x[0].u.k.time=0;x[0].u.k.extra=IntPtr.Zero;"
        "SendInput(1,x,Marshal.SizeOf(typeof(INPUT)));}"
        "public static void Char(char c){INPUT[] x=new INPUT[1];x[0].type=1;x[0].u.k.wVk=0;x[0].u.k.wScan=(ushort)c;"
        "x[0].u.k.flags=4;x[0].u.k.time=0;x[0].u.k.extra=IntPtr.Zero;SendInput(1,x,Marshal.SizeOf(typeof(INPUT)));"
        "x[0].u.k.flags=4|2;SendInput(1,x,Marshal.SizeOf(typeof(INPUT)));}}';"
    )


class KeyboardTool(Tool):
    """Type text and press keys/combos into the focused window."""

    name = "keyboard"
    description = (
        "Send keyboard input to the focused window. action='type' with text (Unicode-safe, "
        "newlines become Enter); action='key' with a key or combo like Enter, Tab, Escape, "
        "Ctrl+S, Alt+F4, Win, Shift+Tab, F5. Focus the right window first (window tool). "
        "NON-INTRUSIVE: pass window=<title substring> to type into a BACKGROUND window via "
        "window messages — the user's keyboard/focus is never touched (plain text and single "
        "keys work; modifier combos may be ignored by some apps)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["type", "key"]},
            "text": {"type": "string", "description": "for action=type"},
            "key": {"type": "string", "description": "for action=key, e.g. 'Ctrl+S', 'Enter', 'Alt+F4'"},
            "window": {"type": "string", "description": "target window title substring for non-intrusive background delivery"},
            "delivery": {"type": "string", "enum": ["background", "foreground"], "description": "default: background when window= is set"},
        },
        "required": ["action"],
    }

    def __init__(self, runner=None) -> None:
        if runner is None:
            if sys.platform.startswith("win"):
                runner = run_ps
            else:
                # macOS/Linux: run_ps shells to powershell, which doesn't
                # exist here. The non-Windows type action is chunked through
                # this same injection point (see spatial_unix.keyboard_tool)
                # so tests can mock it without a real xdotool on PATH.
                from saturday.tools.spatial_unix import xdotool_type_chunk

                runner = xdotool_type_chunk
        self._run_ps = runner

    def run(self, args: dict) -> tuple[bool, str]:
        action = args.get("action")
        if action not in ("type", "key"):
            return False, f"unknown keyboard action {action!r}"
        if not sys.platform.startswith("win"):
            from saturday.tools import spatial_unix

            return spatial_unix.keyboard_tool(self, args)
        window_q = str(args.get("window") or "").strip()
        delivery = (str(args.get("delivery") or ("background" if window_q else "foreground"))).lower()
        if window_q and delivery == "background":
            return self._run_background(action, window_q, args)
        try:
            if action == "type":
                text = str(args.get("text") or "")
                if not text:
                    return False, "action=type needs text="
                seq: list[str] = []
                for ch in text[:4000]:
                    if ch in "\r\n":
                        seq.append("[Kb]::Key(13,$true);[Kb]::Key(13,$false)")
                    else:
                        seq.append(f"[Kb]::Char([char]{ord(ch)})")
                detail = f"typed {min(len(text), 4000)} chars"
                statements = seq
            else:
                spec = str(args.get("key") or "")
                events = parse_combo(spec)
                statements = [f"[Kb]::Key({vk},{'$true' if down else '$false'})" for vk, down in events]
                detail = f"pressed {spec}"
        except ValueError as exc:
            return False, str(exc)
        defines = ps_send_input_defines() + "[Kb]::SetProcessDPIAware()|Out-Null;"
        chunk_statements = 300
        total = len(statements)
        sent = 0
        for start_idx in range(0, total, chunk_statements):
            body = ";".join(statements[start_idx : start_idx + chunk_statements])
            script = defines + body
            rc, _, err = self._run_ps(script, timeout=20.0)
            if rc != 0:
                return False, f"keyboard failed at char {sent}: {(err or '').strip()[-300:]}"
            sent += min(chunk_statements, total - start_idx)
        time.sleep(0.05)
        return True, f"{detail} ok"

    def _run_background(self, action: str, window_q: str, args: dict) -> tuple[bool, str]:
        win = resolve_window(window_q, self._run_ps)
        if win is None:
            return False, f"no visible window matching {window_q!r}; run window action=list to see titles"
        if action == "type":
            text = str(args.get("text") or "")
            if not text:
                return False, "action=type needs text="
            statements = []
            for ch in text[:4000]:
                if ch in "\r\n":
                    statements.append("[BgIn]::Post($e,0x102,[IntPtr]13,[IntPtr]0)|Out-Null;")
                else:
                    statements.append(f"[BgIn]::Post($e,0x102,[IntPtr]{ord(ch)},[IntPtr]0)|Out-Null;")
            detail = f"typed {min(len(text), 4000)} chars into '{win['title']}'"
        else:
            spec = str(args.get("key") or "")
            try:
                events = parse_combo(spec)
            except ValueError as exc:
                return False, str(exc)
            statements = []
            for vk, down in events:
                statements.append(f"[BgIn]::Post($e,0x{0x100 if down else 0x101:X},[IntPtr]{vk},[IntPtr]0)|Out-Null;")
            detail = f"posted {spec} to '{win['title']}'"
        hwnd = win["hwnd"]
        defines = DPI_PREAMBLE + BG_INPUT_DEFINES + f"$e=[BgIn]::EditChild([IntPtr]{hwnd});if($e -eq [IntPtr]::Zero){{$e=[IntPtr]{hwnd};}};"
        chunk_statements = 300
        total = len(statements)
        sent = 0
        if total == 0:
            return False, "nothing to send"
        for start_idx in range(0, total, chunk_statements):
            body = ";".join(statements[start_idx : start_idx + chunk_statements])
            script = defines + body + ";'ok'"
            rc, _, err = self._run_ps(script, timeout=20.0)
            if rc != 0:
                return False, f"background keyboard failed at char {sent}: {(err or '').strip()[-300:]}"
            sent += min(chunk_statements, total - start_idx)
        return True, f"{detail} (background: user's keyboard/focus untouched) ok"


class WindowTool(Tool):
    """List, focus, minimize and maximize top-level windows by title substring."""

    name = "window"
    description = (
        "Manage windows: action='list' shows visible top-level windows with titles and rects; "
        "action='focus'|'minimize'|'maximize' with query=<title substring> (case-insensitive); "
        "action='restore' shows a minimized window WITHOUT activating it (non-intrusive, use "
        "before ui_tree/ui_invoke on background apps); action='close' sends a graceful close "
        "request (WM_CLOSE, non-intrusive — the app may show an unsaved-changes dialog). "
        "Focus before typing or clicking into an app."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "focus", "minimize", "maximize", "restore", "close"]},
            "query": {"type": "string", "description": "title substring for focus/minimize/maximize/restore/close"},
        },
        "required": ["action"],
    }

    def __init__(self, runner=run_ps) -> None:
        self._run_ps = runner

    @staticmethod
    def pick(titles: list[str], query: str) -> str | None:
        q = (query or "").strip().lower()
        exact = [t for t in titles if t.lower() == q]
        if exact:
            return exact[0]
        starts = [t for t in titles if t.lower().startswith(q)]
        if starts:
            return sorted(starts)[0]
        contains = [t for t in titles if q in t.lower()]
        return sorted(contains)[0] if contains else None

    def run(self, args: dict) -> tuple[bool, str]:
        action = args.get("action")
        if action not in ("list", "focus", "minimize", "maximize", "restore", "close"):
            return False, f"unknown window action {action!r}"
        if not sys.platform.startswith("win"):
            from saturday.tools import spatial_unix

            return spatial_unix.window_tool(self, args)
        _, out, _ = self._run_ps(WINDOW_LIST_SCRIPT)
        rows = [line.split("|", 2) for line in (out or "").splitlines() if "|" in line]
        titles = [r[1] for r in rows if len(r) == 3]
        if action == "list":
            lines = [f"hwnd={r[0]} title={r[1]!r} rect=({r[2]})" for r in rows if len(r) == 3][:40]
            return True, "\n".join(lines) or "(no visible windows)"
        query = str(args.get("query") or "")
        chosen = self.pick(titles, query)
        if chosen is None:
            sample = "; ".join(titles[:10])
            return False, f"no window matching {query!r}. Visible: {sample}"
        row = next(r for r in rows if len(r) == 3 and r[1] == chosen)
        hwnd = row[0]
        if action == "close":
            close_script = (
                DPI_PREAMBLE
                + "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
                "public class CloseWin{[DllImport(\"user32.dll\")]public static extern bool PostMessage(IntPtr h,uint m,IntPtr w,IntPtr l);"
                "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();}';"
                "[CloseWin]::SetProcessDPIAware()|Out-Null;"
                f"[CloseWin]::PostMessage([IntPtr]{hwnd},0x0010,[IntPtr]0,[IntPtr]0)|Out-Null;'ok'"
            )
            rc2, _, err2 = self._run_ps(close_script)
            if rc2 != 0:
                return False, f"close failed: {(err2 or '').strip()[-300:]}"
            return (
                True,
                f"sent close request to {chosen!r} (graceful; if it stays open, an "
                "unsaved-changes dialog is showing — ui_invoke 'press' on it)",
            )
        sw = {"minimize": 6, "maximize": 3, "focus": 9, "restore": 4}[action]
        if action == "focus":
            act_script = (
                DPI_PREAMBLE
                + "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
                "public class WinAct{[DllImport(\"user32.dll\")]public static extern IntPtr FindWindow(string c,string t);"
                "[DllImport(\"user32.dll\")]public static extern bool SetForegroundWindow(IntPtr h);"
                "[DllImport(\"user32.dll\")]public static extern bool ShowWindowAsync(IntPtr h,int cmd);"
                "[DllImport(\"user32.dll\")]public static extern void keybd_event(byte vk,byte sc,uint f,int e);"
                "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();}';"
                "[WinAct]::SetProcessDPIAware()|Out-Null;"
                f"$h=[IntPtr]{hwnd};"
                f"[WinAct]::ShowWindowAsync($h,{sw})|Out-Null;Start-Sleep -Milliseconds 120;"
                # no preemptive bare-Alt tap: it opens the menu bar of whatever
                # currently has focus. Try the plain foreground set first and
                # use the Alt unlock only when Windows refuses it.
                "$fg=[WinAct]::SetForegroundWindow($h);"
                "if(-not $fg){"
                "[WinAct]::keybd_event(18,0,0,0);[WinAct]::keybd_event(18,0,2,0);"
                "[WinAct]::SetForegroundWindow($h)|Out-Null};'ok'"
            )
            rc2, _, err2 = self._run_ps(act_script)
            if rc2 != 0:
                return False, f"focus failed: {(err2 or '').strip()[-300:]}"
        else:
            show_only = (
                DPI_PREAMBLE
                + "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
                "public class W2{[DllImport(\"user32.dll\")]public static extern bool ShowWindowAsync(IntPtr h,int c);"
                "[DllImport(\"user32.dll\")]public static extern bool SetProcessDPIAware();}';"
                "[W2]::SetProcessDPIAware()|Out-Null;"
                f"[W2]::ShowWindowAsync([IntPtr]{hwnd},{sw})|Out-Null;'ok'"
            )
            rc2, _, err2 = self._run_ps(show_only)
            if rc2 != 0:
                return False, f"{action} failed: {(err2 or '').strip()[-300:]}"
        extra_note = " (no activation)" if action == "restore" else ""
        return True, f"{action} {chosen!r} ok{extra_note}"


class ClipboardTool(Tool):
    """Read or write the Windows clipboard as text."""

    name = "clipboard"
    description = (
        "Read/write clipboard text. action='get' returns current clipboard content (truncated); "
        "action='set' with text= replaces it. Useful for moving large text into apps: set clipboard, "
        "then keyboard Ctrl+V."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set"]},
            "text": {"type": "string"},
        },
        "required": ["action"],
    }

    MAX_GET_CHARS = 4000

    def __init__(self, runner=run_ps) -> None:
        self._run_ps = runner

    def run(self, args: dict) -> tuple[bool, str]:
        action = args.get("action")
        if action not in ("get", "set"):
            return False, f"unknown clipboard action {action!r}"
        if not sys.platform.startswith("win"):
            from saturday.tools import spatial_unix

            return spatial_unix.clipboard_tool(self, args)
        if action == "get":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$t=[System.Windows.Forms.Clipboard]::GetText();"
                "if($t.Length -gt " + str(self.MAX_GET_CHARS) + "){$t.Substring(0," + str(self.MAX_GET_CHARS) + ")+'...[truncated]'}else{$t}"
            )
            rc, out, err = self._run_ps(script)
            if rc != 0:
                return False, f"clipboard get failed: {(err or '').strip()[-300:]}"
            content = (out or "").rstrip("\r\n")
            return True, content if content else "(clipboard empty)"
        text = str(args.get("text") or "")
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "[System.Windows.Forms.Clipboard]::SetText("
            "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + b64 + "')))"
        )
        rc, _, err = self._run_ps(script)
        if rc != 0:
            return False, f"clipboard set failed: {(err or '').strip()[-300:]}"
        preview = text[:60].replace("\n", "\\n")
        return True, f"clipboard set ({len(text)} chars): {preview!r}"


def collect_marks(elements: list[dict], store: LandmarkStore | None = None) -> list[dict]:
    marks: list[dict] = []
    idx = 1
    for e in elements:
        if e.get("off"):
            continue
        role = str(e.get("t") or "").replace("ControlType.", "")
        if role not in INTERACTIVE_TYPES:
            continue
        w, h = int(e.get("w") or 0), int(e.get("h") or 0)
        if w <= 0 or h <= 0:
            continue
        label = str(idx % 10)
        key = None
        if store is not None and e.get("n"):
            key = store.add(str(e["n"]), int(e.get("x") or 0) + w // 2, int(e.get("y") or 0) + h // 2, role)
        marks.append(
            {
                "x": int(e.get("x") or 0),
                "y": int(e.get("y") or 0),
                "w": min(w, 400),
                "h": min(h, 200),
                "label": label,
                "color": MARK_COLORS[idx % len(MARK_COLORS)],
                "key": key or "",
                "name": str(e.get("n") or ""),
                "role": role,
            }
        )
        idx += 1
        if len(marks) >= 40:
            break
    return marks


def marked_legend(marks: list[dict]) -> str:
    lines = ["interactive elements are boxed and numbered (digit drawn on its top-left corner):"]
    for m in marks:
        nm = f" {m['name']!r}" if m["name"] else ""
        kid = f" [landmark:{m['key']}]" if m.get("key") else ""
        lines.append(f"box {m['label']}: {m['role']}{nm} center=({m['x'] + m['w'] // 2},{m['y'] + m['h'] // 2}){kid}")
    return "\n".join(lines)


def capture_annotated(out: Path, annotate: str, runner=run_ps, landmarks: LandmarkStore | None = None) -> tuple[bool, str]:
    """Windows path: capture + overlay in one shot. Returns (ok, legend_or_error)."""
    if annotate == "marked":
        rc, sout, serr = runner(ps_scan_script("foreground"))
        elements = parse_scan(sout) if rc == 0 else []
        if not elements:
            return False, f"marked capture failed: ui scan empty ({serr.strip()[-200:]})"
        marks = collect_marks(elements, landmarks)
        rc2, _, err2 = runner(ps_marked_overlay(out, marks), timeout=30.0)
        if rc2 != 0:
            return False, f"marked capture failed: {(err2 or '').strip()[-300:]}"
        return True, marked_legend(marks)
    rc, b_w, _ = runner(
        DPI_PREAMBLE
        + "Add-Type -AssemblyName System.Windows.Forms;"
        + "[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width.ToString()+','+[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height.ToString()"
    )
    width, height = (b_w.strip().split(",") + ["1920", "1080"])[:2] if rc == 0 else ("1920", "1080")
    try:
        width_i, height_i = int(width), int(height)
    except ValueError:
        width_i, height_i = 1920, 1080
    rc3, _, err3 = runner(ps_grid_overlay(out, width_i, height_i), timeout=30.0)
    if rc3 != 0:
        return False, f"grid capture failed: {(err3 or '').strip()[-300:]}"
    return True, build_grid_legend(width_i, height_i)
