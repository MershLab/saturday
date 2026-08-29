from __future__ import annotations

from pathlib import Path


def memory_path() -> Path:
    from saturday.config import CONFIG_DIR

    return CONFIG_DIR / "MEMORY.md"


class MemoryTool:
    """hermes-style persistent memory: agent-curated facts that survive sessions.

    When ``scope_path`` is set (per-project memory), writes/updates go to the
    project's ``.saturday/MEMORY.md`` while reads merge global + project so
    the agent keeps baseline facts plus project-specific ones."""

    name = "memory"
    description = (
        "Read or write your persistent memory file (survives across sessions). "
        "action='read' returns it; action='write' replaces it; action='append' adds a line."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "append"]},
            "text": {"type": "string", "description": "for write/append"},
        },
        "required": ["action"],
    }

    MAX_CHARS = 8_000

    def __init__(self, scope_path: str | Path | None = None) -> None:
        self.scope_path = str(scope_path) if scope_path else None

    @staticmethod
    def _read_file(p: Path, limit: int) -> str:
        if not p.is_file():
            return ""
        try:
            return p.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def run(self, args: dict) -> tuple[bool, str]:
        path = Path(self.scope_path) if self.scope_path else memory_path()
        action = args.get("action", "read")
        text = args.get("text", "")
        if action == "read":
            sections = []
            if self.scope_path:
                glob = self._read_file(memory_path(), 2_000)
                # WHY: a full global block made MAX_CHARS - len(glob) <= 0 and
                # text[-limit:] with a non-positive limit returns EVERYTHING,
                # blowing the cap; clamp to a floor so total stays near
                # MAX_CHARS even when global memory is at its own cap.
                limit = max(256, self.MAX_CHARS - len(glob))
                proj = self._read_file(path, limit)
                if not glob and not proj:
                    return True, "(memory empty)"
                if glob:
                    sections.append(f"(global memory)\n{glob}")
                if proj:
                    sections.append(f"(project memory)\n{proj}")
            else:
                content = self._read_file(path, self.MAX_CHARS)
                if not content:
                    return True, "(memory empty)"
                sections.append(content)
            return True, "\n\n".join(sections)
        if not text.strip():
            return False, "empty text for write/append"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if action == "append":
                existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                combined = (existing.rstrip() + "\n" + text.strip()).lstrip("\n")
                if len(combined) > self.MAX_CHARS:
                    combined = combined[-self.MAX_CHARS:]
                    nl = combined.find("\n")
                    if nl != -1:
                        combined = combined[nl + 1:]
                path.write_text(combined, encoding="utf-8")
            else:
                path.write_text(text.strip()[:self.MAX_CHARS], encoding="utf-8")
            return True, f"memory {action}ed ({path})"
        except OSError as exc:
            return False, f"memory unavailable: {exc}"


def load_memory_block(scope: str | Path | None = None) -> str:
    blocks = []
    if scope:
        proj = Path(scope) / ".saturday" / "MEMORY.md"
        text = MemoryTool._read_file(proj, 4_000)
        if text:
            blocks.append(f"(project memory)\n{text}")
    base = MemoryTool._read_file(memory_path(), 4_000)
    if base:
        blocks.append(base)
    return "\n\n".join(blocks)
