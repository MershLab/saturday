"""Pre-execution snapshot of destructible files (databases first).

When a shell command looks destructive, every referenced existing file with a
database-ish extension (*.db, *.sqlite, *.sqlite3, *.mdb, *.accdb) — including
wildcard targets like ``data/*.db`` — is copied into ``<workdir>/.saturday/backup/``
before the command runs. This is the seatbelt under "the agent deleted my
database": even an approved or unguarded deletion stays recoverable.

Caps: files larger than 64 MB are skipped (noted), and only the most recent
GUARDRAIL_BACKUP_KEEP backups are retained.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

from saturday.safety import DB_FILE_EXTS, DESTRUCTIVE_CMD_RX

BACKUP_DIR = ".saturday/backup"
MAX_BACKUP_FILE_BYTES = 64 * 1024 * 1024
GUARDRAIL_BACKUP_KEEP = 10


def _candidate_paths(command: str, wd: Path):
    """Yield candidate Paths from quoted/unquoted/wildcarded tokens."""
    for tok in re.findall(r"\"([^\"]+)\"|'([^']+)'|([^;&|\s]+)", command):
        t = next((g for g in tok if g), "").strip().strip("\"'")
        if not t or t.startswith("-") or len(t) < 4:
            continue
        if "*" in t or "?" in t:
            parent = Path(t.split("*")[0].split("?")[0])
            parent = parent.parent if parent.suffix else parent
            pattern = Path(t).name
            base = parent if parent.is_absolute() else wd / parent
            try:
                yield from sorted(base.glob(pattern))
            except OSError:
                continue
            continue
        p = Path(t)
        if not p.is_absolute():
            p = wd / p
        yield p


def backup_destructible_targets(command: str, wd: Path) -> list[str]:
    """Snapshot db-like targets of a destructive command; returns note lines."""
    if not DESTRUCTIVE_CMD_RX.search(command or ""):
        return []
    notes: list[str] = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    seen: set[Path] = set()
    for p in _candidate_paths(command, wd):
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen or rp.suffix.lower() not in DB_FILE_EXTS or not rp.is_file():
            continue
        seen.add(rp)
        try:
            if rp.stat().st_size > MAX_BACKUP_FILE_BYTES:
                notes.append(f"[guardrail] skipped backup (file too large): {rp}")
                continue
            bdir = wd / BACKUP_DIR
            bdir.mkdir(parents=True, exist_ok=True)
            # disambiguate same-basename files and same-second commands
            digest = hashlib.sha1(str(rp).lower().encode("utf-8")).hexdigest()[:8]
            dest = bdir / f"{stamp}_{digest}_{rp.name}"
            shutil.copy2(rp, dest)
            notes.append(f"[guardrail] backed up {rp.name} -> {dest} (recoverable there)")
        except OSError as exc:
            notes.append(f"[guardrail] backup failed for {rp}: {exc}")
    if seen:
        _prune(wd / BACKUP_DIR)
    return notes


def _prune(bdir: Path) -> None:
    try:
        backups = sorted(bdir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in backups[GUARDRAIL_BACKUP_KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
