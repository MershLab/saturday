"""Regression: GUI-spawning shell commands must never hang the tool past its timeout.

Root cause: `cmd /c start <gui>` leaves conhost/notepad holding our stdout pipes;
subprocess.run's post-timeout drain blocks forever. Fixed via Job-Object tree kill +
bounded drain + abandoning unread streams instead of closing them."""
from __future__ import annotations

import sys
import time

import pytest

from saturday.tools.shell import ShellTool


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows pipe/conhost semantics")
def test_gui_spawn_command_returns_within_timeout():
    tool = ShellTool(timeout=6.0)
    t0 = time.time()
    ok, out = tool.run({"command": "cmd /c start notepad"})
    elapsed = time.time() - t0
    try:
        import subprocess

        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)
    except Exception:
        pass
    assert ok, out
    assert elapsed < 20, f"shell tool hung {elapsed:.1f}s on GUI-spawning command"
    assert "timed out after 6" in out


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-specific")
def test_winjob_assign_and_terminate():
    import subprocess as sp

    from saturday.tools.winjob import JobObject

    proc = sp.Popen("cmd /c ping -n 30 127.0.0.1", shell=True, stdout=sp.PIPE, stderr=sp.PIPE)
    job = JobObject()
    job.assign(proc.pid)
    job.terminate()
    try:
        proc.communicate(timeout=5)
    except sp.TimeoutExpired:
        proc.kill()
        proc.communicate()
    assert proc.returncode != 0
