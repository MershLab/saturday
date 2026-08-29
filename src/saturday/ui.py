from __future__ import annotations

import itertools
import os
import sys
import threading
import time


def enable_ansi() -> None:
    if os.name == "nt":
        os.system("")


def _color_ok() -> bool:
    return bool(sys.stdout.isatty())


CODES = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "red": "\x1b[31m",
    "yellow": "\x1b[33m",
    "reset": "\x1b[0m",
}


def paint(text: str, *names: str) -> str:
    if not _color_ok():
        return text
    prefix = "".join(CODES[n] for n in names if n in CODES)
    return f"{prefix}{text}{CODES['reset']}"


class Spinner:
    FRAMES = "|/-\\"

    def __init__(self, label: str = "working", interval: float = 0.12) -> None:
        self.label = label
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stdout.write(f"\r{paint(frame + ' ' + self.label, 'dim')}   ")
            sys.stdout.flush()
            time.sleep(self.interval)

    def start(self) -> "Spinner":
        if not _color_ok():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            sys.stdout.write("\r" + " " * (len(self.label) + 12) + "\r")
            sys.stdout.flush()
        except OSError:
            pass

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False
