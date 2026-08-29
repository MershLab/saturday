"""World-model principles at harness scale: state cache, delta observations,
frame dedupe, prediction-verify, asset embedding, ablation rig."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.ablation import FILE_MARKER, run_ablation, _summary  # noqa: E402
from saturday.exporter import collect_image_paths, embed_assets  # noqa: E402
from saturday.statemap import StateCache, compute_delta, element_box, element_identity  # noqa: E402
from saturday.tools.screen import ScreenTool  # noqa: E402
from saturday.tools.spatial import UiTreeTool, verify_expect  # noqa: E402


# -- state cache ---------------------------------------------------------------


def el(t, n, x, y, w=10, h=10):
    return {"t": t, "n": n, "x": x, "y": y, "w": w, "h": h}


def test_compute_delta_classifies_changes():
    old = {element_identity(e): e for e in [el("Button", "Save", 1, 1), el("Button", "OK", 5, 5)]}
    new = [el("Button", "Save", 1, 1), el("Button", "Cancel", 30, 30), el("Button", "OK", 5, 5)]
    d = compute_delta(old, new)
    assert len(d["added"]) == 1 and d["added"][0]["n"] == "Cancel"
    assert len(d["removed"]) == 0
    # moved element: same identity, different box
    d2 = compute_delta(old, [el("Button", "Save", 99, 99), el("Button", "OK", 5, 5)])
    assert len(d2["changed"]) == 1 and d2["changed"][0]["n"] == "Save"
    assert element_box(old[element_identity(el("Button", "Save", 1, 1))]) == (1, 1, 10, 10)


def test_state_cache_frame_dedupe(tmp_path):
    cache = StateCache()
    a = tmp_path / "a.png"
    a.write_bytes(b"frame-1")
    b = tmp_path / "b.png"
    b.write_bytes(b"frame-1")
    c = tmp_path / "c.png"
    c.write_bytes(b"frame-2")
    assert cache.frame_unchanged("k", a) is False  # first sighting
    assert cache.frame_unchanged("k", b) is True  # same content
    assert cache.frame_unchanged("k", c) is False  # different content


# -- ui_tree delta mode --------------------------------------------------------


def test_ui_tree_delta_mode_with_fake_runner():
    a = [el("Button", "Save", 10, 10), el("Button", "OK", 50, 50)]
    b = [el("Button", "Save", 10, 10), el("Button", "Cancel", 30, 30)]
    calls = {"n": 0}

    def runner(s, timeout=20.0):
        calls["n"] += 1
        out = json.dumps(a) if calls["n"] == 1 else json.dumps(b)
        return (0, out, "") if "EnumWindows" not in s else (0, "111|Notepad|0,0,800,600", "")

    tool = UiTreeTool(runner=runner)
    ok1, out1 = tool.run({"scope": "foreground"})
    assert ok1 and "elements=2" in out1  # first scan: full (no cache yet)
    ok2, out2 = tool.run({"scope": "foreground"})
    assert ok2 and "ui_tree delta" in out2
    assert "+1 new" in out2 and "-1 gone" in out2 and "Cancel" in out2
    ok3, out3 = tool.run({"scope": "foreground"})
    assert ok3 and "NO CHANGE" in out3
    ok4, out4 = tool.run({"scope": "foreground", "mode": "full"})
    assert ok4 and "elements=2" in out4  # explicit full still works


def test_verify_expect_observed_and_missing():
    def runner(s, timeout=20.0):
        if "EnumWindows" in s:
            return 0, "111|Notepad - Untitled|0,0,800,600", ""
        return 0, "[]", ""  # scan returns nothing

    note = verify_expect(runner, "notepad", attempts=1)
    assert "observed in window title" in note
    miss = verify_expect(lambda s, t=20.0: (0, "111|Calc", ""), "notepad", attempts=1)
    assert "NOT observed" in miss


# -- screenshot frame dedupe ---------------------------------------------------


def test_screen_tool_returns_unchanged_frame(tmp_path, monkeypatch):
    tool = ScreenTool(shots_dir=tmp_path, cache=StateCache())

    def fake_shot(self, out):
        out.write_bytes(b"SAME" * 64)
        return True

    monkeypatch.setattr(ScreenTool, "_shot_via_pillow", fake_shot)
    ok1, out1 = tool.run({"annotate": "none"})
    assert ok1 and "screenshot saved" in out1 and tool.pending_images
    time.sleep(0.01)
    ok2, out2 = tool.run({"annotate": "none"})
    assert ok2 and "unchanged" in out2
    assert tool.pending_images == [], "unchanged frame must not re-attach"


# -- asset embedding -----------------------------------------------------------


def test_embed_assets_copies_and_rewrites(tmp_path):
    img = tmp_path / "shot-1.png"
    img.write_bytes(b"png")
    records = [
        {"messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": str(img)}}]},
            {"role": "assistant", "content": "done"},
        ]}
    ]
    assert collect_image_paths(records) == [str(img)]
    copied = embed_assets(records, tmp_path / "out.jl.assets")
    assert copied == 1
    ref = records[0]["messages"][0]["content"][0]["image_url"]["url"]
    assert ref.startswith("out.jl.assets/") and (tmp_path / ref).is_file()
    # http/data refs are never touched
    records2 = [{"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}]
    assert collect_image_paths(records2) == []


# -- ablation rig --------------------------------------------------------------


def test_run_ablation_full_variant(tmp_path):
    turns = [
        {"tool_calls": [{"name": "write_file", "arguments": {"path": "ablation_probe.txt", "content": FILE_MARKER}}]},
        {"content": "done"},
    ]

    def factory(cfg):
        from saturday.agent.core import Agent

        return Agent(cfg=cfg, client=make_scripted_model(turns), enable_subagents=False)

    task = {
        "id": "file-write",
        "prompt": "probe",
        "check": lambda ws, traj: (
            (ws / "ablation_probe.txt").is_file()
            and (ws / "ablation_probe.txt").read_text(encoding="utf-8").strip() == FILE_MARKER,
            "checked",
        ),
    }
    payload = run_ablation(tasks=[task], variants=["full"], workspace=tmp_path, out_dir=tmp_path / "runs", agent_factory=factory)
    row = payload["results"][0]
    assert row["ok"] is True and row["variant"] == "full" and row["steps"] == 2
    assert payload["summary"]["full"]["passed"] == 1
    assert list((tmp_path / "runs").glob("ablation-*.json")), "results json persisted"


def test_summary_math():
    summary = _summary([
        {"variant": "full", "ok": True, "steps": 2, "tokens": 10, "seconds": 1.0},
        {"variant": "full", "ok": False, "steps": 5, "tokens": 20, "seconds": 3.0},
    ])
    assert summary["full"]["pass_rate"] == 0.5 and summary["full"]["avg_steps"] == 3.5
