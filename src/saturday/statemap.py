"""Persistent element map + diff observations (world-model principle).

A world model reasons over persistent state instead of re-reading the world
every step. The desktop's ground truth is the accessibility tree, so the
cheap equivalent is: keep the last scanned element set per scope, answer
"what changed since I last looked", and dedupe unchanged screenshot frames by
content hash — the model then pays for deltas, not full re-observations.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def element_identity(e: dict) -> tuple[str, str]:
    """(role, name) — the identity an element keeps when it moves."""
    return (str(e.get("t") or ""), str(e.get("n") or "").strip().lower())


def element_box(e: dict) -> tuple[int, int, int, int]:
    return (int(e.get("x") or 0), int(e.get("y") or 0), int(e.get("w") or 0), int(e.get("h") or 0))


def compute_delta(previous: dict[tuple[str, str], dict] | None, current: list[dict]) -> dict[str, list[dict]]:
    """Classify a fresh scan vs the cached one. Returns added/removed/changed."""
    cur = {element_identity(e): e for e in current}
    old = previous or {}
    added = [e for ident, e in cur.items() if ident not in old]
    removed = [e for ident, e in old.items() if ident not in cur]
    changed = [
        e
        for ident, e in cur.items()
        if ident in old and element_box(old[ident]) != element_box(e)
    ]
    return {"added": added, "removed": removed, "changed": changed}


class StateCache:
    """Session-scoped memory of scans and frames (one per Agent)."""

    def __init__(self) -> None:
        self._scans: dict[str, dict[tuple[str, str], dict]] = {}
        self._frames: dict[str, tuple[str, str]] = {}  # key -> (sha256, path)

    # -- element scans --------------------------------------------------------

    def last_scan(self, scope: str) -> dict[tuple[str, str], dict]:
        return self._scans.get(scope, {})

    def put_scan(self, scope: str, elements: list[dict]) -> dict[str, list[dict]]:
        """Store a scan and report what changed vs the previous one."""
        delta = compute_delta(self._scans.get(scope), elements)
        self._scans[scope] = {element_identity(e): e for e in elements}
        return delta

    def scan_counts(self, scope: str) -> int:
        return len(self._scans.get(scope, {}))

    # -- screenshot frames ----------------------------------------------------

    @staticmethod
    def digest(path: Path) -> str:
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
        except OSError:
            return ""

    def frame_digest(self, key: str, path: Path) -> tuple[str, str]:
        """(sha256, path) for a capture, updating the cache entry."""
        d = self.digest(path)
        self._frames[key] = (d, str(path))
        return d, str(path)

    def frame_unchanged(self, key: str, path: Path) -> bool:
        """True when this capture's content equals the last one for key."""
        d = self.digest(path)
        entry = self._frames.get(key)
        if entry and entry[0] == d:
            return True
        self._frames[key] = (d, str(path))
        return False
