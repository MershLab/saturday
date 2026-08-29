"""Windowed entry point for the packaged Saturday desktop app (all desktop OSes).

PyInstaller builds this into Saturday.exe / Saturday.app / saturday binaries.
Env knobs keep every bundle testable: SATURDAY_PORT, SATURDAY_TOKEN,
SATURDAY_NO_WINDOW=1. Startup failures land in the per-OS log dir plus a
MessageBox (Windows) / stderr, so a double-click never dies silently.

Per-OS data/log dirs:
  Windows  %LOCALAPPDATA%/Saturday
  macOS    ~/Library/Application Support/Saturday
  Linux    ${XDG_DATA_HOME:-~/.local/share}/saturday
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _log_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Saturday"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Saturday"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share" / "saturday"


def _load_env() -> None:
    """Load .env from the bundle dir, then cwd (cwd wins)."""
    try:
        from saturday.utils.env import load_env_file

        load_env_file(str(_exe_dir() / ".env"))
        load_env_file(None)
    except Exception:
        pass


def _report_crash(log_dir: Path) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "startup-error.log"
        log_file.write_text(traceback.format_exc(), encoding="utf-8")
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                "Saturday failed to start.\n\nDetails were written to:\n" + str(log_file),
                "Saturday",
                0x10,
            )
        else:
            print(f"Saturday failed to start. Details: {log_file}", file=sys.stderr)
    except Exception:
        pass


def main() -> int:
    try:
        _load_env()
        from saturday.webui import DEFAULT_PORT, serve

        port = int(os.environ.get("SATURDAY_PORT") or 0) or DEFAULT_PORT
        open_window = os.environ.get("SATURDAY_NO_WINDOW", "") != "1"
        token = os.environ.get("SATURDAY_TOKEN") or None
        return serve(port=port, open_window=open_window, token=token)
    except Exception:
        _report_crash(_log_dir())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
