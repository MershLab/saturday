"""Text-grounding expert (Agent S2 "Mixture of Grounding" parity).

Structural grounding comes from UIA (ui_tree); screenshots give the model
vision; this adds the third pillar: OCR text boxes with coordinates, so the
agent can click what the accessibility tree cannot name. Backends:
Windows.Media.Ocr (built-in, no install) then tesseract CLI (all platforms,
optional). Boxes feed the same LandmarkStore the pointer uses, so
``pointer target=<text-slug>`` works after a ui_text scan.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from saturday.tools.base import Tool


def parse_tsv(tsv_text: str) -> list[dict]:
    """Parse tesseract TSV (tsv mode) into word boxes."""
    boxes: list[dict] = []
    headers = None
    for line in tsv_text.splitlines():
        parts = line.split("\t")
        if headers is None:
            headers = parts
            continue
        if len(parts) < 12:
            continue
        row = dict(zip(headers, parts))
        if row.get("level") != "5":
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            x, y, w, h = int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"])
            conf = int(float(row.get("conf", 0)))
        except ValueError:
            continue
        boxes.append({"text": text, "x": x, "y": y, "w": w, "h": h, "conf": conf})
    return boxes


def _parse_pipe_lines(text: str) -> list[dict]:
    boxes: list[dict] = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        try:
            x, y, w, h, conf = (int(float(p)) for p in parts[:5])
        except ValueError:
            continue
        box_text = "|".join(parts[5:]).strip()
        if not box_text:
            continue
        boxes.append({"text": box_text, "x": x, "y": y, "w": w, "h": h, "conf": conf})
    return boxes


def winrt_ocr_ps(image: Path) -> tuple[bool, str]:
    """Windows built-in OCR (Windows.Media.Ocr via PowerShell 5.1)."""
    q = str(image.resolve()).replace("'", "''")
    script = """
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $resType) {
    $t = $asTask.MakeGenericMethod($resType).Invoke($null, @($op)); $t.Wait(); $t.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('__IMG__')) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language('en-US'))) }
if (-not $engine) { Write-Output 'ERR no-ocr-engine'; exit }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { foreach ($w in $line.Words) {
  $r = $w.BoundingRect
  '{0}|{1}|{2}|{3}|{4}|{5}' -f [int]$r.X,[int]$r.Y,[int]$r.Width,[int]$r.Height,100,$w.Text } }
""".replace("__IMG__", q)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = (proc.stdout or "").strip()
    if out.startswith("ERR") or (proc.returncode != 0 and not out):
        return False, (proc.stderr or out or "winrt OCR failed")[:300]
    return True, out


def ocr_text_boxes(image: Path) -> tuple[bool, str, list[dict]]:
    """(ok, error_or_empty, boxes) — WinRT OCR first, then tesseract."""
    if sys.platform.startswith("win"):
        try:
            ok, out = winrt_ocr_ps(image)
            if ok:
                boxes = _parse_pipe_lines(out)
                if boxes:
                    return True, "", boxes
            # fall through: WinRT missing/failed silently -> tesseract
        except Exception:
            pass
    tess = shutil.which("tesseract")
    if tess:
        try:
            proc = subprocess.run(
                [tess, str(image), "-", "-l", "eng", "--psm", "6", "tsv"],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0:
                boxes = parse_tsv(proc.stdout or "")
                if boxes:
                    return True, "", boxes
        except (OSError, subprocess.TimeoutExpired):
            pass
    hint = "on Windows no install is needed; tesseract or WinRT OCR did not run"
    return False, f"OCR unavailable ({hint})", []


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "-"


class UiTextTool(Tool):
    """OCR text grounding: scan the screen, list text boxes with coordinates."""

    name = "ui_text"
    description = (
        "OCR the screen and list every text box with its center coordinates — the "
        "grounding path for apps whose accessibility tree hides controls (games, "
        "canvas UIs, screenshots, remote desktops). Each entry gets a target id so "
        "pointer target=<id> clicks it, or use the displayed x,y. Reads the PRIMARY "
        "monitor; Windows uses built-in OCR, other platforms use tesseract."
    )
    parameters = {
        "type": "object",
        "properties": {
            "keep": {"type": "boolean", "description": "persist listed text boxes as landmarks (default true)"},
        },
        "required": [],
    }

    def __init__(self, landmarks=None, screen_tool=None) -> None:
        self.landmarks = landmarks
        self.screen_tool = screen_tool

    def _capture(self) -> tuple[bool, str, Path]:
        from saturday.tools.screen import ScreenTool

        st = self.screen_tool or ScreenTool()
        ok, msg = st.run({"annotate": "none"})
        if not ok or not st.pending_images:
            return False, msg or "capture failed", Path("")
        return True, "", Path(st.pending_images[0])

    def run(self, args: dict) -> tuple[bool, str]:
        keep = args.get("keep", True)
        ok, err, image = self._capture()
        if not ok:
            return False, err
        captured_ok, ocr_err, boxes = ocr_text_boxes(image)
        if not captured_ok or not boxes:
            return False, ocr_err or "no text found"
        lines = ["ui_text scan (screen coordinates; center = click point):"]
        used: dict[str, int] = {}
        for i, b in enumerate(boxes, 1):
            cx, cy = b["x"] + b["w"] // 2, b["y"] + b["h"] // 2
            if keep and self.landmarks is not None:
                base = slugify(b["text"])
                used[base] = used.get(base, 0) + 1
                tid = base if used[base] == 1 else f"{base}-{used[base]}"
                self.landmarks.add(tid, cx, cy, b["text"][:40])
                text_out = b["text"][:60].replace("\n", " ")
                lines.append(f"{i}. ({cx},{cy}) [{tid}] conf={b['conf']} {text_out}")
            else:
                lines.append(f"{i}. ({cx},{cy}) {b['text'][:60]}")
            if len(lines) > 25:
                lines.append(f"... {len(boxes) - 25} more; pointer target=<id> clicks any of them")
                break
        return True, "\n".join(lines)
