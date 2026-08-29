from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from saturday.tools.base import Tool


def build_vision_content(text: str, image_paths: list[str], max_images: int = 4) -> list[dict]:
    parts: list[dict] = [{"type": "text", "text": text}]
    for raw in image_paths[:max_images]:
        p = Path(raw)
        if not p.is_file():
            raise FileNotFoundError(f"image not found: {p}")
        mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return parts


class ViewImageTool(Tool):
    """Attaches a local image into the conversation as a vision message part."""

    name = "view_image"
    description = (
        "Show a local image file to yourself (vision models). The image is attached to "
        "the next observation so you can inspect screenshots, diagrams, or photos."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_side": {"type": "integer", "description": "ignored placeholder for future resizing"},
        },
        "required": ["path"],
    }

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root).resolve() if root else None
        self.pending_images: list[str] = []

    def run(self, args: dict) -> tuple[bool, str]:
        path = str(args.get("path") or "")
        p = Path(path)
        if not p.is_file():
            return False, f"image not found: {p}"
        resolved = p.resolve()
        if self.root is not None and resolved != self.root and self.root not in resolved.parents:
            return False, "path escapes workspace root"
        size = p.stat().st_size
        if size > 8_000_000:
            return False, f"image too large ({size} bytes; max 8MB)"
        mime = mimetypes.guess_type(str(p))[0] or ""
        if not mime.startswith("image/"):
            return False, f"not an image ({mime or 'unknown type'})"
        self.pending_images = [str(resolved)]
        return True, f"[image attached: {p.name}, {size} bytes]"
