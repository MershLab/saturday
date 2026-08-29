"""Windows Job Object wrapper: deterministic descendant cleanup for tool calls.

`cmd /c start notepad`-style commands leave grandchildren holding our stdout
pipes after the shell exits; taskkill /T fails once the intermediate process is
gone. Assigning the child to a Job tracks every descendant, so terminate()
releases the pipes deterministically. Stdlib-only (ctypes)."""
from __future__ import annotations

import ctypes

JobObjectExtendedLimitInformation = 9


class JobObject:
    """Job handle whose descendants can be terminated deterministically.

    Deliberately NOT kill-on-close: apps launched via `start` must outlive the
    shell call that spawned them. Termination happens only via terminate()."""

    def __init__(self) -> None:
        self._kernel32 = ctypes.windll.kernel32
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("CreateJobObjectW failed")

    def assign(self, pid: int) -> None:
        PROCESS_ALL_ACCESS = 0x1F0FFF
        handle = None
        try:
            handle = self._kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
            if not handle:
                raise OSError(f"OpenProcess({pid}) failed")
            if not self._kernel32.AssignProcessToJobObject(self._handle, handle):
                raise OSError(f"AssignProcessToJobObject({pid}) failed")
        finally:
            if handle:
                self._kernel32.CloseHandle(handle)

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
