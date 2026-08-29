"""Trajectory image embedding for dataset exports (R2S2R-style asset pipeline).

The export flow writes OpenAI-format JSONL; ``embed_assets`` copies every
screenshot the agent actually captured into a sidecar assets dir and rewrites
references to relative paths, so a dataset ships self-contained (and the
image+action pairs become usable for policy training, not just prose).
"""
from __future__ import annotations

import shutil
from pathlib import Path

_REF_KEYS = ("image_url", "url", "image")


def _iter_refs(records: list[dict]):
    for rec in records:
        for m in rec.get("messages", []):
            content = m.get("content") if isinstance(m, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict):
                    yield part


def _ref_value(part: dict) -> str:
    for key in _REF_KEYS:
        val = part.get(key)
        if isinstance(val, dict):
            val = val.get("url")
        if isinstance(val, str):
            return val
    return ""


def collect_image_paths(records: list[dict]) -> list[str]:
    """Local file paths referenced by image fields (skips http/data:)."""
    out: list[str] = []
    seen: set[str] = set()
    for part in _iter_refs(records):
        val = _ref_value(part)
        if not val or val.startswith(("http://", "https://", "data:")) or val in seen:
            continue
        if Path(val).is_file():
            seen.add(val)
            out.append(val)
    return out


def embed_assets(records: list[dict], assets_dir: Path) -> int:
    """Copy referenced images into ``assets_dir`` and rewrite refs; count copied."""
    copied = 0
    for img in collect_image_paths(records):
        p = Path(img)
        assets_dir.mkdir(parents=True, exist_ok=True)
        dest = assets_dir / p.name
        if not dest.exists():
            try:
                shutil.copy2(p, dest)
                copied += 1
            except OSError:
                continue
        rel = f"{assets_dir.name}/{p.name}"  # POSIX refs for cross-platform datasets
        for part in _iter_refs(records):
            for key in _REF_KEYS:
                val = part.get(key)
                if isinstance(val, dict):
                    if val.get("url") == img:
                        val["url"] = rel
                elif val == img:
                    part[key] = rel
    return copied
