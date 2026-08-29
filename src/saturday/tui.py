from __future__ import annotations

import shutil
import sys

from saturday.ui import paint

ALT_ENTER = "\x1b[?1049h"
ALT_EXIT = "\x1b[?1049l"


def terminal_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


def status_line(agent, session_id: str | None = None) -> str:
    cfg = getattr(agent, "cfg", None)
    model = (getattr(cfg, "model", None) or "?") if cfg else "?"
    provider = (getattr(cfg, "provider", None) or "?") if cfg else "?"
    mem = len(getattr(agent, "memory", []) or [])
    tools = 0
    try:
        reg = agent._build_registry()
        tools = len(reg.names())
    except Exception:
        pass
    parts = [f" {provider}:{model}", f"tools {tools}", f"memory {mem}"]
    if session_id:
        parts.append(f"session {session_id}")
    return paint(" ┃ ".join(parts), "dim")


def header(title: str = " Saturday ") -> str:
    width = terminal_width()
    line = title.center(width, "═")
    return paint(line, "cyan")


def rule() -> str:
    return paint("─" * terminal_width(), "dim")


def render_frame(header_text: str) -> str:
    return "\r" + header_text


def enter_alt_screen() -> None:
    if sys.stdout.isatty():
        sys.stdout.write(ALT_ENTER)
        sys.stdout.flush()


def exit_alt_screen() -> None:
    if sys.stdout.isatty():
        sys.stdout.write(ALT_EXIT)
        sys.stdout.flush()
