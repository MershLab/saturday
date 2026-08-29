from __future__ import annotations

import sys
from pathlib import Path

from saturday.tools.base import Tool, ToolRegistry, ToolSpec
from saturday.tools.files import EditFile, GlobTool, GrepTool, ListDir, ReadFile, WriteFile
from saturday.tools.python_repl import PythonREPL
from saturday.tools.shell import ShellTool
from saturday.tools.vision import ViewImageTool
from saturday.tools.web import BrowserTool, WebFetchTool, WebSearchTool

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolSpec",
    "ShellTool",
    "ReadFile",
    "WriteFile",
    "EditFile",
    "ListDir",
    "GlobTool",
    "GrepTool",
    "PythonREPL",
    "WebFetchTool",
    "WebSearchTool",
    "BrowserTool",
    "ViewImageTool",
    "AskUserTool",
]


def default_registry(cfg=None) -> ToolRegistry:
    from saturday.tools.jobs import JobManager, make_job_tools

    reg = ToolRegistry()
    # shared singleton, same as the core plugin: a separate manager here made
    # `saturday tools`/doctor report jobs that real sessions could never see
    manager = JobManager.shared()
    verify_cmd = getattr(cfg, "verify_command", "") or ""
    reg.register(ShellTool(timeout=getattr(cfg, "tool_timeout", 120.0), root=getattr(cfg, "workspace_root", None), job_manager=manager))
    reg.register(ReadFile(root=getattr(cfg, "workspace_root", None)))
    reg.register(WriteFile(root=getattr(cfg, "workspace_root", None), verify_command=verify_cmd))
    reg.register(EditFile(root=getattr(cfg, "workspace_root", None), verify_command=verify_cmd))
    reg.register(ListDir(root=getattr(cfg, "workspace_root", None)))
    reg.register(GlobTool(root=getattr(cfg, "workspace_root", None)))
    reg.register(GrepTool(root=getattr(cfg, "workspace_root", None)))
    reg.register(PythonREPL())
    reg.register(WebFetchTool())
    reg.register(WebSearchTool())
    reg.register(BrowserTool())
    reg.register(ViewImageTool(root=getattr(cfg, "workspace_root", None)))

    # Lovable/Windsurf-style clarifying questions (web surface resolves them)
    from saturday.tools.ask import AskUserTool

    reg.register(AskUserTool())

    from saturday.tools.screen import ScreenTool

    landmarks = None
    if sys.platform.startswith("win"):
        from saturday.tools.spatial import LandmarkStore

        landmarks = LandmarkStore()

    reg.register(ScreenTool(shots_dir=(Path(getattr(cfg, "workspace_root", ".")) / ".saturday" / "shots"), landmarks=landmarks))

    if landmarks is not None:
        from saturday.tools.spatial import (
            AppOpenTool,
            ClipboardTool,
            KeyboardTool,
            PointerTool,
            UiInvokeTool,
            UiTreeTool,
            WindowTool,
        )

        reg.register(UiTreeTool(landmarks=landmarks))
        reg.register(PointerTool(landmarks=landmarks))
        reg.register(KeyboardTool())
        reg.register(WindowTool())
        reg.register(ClipboardTool())
        reg.register(UiInvokeTool(restore_focus_after=bool(getattr(cfg, "desktop_background_only", False))))
        reg.register(AppOpenTool())

    try:
        from saturday.tools.browser_playwright import playwright_available

        if playwright_available():
            from saturday.tools.browser_playwright import PlaywrightBrowserTool

            reg.register(PlaywrightBrowserTool())
    except Exception:
        pass

    # lexical repo index (zero-dep retrieval upgrade over plain grep)
    from saturday.tools.repo_index import make_repo_search_tool

    reg.register(make_repo_search_tool(lambda: getattr(cfg, "workspace_root", None) or "."))

    # LSP tools when servers are configured (never required)
    lsp_cfg = getattr(cfg, "lsp_servers", None) or {}
    if isinstance(lsp_cfg, dict) and lsp_cfg:
        try:
            from saturday.tools.lsp import make_lsp_tools

            for lt in make_lsp_tools(lsp_cfg, lambda: getattr(cfg, "workspace_root", None) or "."):
                reg.register(lt)
        except Exception:
            pass

    for jt in make_job_tools(manager):
        reg.register(jt)
    return reg
