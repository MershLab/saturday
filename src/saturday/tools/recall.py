"""memory_search tool: cross-session recall over past transcripts."""
from __future__ import annotations

from saturday.tools.base import Tool


class MemorySearchTool(Tool):
    name = "memory_search"
    description = (
        "Search ALL past sessions (across chats and runs) for things you did before. "
        "Use when a task references earlier work, past decisions, or knowledge the user "
        "expects you to remember. Returns matching snippets with session id + date. "
        "The index is local (SQLite FTS5), never sent anywhere."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "what to look for, e.g. 'deployment script' or 'database schema'" },
            "k": {"type": "integer", "description": "max snippets to return (1-20, default 6)"},
        },
        "required": ["query"],
    }

    def __init__(self, index=None) -> None:
        self._index = index

    def _index_for(self):
        if self._index is None:
            from saturday.recall import RecallIndex, default_store_root

            self._index = RecallIndex(store_root=default_store_root())
        return self._index

    def run(self, args: dict) -> tuple[bool, str]:
        query = str(args.get("query") or "").strip()
        if not query:
            return False, "memory_search needs query="
        k = args.get("k") or 6
        try:
            k = max(1, min(20, int(k)))
        except (TypeError, ValueError):
            k = 6
        try:
            from saturday.recall import format_recall

            results = self._index_for().search(query, k=k)
            return True, format_recall(results)
        except Exception as exc:  # recall must never break the agent loop
            return False, f"memory_search unavailable: {type(exc).__name__}: {exc}"
