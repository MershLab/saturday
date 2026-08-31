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
        "Read, search or write your persistent memory (survives across sessions). "
        "action='search' with query= is the one to reach for: it ranks notes by "
        "relevance, how recently they were touched and how much they add, follows "
        "links between them, and flags notes that contradict each other. "
        "action='read' returns the whole file; 'write' replaces it; 'append' adds a line."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "search", "write", "append"]},
            "text": {"type": "string", "description": "for write/append"},
            "query": {"type": "string", "description": "for search"},
            "k": {"type": "integer", "description": "for search: how many notes (default 6)"},
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


    def _search(self, args: dict, path: Path) -> tuple[bool, str]:
        """Rank the curated notes instead of returning the whole file.

        Reading everything works while memory is small and stops working
        exactly when it starts being valuable. The index behind this already
        scores relevance, recency and novelty, and knows which notes
        contradict each other."""
        query = str(args.get("query") or "").strip()
        if not query:
            return False, "memory search needs query="
        try:
            k = max(1, min(20, int(args.get("k") or 6)))
        except (TypeError, ValueError):
            k = 6
        try:
            from saturday.memindex import MemoryIndex

            idx = MemoryIndex()
            try:
                for scope, src in self._sources(path):
                    text = src.read_text(encoding="utf-8", errors="replace") if src.is_file() else ""
                    idx.reindex(text, scope=scope)
                hits = idx.search(query, k=k)
                clashes = {e["from"] for e in idx.graph()["edges"] if e["relation"] == "contradicts"}
                clashes |= {e["to"] for e in idx.graph()["edges"] if e["relation"] == "contradicts"}
            finally:
                idx.close()
        except Exception as exc:  # memory must never break the loop
            return False, f"memory search unavailable: {type(exc).__name__}: {exc}"
        if not hits:
            return True, "nothing in memory matches that"
        lines = []
        for h in hits:
            flags = []
            if not h["matched"]:
                flags.append("via a link")
            if h["id"] in clashes:
                # a note another note disagrees with is worth saying out loud
                flags.append("DISPUTED by another note")
            suffix = f"  ({', '.join(flags)})" if flags else ""
            lines.append(f"- {h['text']}{suffix}")
        return True, "\n".join(lines)

    def _sources(self, path: Path):
        """(scope, file) pairs this tool indexes, matching what read merges."""
        out = [("global", memory_path())]
        if self.scope_path:
            out.append((f"project:{Path(self.scope_path).resolve().parent.parent}", path))
        return out

    def run(self, args: dict) -> tuple[bool, str]:
        path = Path(self.scope_path) if self.scope_path else memory_path()
        action = args.get("action", "read")
        text = args.get("text", "")
        if action == "search":
            return self._search(args, path)
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
