"""OCR text-grounding (Mixture of Grounding parity) + macOS/Linux key helpers."""
from __future__ import annotations

from pathlib import Path

import saturday.tools.ocr as ocr
from saturday.tools.ocr import UiTextTool, parse_tsv, slugify
from saturday.tools.spatial import LandmarkStore
from saturday.tools.spatial_unix import parse_combo_mac, translate_linux_key


def test_parse_tesseract_tsv():
    tsv = (
        "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t800\t600\t-1\t\n"
        "5\t1\t0\t0\t0\t0\t120\t40\t60\t21\t92\tSave\n"
        "5\t1\t0\t0\t0\t1\t300\t55\t40\t18\t55\tCancel\n"
        "5\t1\t0\t0\t0\t2\t700\t30\t30\t15\t88\tOK\n"
    )
    boxes = parse_tsv(tsv)
    assert [b["text"] for b in boxes] == ["Save", "Cancel", "OK"]
    assert boxes[0]["x"] == 120 and boxes[0]["conf"] == 92


def test_parse_pipe_lines():
    boxes = ocr._parse_pipe_lines("10|20|30|40|100|Hello World\n5|6|7|8|99|ok\njunk")
    assert len(boxes) == 2 and boxes[0]["text"] == "Hello World"


def test_slugify():
    assert slugify("Save File!") == "save-file"
    assert slugify("日本語") == "-"


def test_ui_text_tool_registers_landmarks(monkeypatch):
    store = LandmarkStore()

    class FakeScreen:
        def __init__(self):
            self.pending_images = [str(Path("x") / "shot.png")]

        def run(self, args):
            return True, "captured"

    tool = UiTextTool(landmarks=store, screen_tool=FakeScreen())
    boxes = [
        {"text": "Save", "x": 120, "y": 40, "w": 60, "h": 21, "conf": 92},
        {"text": "Cancel", "x": 280, "y": 46, "w": 40, "h": 18, "conf": 55},
    ]

    def fake_ocr(image):
        return True, "", boxes

    monkeypatch.setattr(ocr, "ocr_text_boxes", fake_ocr)
    ok, out = tool.run({})
    assert ok
    assert "(150,50) [save]" in out and "(300,55) [cancel]" in out
    assert store.resolve("save")["x"] == 150 and store.resolve("save")["y"] == 50
    assert store.resolve("cancel")["x"] == 300 and store.resolve("cancel")["y"] == 55


def test_ui_text_tool_ocr_failure_is_clean(monkeypatch):
    class FakeScreen:
        def __init__(self):
            self.pending_images = [str(Path("x") / "shot.png")]

        def run(self, args):
            return True, "captured"

    tool = UiTextTool(screen_tool=FakeScreen())

    def fake_ocr(image):
        return False, "OCR unavailable (tesseract not found on macOS/Linux)", []

    monkeypatch.setattr(ocr, "ocr_text_boxes", fake_ocr)
    ok, out = tool.run({})
    assert not ok and "OCR unavailable" in out


# -- macOS/Linux key helpers (pure, verifiable without hardware) ---------------


def test_mac_combo_maps_to_kvk_and_modifiers():
    assert parse_combo_mac("Ctrl+Q") == (12, ["control down"])
    assert parse_combo_mac("Cmd+Shift+Right") == (124, ["command down", "shift down"])
    assert parse_combo_mac("Alt+F4") == (118, ["option down"])
    assert parse_combo_mac("Enter") == (36, [])
    assert parse_combo_mac("Ctrl+UnknownKey") is None


def test_linux_combo_translates_to_xdotool():
    assert translate_linux_key("Ctrl+S") == "ctrl+s"
    assert translate_linux_key("Shift+Tab") == "shift+Tab"
    assert translate_linux_key("F5") == "F5"
    assert translate_linux_key("Alt+F4") == "alt+F4"
    assert translate_linux_key("Ctrl+Nope") is None
