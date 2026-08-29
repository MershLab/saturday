from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from saturday.tools.base import Tool


class ScreenTool(Tool):
    """Computer-use lite: capture the screen and attach it for vision inspection."""

    name = "screen"
    description = (
        "Take a screenshot of the current screen and attach it to the next observation "
        "so you can see what the user sees. annotate='grid' overlays a labeled coordinate "
        "grid (A1, B3, ...) so you can reason about positions; annotate='marked' (Windows) "
        "boxes every interactive UI element, numbers it, and lists each box's center "
        "coordinates plus a pointer target id. Windows uses built-in capture; other "
        "platforms need 'pip install pillow'. Multi-monitor: display=N captures a specific "
        "monitor (1 = primary); pointer coordinates are virtual-desktop coordinates."
    )
    parameters = {
        "type": "object",
        "properties": {
            "monitor_note": {"type": "string", "description": "optional context, e.g. 'left monitor'"},
            "annotate": {
                "type": "string",
                "enum": ["none", "grid", "marked"],
                "description": "overlay to bake into the capture; default none",
            },
            "capture_window": {
                "type": "string",
                "description": "capture ONLY this window (title substring) instead of the whole screen; works while the window is in the background/occluded; non-intrusive",
            },
            "display": {
                "type": "integer",
                "description": "1-based monitor index to capture (1 = primary); omit for the primary monitor",
            },
        },
        "required": [],
    }

    def __init__(self, shots_dir: str | Path | None = None, landmarks=None, cache=None) -> None:
        self.shots_dir = Path(shots_dir) if shots_dir else None
        self.pending_images: list[str] = []
        self.landmarks = landmarks
        self.cache = cache

    def _shot_via_pillow(self, out: Path) -> bool:
        try:
            from PIL import ImageGrab
        except ImportError:
            return False
        if sys.platform.startswith("win"):
            # best-effort DPI awareness, same as spatial.py: without it
            # ImageGrab returns a scaled/blurred bitmap on high-DPI displays
            try:
                import ctypes

                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        img = ImageGrab.grab()
        img.save(out)
        return True

    def _shot_via_powershell(self, out: Path, display: int = 0) -> tuple[bool, str]:
        from saturday.tools.spatial import DPI_PREAMBLE

        # single quotes in the path must be doubled or PS breaks the string
        q = out.as_posix().replace("'", "''")
        # display: 0 = primary (legacy behavior); N = Nth AllScreens entry
        # (1-based, multi-monitor). Bounds are virtual-desktop coords, so the
        # offset is passed to CopyFromScreen and the bitmap is display-local.
        sel = f"$scr=[System.Windows.Forms.Screen]::AllScreens;$d=$scr[{display - 1}].Bounds;" if display >= 1 else "$d=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
        ps = (
            DPI_PREAMBLE
            + "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            + sel
            + "$bmp=New-Object Drawing.Bitmap $d.Width,$d.Height;"
            "$g=[Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($d.X,$d.Y,0,0,$d.Width,$d.Height);"
            f"$bmp.Save('{q}');"
            "$g.Dispose();$bmp.Dispose()"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if proc.returncode != 0:
            return False, proc.stderr.strip()[-500:] or "powershell capture failed"
        return True, ""

    def _grid_overlay(self, out: Path) -> None:
        """Bake the labeled coordinate grid into a captured image."""
        try:
            from PIL import Image, ImageDraw

            from saturday.tools.spatial import GRID_CELL, cell_name

            img = Image.open(out)
            w, h = img.size
            draw = ImageDraw.Draw(img)
            c = 0
            while c * GRID_CELL <= w:
                draw.line([(c * GRID_CELL, 0), (c * GRID_CELL, h)], fill=(255, 0, 0))
                r = 0
                while r * GRID_CELL <= h:
                    draw.line([(0, r * GRID_CELL), (w, r * GRID_CELL)], fill=(255, 0, 0))
                    draw.text((c * GRID_CELL + 3, r * GRID_CELL + 2), cell_name(c, r), fill=(255, 0, 0))
                    r += 1
                c += 1
            img.save(out)
        except Exception:
            pass  # no pillow: capture lands unannotated, matching legacy behavior

    def _shot_via_native(self, out: Path) -> bool:
        """No-pillow fallback: macOS screencapture / Linux ImageMagick import."""
        try:
            if sys.platform == "darwin":
                proc = subprocess.run(
                    ["screencapture", "-x", "-t", "png", str(out)],
                    capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return proc.returncode == 0 and out.stat().st_size > 0
            if shutil.which("import"):
                proc = subprocess.run(["import", "-window", "root", str(out)], capture_output=True, timeout=30)
                return proc.returncode == 0 and out.stat().st_size > 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        return False

    def _native_window_capture(self, query: str, out: Path, note: str = "") -> tuple[bool, str]:
        """Screen capture of a single window on macOS/Linux (best effort)."""
        from saturday.tools import spatial_unix

        ok, err, row = spatial_unix._resolve_window(query)
        if not ok:
            return False, err
        try:
            if sys.platform == "darwin":
                region = f"{row['left']},{row['top']},{row['width']},{row['height']}"
                proc = subprocess.run(
                    ["screencapture", "-x", "-R", region, str(out)],
                    capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                detail = (proc.stderr or b"").decode("utf-8", errors="replace")[-200:]
                if proc.returncode != 0:
                    return False, f"window capture failed: {detail or 'grant Screen Recording permission in System Settings'}"
            else:
                if shutil.which("import") is None:
                    return False, "window capture on Linux needs ImageMagick 'import'"
                proc = subprocess.run(["import", "-window", row["winid"], str(out)], capture_output=True, timeout=30)
                if proc.returncode != 0:
                    return False, f"window capture failed: {(proc.stderr or b'').decode('utf-8', errors='replace')[-200:]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"window capture failed: {exc}"
        if not out.exists() or out.stat().st_size < 100:
            return False, "window capture produced no image data"
        self.pending_images = [str(out.resolve())]
        head = f"[window capture saved: {out} ({out.stat().st_size} bytes) | {row.get('title', '?')!r} | platform-native capture"
        return True, head + (f" | {note}]" if note else "]")

    def run(self, args: dict) -> tuple[bool, str]:
        target = self.shots_dir or (Path(".saturday") / "shots")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"cannot create shots dir: {exc}"
        out = target / f"screen_{int(time.time()*1000)}.png"
        annotate = args.get("annotate") if args.get("annotate") in ("none", "grid", "marked") else "none"

        if args.get("capture_window"):
            if not sys.platform.startswith("win"):
                return self._native_window_capture(str(args["capture_window"]), out, note=args.get("monitor_note") or "")
            from saturday.tools.spatial import capture_window_bg

            ok, legend = capture_window_bg(str(args["capture_window"]), out)
            if not ok:
                return False, legend
            if self.cache is not None and self.cache.frame_unchanged(f"win:{args['capture_window']}", out):
                return True, "[window capture: unchanged frame since the last capture - reuse the existing image]"
            self.pending_images = [str(out.resolve())]
            note = args.get("monitor_note") or ""
            msg = f"[window capture saved: {out} ({out.stat().st_size} bytes) | {legend} | captured non-intrusively (window stayed in background)]"
            if note:
                msg += " | " + note
            return True, msg + "]"

        legend = ""
        display = args.get("display")
        display_idx = display if isinstance(display, int) and display >= 1 else 0
        if sys.platform.startswith("win") and annotate in ("grid", "marked") and not display_idx:
            from saturday.tools.spatial import capture_annotated

            ok, legend = capture_annotated(out, annotate, landmarks=self.landmarks)
            if not ok:
                return False, legend
        else:
            if display_idx:
                if not sys.platform.startswith("win"):
                    return False, "display selection requires Windows"
                if annotate == "marked":
                    return False, "annotate='marked' is primary-monitor only; use annotate='grid' with display=N"
                ok, err = self._shot_via_powershell(out, display=display_idx)
                if not ok:
                    return False, f"display {display_idx} capture failed: {err} (does that monitor exist? omit display= for the primary)"
            elif not self._shot_via_pillow(out):
                if not sys.platform.startswith("win"):
                    if not self._shot_via_native(out):
                        return False, "screen capture needs pillow (pip install pillow), macOS 'screencapture', or Linux ImageMagick 'import'"
                else:
                    ok, err = self._shot_via_powershell(out)
                    if not ok:
                        return False, f"screen capture failed: {err} (or install pillow)"
            if annotate == "grid":
                self._grid_overlay(out)

        if not out.exists() or out.stat().st_size < 100:
            return False, "capture produced no image data"
        if self.cache is not None:
            ckey = f"screen:{display_idx}:{annotate}"
            if self.cache.frame_unchanged(ckey, out):
                self.pending_images = []  # nothing new to attach
                nb = args.get("monitor_note") or ""
                head = "[screen unchanged: identical frame to the last capture - act on the image you already have"
                return True, head + (f" | {nb}]" if nb else "]")
        self.pending_images = [str(out.resolve())]
        note = args.get("monitor_note") or ""
        size = out.stat().st_size
        msg = f"[screenshot saved: {out} ({size} bytes)"
        if display_idx:
            msg += f" | display {display_idx}"
        if annotate == "grid":
            try:
                from PIL import Image

                with Image.open(out) as img:
                    w, h = img.size
                from saturday.tools.spatial import build_grid_legend

                msg += " | " + build_grid_legend(w, h)
            except Exception:
                msg += " | labeled grid overlaid"
        if annotate == "marked" and legend:
            msg += "\n" + legend
        if note:
            msg += " | " + note
        return True, msg + "]"
