"""Desktop task-suite ablation runner (measure the harness, not the model).

Runs one-shot agents on verifiable LOCAL tasks under different grounding
configurations and records per-variant: pass, steps, tokens, duration,
captures. This is the measurement rig for "how much does this harness
contribute" — the structural/textual/visual layer ablations map directly to
Windows Agent Arena experiments if you swap the task list for the official
WAA harness (microsoft/Windows-Agent-Arena); the variant matrix is identical.

Variants (which grounding tools stay enabled):
  full          -> everything (structural + textual + visual)
  no-textual    -> ui_text disabled (OCR expert off)
  no-structural -> ui_tree/ui_invoke disabled (accessibility expert off)
  vision-only   -> both experts off (classic screenshot+grid loop)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

VARIANTS: dict[str, list[str]] = {
    "full": [],
    "no-textual": ["ui_text"],
    "no-structural": ["ui_tree", "ui_invoke"],
    "vision-only": ["ui_tree", "ui_invoke", "ui_text"],
}

FILE_MARKER = "SAT_ABLATION_OK"
CLIPBOARD_MARKER = "SAT_CLIPBOARD_OK"
TYPED_MARKER = "SAT_TYPED_OK"


def make_tasks() -> list[dict]:
    """Verifiable local tasks: post-check is independent of the agent's claims."""

    def check_file(ws: Path, traj) -> tuple[bool, str]:
        p = ws / "ablation_probe.txt"
        if not p.is_file():
            return False, "ablation_probe.txt missing"
        body = p.read_text(encoding="utf-8", errors="replace").strip()
        return (body == FILE_MARKER, f"content={body[:40]!r}")

    def check_clipboard(ws: Path, traj) -> tuple[bool, str]:
        from saturday.tools.spatial import ClipboardTool

        ok, out = ClipboardTool().run({"action": "get"})
        return ok and CLIPBOARD_MARKER in out, (out[:100] if ok else out)

    def check_notepad(ws: Path, traj) -> tuple[bool, str]:
        from saturday.tools.spatial import WindowTool

        ok, out = WindowTool().run({"action": "list"})
        return ok and "Notepad" in out, out[:120]

    return [
        {
            "id": "file-write",
            "prompt": f"Create a file named ablation_probe.txt in the workspace containing exactly {FILE_MARKER} and no other text.",
            "check": check_file,
        },
        {
            "id": "clipboard-set",
            "prompt": f"Copy the text {CLIPBOARD_MARKER} to the clipboard so the user can paste it.",
            "check": check_clipboard,
        },
        {
            "id": "notepad-type",
            "prompt": "Open Notepad and type exactly " + TYPED_MARKER + " into it.",
            "check": check_notepad,
        },
    ]


def run_ablation(
    tasks: list[dict] | None = None,
    variants: list[str] | None = None,
    workspace: str | Path | None = None,
    out_dir: str | Path = "runs",
    dry: bool = False,
    agent_factory=None,
) -> dict:
    """Run the matrix; returns the full payload (and writes runs/ablation-*.json)."""
    tasks = tasks or make_tasks()
    variants = variants or list(VARIANTS)
    workspace = Path(workspace or ".")
    results: list[dict] = []

    for variant in variants:
        disabled = VARIANTS.get(variant, [])
        for task in tasks:
            start = time.time()
            entry = {
                "variant": variant,
                "task": task["id"],
                "disabled_tools": disabled,
                "ok": False,
                "steps": 0,
                "tokens": 0,
                "seconds": 0.0,
                "detail": "",
            }
            try:
                from saturday.agent.core import Agent
                from saturday.config import AgentConfig

                cfg = AgentConfig.load({"disabled_tools": disabled, "workspace_root": str(workspace)})
                agent = agent_factory(cfg) if agent_factory else Agent(cfg=cfg, safety="off")
                traj = agent.run(task["prompt"], session_id=f"ablation-{variant}-{task['id']}")
                ok, detail = task["check"](workspace, traj)
                entry.update({
                    "ok": bool(ok),
                    "steps": len(traj.steps),
                    "tokens": int(getattr(traj.usage, "total_tokens", 0) or 0),
                    "detail": str(detail)[:220],
                })
            except Exception as exc:  # a broken variant must never kill the matrix
                entry["detail"] = f"{type(exc).__name__}: {exc}"[:220]
            entry["seconds"] = round(time.time() - start, 2)
            results.append(entry)

    ts = time.strftime("%Y%m%d-%H%M%S")
    summary = _summary(results)
    payload = {"generated": ts, "variants": variants, "tasks": [t["id"] for t in tasks], "results": results, "summary": summary}
    if not dry:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"ablation-{ts}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["_path"] = str(out / f"ablation-{ts}.json") if not dry else ""
    return payload


def _summary(results: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r["variant"], []).append(r)
    out: dict[str, dict] = {}
    for variant, rows in by.items():
        ok_rows = [r for r in rows if r["ok"]]
        out[variant] = {
            "passed": sum(1 for r in rows if r["ok"]),
            "total": len(rows),
            "pass_rate": round(len(ok_rows) / len(rows), 3) if rows else 0.0,
            "avg_steps": round(sum(r["steps"] for r in rows) / len(rows), 1) if rows else 0.0,
            "avg_tokens": round(sum(r["tokens"] for r in rows) / len(rows), 0) if rows else 0.0,
            "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 2) if rows else 0.0,
        }
    return out
