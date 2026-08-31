from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from saturday.types import ToolResult


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    @abstractmethod
    def run(self, args: dict[str, Any]) -> tuple[bool, str]:
        ...

    def __call__(self, call_id: str, args: dict[str, Any]) -> ToolResult:
        try:
            ok, output = self.run(args)
            return ToolResult(call_id=call_id, name=self.name, ok=ok, output=output)
        except Exception as exc:
            return ToolResult(call_id=call_id, name=self.name, ok=False, output="", error=f"{type(exc).__name__}: {exc}")


def _truncate(text: str, limit: int = 20_000) -> str:
    """Fit text to a limit, keeping the ends and the failures.

    Named for what it replaced; it compresses rather than slicing, because the
    tail of an oversized tool output is usually the part worth reading."""
    if len(text) <= limit:
        return text
    from saturday.compress import compress

    return compress(text, limit)


class ToolRegistry:
    # Plan-mode allowlist: observation/planning only, zero world-mutation.
    # Exact registry names; the window/pointer/keyboard/shell/python families
    # are excluded on purpose (focus changes and execution are mutations).
    READ_ONLY_TOOLS = frozenset(
        {
            "read_file", "list_dir", "glob", "grep", "repo_search", "todo",
            "web_fetch", "web_search",
            "ui_tree", "screen", "view_image",
            "job_list", "job_output", "get_goal", "memory",
            "skills_index", "skill_load",
            "lsp_diagnostics", "lsp_definition",
            "ask_user",  # non-mutating: plan mode may still ask the human
        }
    )

    # Family aliases for tool toggles: disable one alias, hide the whole group.
    TOOL_FAMILIES: dict[str, frozenset] = {
        "web": frozenset({"web_search", "web_fetch"}),
        "browser": frozenset({"browser", "web_browser_js"}),
        "computer_use": frozenset(
            {"pointer", "keyboard", "ui_invoke", "app_open", "window", "clipboard", "screen", "ui_tree"}
        ),
        "shell": frozenset({"shell"}),
        "python": frozenset({"python"}),
        "file_writes": frozenset({"write_file", "edit_file"}),
        "subagents": frozenset({"task"}),
        "memory": frozenset({"memory", "skill_save", "skill_load", "skills_index"}),
        "external_agents": frozenset({"external_agent"}),
    }

    @staticmethod
    def expand_tool_names(names) -> set[str]:
        """Resolve family aliases to concrete tool names; pass through others."""
        out: set[str] = set()
        for n in names or ():
            out.update(ToolRegistry.TOOL_FAMILIES.get(str(n).strip(), frozenset({str(n).strip()})))
        return {n for n in out if n}

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._specs_cache: list[dict[str, Any]] | None = None
        # One lock per tool NAME: parallel calls to the same tool mutate
        # shared per-instance state (pending_images is drained in execute
        # below), so same-name calls must serialize; different tools still
        # run concurrently. dict.setdefault is atomic — racing creators lose
        # one lock object harmlessly.
        self._name_locks: dict[str, threading.Lock] = {}

    def _invalidate(self) -> None:
        self._specs_cache = None

    def excluding(self, names) -> "ToolRegistry":
        """Shallow view with the given concrete tool names removed."""
        view = ToolRegistry()
        for name, tool in self._tools.items():
            if name not in names:
                view._tools[name] = tool
        return view

    def filtered(self, allowed: frozenset | set[str]) -> "ToolRegistry":
        """Shallow view sharing the tool instances but exposing only names in
        ``allowed``. Used by plan mode."""
        view = ToolRegistry()
        for name, tool in self._tools.items():
            if name in allowed:
                view._tools[name] = tool
        return view

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool '{tool.name}'")
        self._tools[tool.name] = tool
        self._invalidate()

    def unregister(self, name: str) -> bool:
        removed = self._tools.pop(name, None) is not None
        if removed:
            self._invalidate()
        return removed

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        """Tool schemas as the API expects them.

        Called on EVERY agent step; schemas never change between registry
        mutations, so the list is cached until register/unregister."""
        if self._specs_cache is None:
            out = []
            for t in self._tools.values():
                spec_fn = getattr(t, "spec", None)
                out.append(spec_fn().schema() if callable(spec_fn) else {"name": t.name, "description": t.description, "parameters": t.parameters})
            self._specs_cache = out
        return self._specs_cache

    def execute(self, call_id: str, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(call_id=call_id, name=name, ok=False, output="", error=f"unknown tool '{name}'")
        # Serialize same-name calls (shared instance state below); cross-tool
        # parallelism is untouched because the lock is per name.
        lock = self._name_locks.setdefault(name, threading.Lock())
        with lock:
            result = self._invoke(tool, call_id, name, args)
            pending = getattr(tool, "pending_images", None)
            if pending:
                result.images = list(pending)
                tool.pending_images = []
        return result

    @staticmethod
    def _invoke(tool, call_id: str, name: str, args: dict[str, Any]) -> ToolResult:
        if isinstance(tool, Tool):
            return tool(call_id, args)
        try:
            ok, output = tool.run(args)
            return ToolResult(call_id=call_id, name=name, ok=ok, output=output)
        except Exception as exc:
            return ToolResult(call_id=call_id, name=name, ok=False, output="", error=f"{type(exc).__name__}: {exc}")

    def render_catalog(self) -> str:
        lines = []
        for t in self._tools.values():
            lines.append(f"- {t.name}: {t.description}\n  params: {json.dumps(t.parameters)}")
        return "\n".join(lines)
