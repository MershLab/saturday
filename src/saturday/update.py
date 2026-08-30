"""Self-update: version check, install-channel detection, and a delegated
update per channel - pip/deb/rpm/AppImage/pacman/the Windows installer all
update differently, so there's no single generic "update yourself" action.

No pip/requests dependency: GitHub's releases API is one GET, same pattern
as the LLM connection probe in llm/probe.py."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

RELEASES_API = "https://api.github.com/repos/MershLab/saturday/releases/latest"


def current_version() -> str:
    from saturday import __version__

    return __version__


def _parse_version(v: str) -> tuple[int, ...]:
    v = v.lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return _parse_version(candidate) > _parse_version(current)


def latest_release(timeout: float = 8.0) -> dict[str, Any] | None:
    """Never raises. None on any failure - network down, rate limited,
    malformed response, all read the same to a caller: nothing to report."""
    try:
        with urllib.request.urlopen(
            urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json"}),
            timeout=timeout,
        ) as resp:
            raw = resp.read(1024 * 1024)
    except Exception:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        return None
    assets = [a.get("name") for a in data.get("assets", []) if isinstance(a, dict)]
    return {"tag": tag, "url": data.get("html_url", ""), "assets": assets}


def _pkg_query(cmd: list[str]) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def detect_channel() -> str:
    """Best-effort: how this instance is actually running right now, not
    how it was originally built - the same wheel can end up under pip or
    pipx, so this checks the live process, not a compile-time flag."""
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            return "macos-dmg"
        if sys.platform.startswith("win"):
            return "windows-installer"
        if os.environ.get("APPIMAGE"):
            return "appimage"
        if shutil.which("dpkg") and _pkg_query(["dpkg", "-s", "saturday"]):
            return "deb"
        if shutil.which("rpm") and _pkg_query(["rpm", "-q", "saturday"]):
            return "rpm"
        if shutil.which("pacman") and _pkg_query(["pacman", "-Qi", "saturday"]):
            return "pacman"
        return "linux-bundle-unknown"
    exe = sys.executable.replace("\\", "/")
    if os.environ.get("PIPX_HOME") or "/pipx/" in exe:
        return "pipx"
    return "pip"


_MANUAL_HINTS = {
    "deb": "sudo apt-get install --only-upgrade saturday  (or download the new .deb from the GitHub release)",
    "rpm": "sudo dnf upgrade saturday  (or download the new .rpm from the GitHub release)",
    "pacman": "download the updated package from the GitHub release, reinstall with pacman -U",
    "appimage": "download the new AppImage from the GitHub release and replace the existing file",
    "windows-installer": "download and run the new installer from the GitHub release - it installs over the existing copy",
    "macos-dmg": "download the new .dmg from the GitHub release and drag-replace the app",
    "linux-bundle-unknown": "download the matching installer from the GitHub release",
}


def manual_update_hint(channel: str) -> str:
    return _MANUAL_HINTS.get(channel, "download the new release from https://github.com/MershLab/saturday/releases")


def perform_update(channel: str) -> tuple[bool, str]:
    """Only actually applies the update for channels where that's safe
    without privilege escalation or replacing a running binary out from
    under itself - pip/pipx. Everything else gets the exact manual command
    instead of a silent no-op or an unwanted sudo prompt."""
    if channel == "pip":
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "saturday"]
    elif channel == "pipx":
        cmd = ["pipx", "upgrade", "saturday"]
    else:
        return False, manual_update_hint(channel)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"update command failed to run: {exc}"
    if r.returncode != 0:
        return False, f"update failed (exit {r.returncode}): {(r.stderr or r.stdout)[-500:]}"
    return True, "updated"


class UpdateInProgress(Exception):
    pass


def _lock_path() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "update.lock"


@contextmanager
def update_lock() -> Iterator[None]:
    """Mutually exclusive: a scheduled check and a manual `saturday update`
    firing at once must not race each other. A lock left by a dead pid
    (crashed mid-update) doesn't block forever - reclaimed, not respected."""
    from saturday.sessions import _pid_alive

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("pid")
        except (OSError, json.JSONDecodeError):
            pid = None
        if isinstance(pid, int) and _pid_alive(pid):
            raise UpdateInProgress(f"another update is already running (pid {pid})")
    path.write_text(json.dumps({"pid": os.getpid(), "started": time.time()}), encoding="utf-8")
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def record_receipt(*, from_version: str, to_version: str, channel: str, ok: bool, detail: str) -> None:
    from saturday.config import get_config_dir

    path = get_config_dir() / "update-log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": time.time(),
        "from": from_version,
        "to": to_version,
        "channel": channel,
        "ok": ok,
        "detail": detail[:2000],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def relaunch() -> None:
    """Re-exec this process with the same args - the freshly-installed
    version takes over in place; nobody has to notice a new version landed
    and manually restart it."""
    os.execv(sys.executable, [sys.executable, "-m", "saturday"] + sys.argv[1:])
