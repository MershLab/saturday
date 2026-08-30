from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from saturday.tools.base import Tool, ToolRegistry

__all__ = ["Plugin", "make_plugin", "install_plugins", "core_plugin", "workflow_plugin"]


@dataclass
class Plugin:
    """Everything is a plugin (cf. deepseek-harness): tools + prompt persona + hooks."""

    name: str
    description: str = ""
    register_fn: Callable[[ToolRegistry], None] | None = None
    tools: list[Tool] = field(default_factory=list)
    persona_sections: list[str] = field(default_factory=list)
    # resource handles (e.g. live MCP clients) this plugin owns and must keep
    # referenced so they can be closed instead of leaking their processes
    clients: list = field(default_factory=list)

    def register(self, registry: ToolRegistry) -> None:
        if self.register_fn is not None:
            self.register_fn(registry)
            return
        for tool in self.tools:
            registry.register(tool)


def make_plugin(
    name: str,
    tools: list,
    description: str = "",
    persona_sections: list[str] | None = None,
) -> Plugin:
    return Plugin(
        name=name,
        description=description,
        tools=list(tools),
        persona_sections=persona_sections or [],
    )


def install_plugins(registry: ToolRegistry, plugins: list[Plugin], persona_out: list[str]) -> None:
    seen: set[str] = set()
    for plugin in plugins:
        if plugin.name in seen:
            raise ValueError(f"duplicate plugin '{plugin.name}'")
        seen.add(plugin.name)
        plugin.register(registry)
        persona_out.extend(plugin.persona_sections)


def _core_tools(cfg) -> list[Tool]:
    from saturday.tools import (
        BrowserTool,
        EditFile,
        GlobTool,
        GrepTool,
        ListDir,
        PythonREPL,
        ReadFile,
        ShellTool,
        WebFetchTool,
        WebSearchTool,
        WriteFile,
    )
    from saturday.tools.jobs import JobManager
    from saturday.tools.recall import MemorySearchTool
    from saturday.tools.vision import ViewImageTool

    root = getattr(cfg, "workspace_root", None)
    timeout = getattr(cfg, "tool_timeout", 120.0)
    verify_cmd = getattr(cfg, "verify_command", "") or ""
    # dynamic wiring: the shell tool reads the CURRENT shell_allow_network
    # setting on every call, so Settings changes apply without an agent rebuild
    shell = ShellTool(
        timeout=timeout,
        root=root,
        job_manager=JobManager.shared(),
        allow_network_fn=lambda: bool(getattr(cfg, "shell_allow_network", True)),
    )
    repl = PythonREPL(timeout=timeout)
    tools: list[Tool] = [
        shell,
        ReadFile(root=root),
        WriteFile(root=root, verify_command=verify_cmd),
        EditFile(root=root, verify_command=verify_cmd),
        ListDir(root=root),
        GlobTool(root=root),
        GrepTool(root=root),
        repl,
        WebFetchTool(),
        WebSearchTool(),
        BrowserTool(),
        ViewImageTool(root=root),
        MemorySearchTool(),
    ]

    from saturday.statemap import StateCache
    from saturday.tools.ocr import UiTextTool
    from saturday.tools.screen import ScreenTool
    from saturday.tools.spatial import (
        AppOpenTool,
        ClipboardTool,
        KeyboardTool,
        LandmarkStore,
        PointerTool,
        UiInvokeTool,
        UiTreeTool,
        WindowTool,
    )

    # desktop suite registers on EVERY platform now: Windows uses the PS/UIA
    # backends, macOS/Linux dispatch to spatial_unix (cliclick/xdotool/wmctrl)
    landmarks = LandmarkStore()
    state_cache = StateCache()  # shared: ui_tree deltas + screenshot frame dedupe
    tools.append(ScreenTool(
        shots_dir=(Path(root) / ".saturday" / "shots") if root else None,
        landmarks=landmarks,
        cache=state_cache,
    ))
    tools.append(UiTreeTool(landmarks=landmarks, cache=state_cache))
    tools.append(PointerTool(landmarks=landmarks))
    tools.append(KeyboardTool())
    tools.append(WindowTool())
    tools.append(ClipboardTool())
    tools.append(UiInvokeTool(restore_focus_after=bool(getattr(cfg, "desktop_background_only", False))))
    tools.append(AppOpenTool())
    tools.append(UiTextTool(landmarks=landmarks))

    try:
        from saturday.tools.browser_playwright import playwright_available

        if playwright_available():
            from saturday.tools.browser_playwright import PlaywrightBrowserTool

            tools.append(PlaywrightBrowserTool())
    except Exception:
        pass

    # lexical repo index (zero-dep retrieval upgrade over plain grep)
    from saturday.tools.repo_index import make_repo_search_tool

    tools.append(make_repo_search_tool(lambda: getattr(cfg, "workspace_root", None) or "."))

    # LSP tools when servers are configured (never required)
    lsp_cfg = getattr(cfg, "lsp_servers", None) or {}
    if isinstance(lsp_cfg, dict) and lsp_cfg:
        try:
            from saturday.tools.lsp import make_lsp_tools

            tools.extend(make_lsp_tools(lsp_cfg, lambda: getattr(cfg, "workspace_root", None) or "."))
        except Exception:
            pass

    from saturday.tools.external_agent import ExternalAgentTool

    tools.append(ExternalAgentTool())
    return tools


def core_plugin(cfg=None) -> Plugin:
    return make_plugin(
        "builtin-core",
        _core_tools(cfg),
        description="file, shell, python, and web primitives",
    )


def learning_plugin() -> Plugin:
    from saturday.tools.skills import build_skill_tools

    _, skill_tools = build_skill_tools()
    return make_plugin(
        "learning",
        list(skill_tools),
        description="skills self-improvement loop (save/load/index procedures)",
        persona_sections=[
            "# Skills loop\n"
            "- Before inventing a procedure, check `skills_index` and load a matching one with `skill_load`.\n"
            "- After completing something non-obvious and reusable, capture it with `skill_save` "
            "(reuse the same id to improve an existing skill)."
        ],
    )


def workflow_plugin() -> Plugin:
    from saturday.agent.todo import TodoTool
    from saturday.tools.goals import build_goal_tools
    from saturday.tools.jobs import JobManager, make_job_tools
    from saturday.tools.memory import MemoryTool

    todo = TodoTool()
    _, goal_tools = build_goal_tools()
    job_tools = make_job_tools(JobManager.shared())
    return make_plugin(
        "workflow",
        [todo] + goal_tools + job_tools + [MemoryTool()],
        description="todo planning, goal tracking, background jobs, persistent memory",
        persona_sections=[
            "# Workflow tools\n"
            "- Maintain your plan with the `todo` tool; mark steps done as you complete them.\n"
            "- Record the session objective with `create_goal` and keep its status updated.\n"
            "- Persist durable facts (user preferences, project decisions) via the `memory` tool."
        ],
    )
