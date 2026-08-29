"""File-edit journal: Cursor/Windsurf-style checkpoint & undo for agent edits.

Every write_file/edit_file appends the PREVIOUS on-disk content (capped) to
``<workspace>/.saturday/file_journal.jsonl`` BEFORE mutating, so any agent
edit can be reverted exactly — even across restarts. ``/revert`` in the REPL
and web UI restores from this journal. Stdlib-only."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from saturday.tools.files import is_privileged_path

JOURNAL_NAME = "file_journal.jsonl"
MAX_BEFORE_CHARS = 200_000
MAX_ENTRIES = 500


def journal_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / ".saturday" / JOURNAL_NAME


def record_edit(workspace_root: str | Path, tool: str, path: str) -> None:
    """Snapshot the CURRENT content of path (before an impending overwrite)."""
    root = Path(workspace_root)
    target = Path(path)
    try:
        before = target.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        before = None  # file did not exist (a create, not a modify)
    entry = {
        "ts": time.time(),
        "tool": tool,
        "path": str(target),
        "existed": before is not None,
    }
    if before is not None:
        entry["before"] = before[:MAX_BEFORE_CHARS]
        entry["before_truncated"] = len(before) > MAX_BEFORE_CHARS
    try:
        jp = journal_path(root)
        jp.parent.mkdir(parents=True, exist_ok=True)
        with jp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # process-kill durability is implied by the OS; fsync covers
            # power-loss so a checkpoint's journal_len never points past
            # entries that vanished from disk
            fh.flush()
            os.fsync(fh.fileno())
        _prune(jp)
    except OSError:
        pass  # journaling must never break the edit itself


def _prune(jp: Path) -> None:
    try:
        lines = jp.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_ENTRIES:
            jp.write_text("\n".join(lines[-MAX_ENTRIES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def journal_length(workspace_root: str | Path) -> int:
    """Number of valid journal entries (checkpoint metadata: 'files as of
    entry N'). Journals are pruned to MAX_ENTRIES, so this read is cheap."""
    try:
        lines = journal_path(workspace_root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    n = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            if isinstance(json.loads(line), dict):
                n += 1
        except json.JSONDecodeError:
            continue
    return n


def _privileged_target(resolved: Path, allowed: Path) -> bool:
    """True when a journal restore would rewrite Saturday's own state files.

    Journal entries are model-influenced data (paths come from tool args), so
    a poisoned entry must not be able to plant content into files like
    ``.saturday/hooks.json`` (shell commands on every tool call) or
    ``config.json`` via /revert. Paths outside ``allowed`` are rejected by
    the caller's workspace-bounds check; treat them as privileged here too."""
    try:
        rel = resolved.relative_to(allowed).as_posix()
    except ValueError:
        return True
    return is_privileged_path(rel)


def restore_to_length(workspace_root: str | Path, target_len: int, root: str | Path | None = None) -> tuple[bool, str]:
    """Undo every edit NEWER than entry ``target_len`` (Cursor-style rewind to
    checkpoint state): files return to their pre-edit content, entries that
    were creations get deleted. Each inverse is itself journaled first, so a
    rewind is repeatable / re-doable via /revert. Aborts BEFORE touching
    anything when any newer snapshot is truncated (>200k chars)."""
    allowed = Path(root if root is not None else workspace_root).resolve()
    try:
        lines = journal_path(workspace_root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return True, "nothing to rewind (no journal yet)"
    entries: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            entries.append(rec)
    newer = entries[max(0, int(target_len)):]
    # NOTE: _prune keeps the last MAX_ENTRIES lines, so once a journal exceeds
    # 500 entries absolute positions shift and a pre-prune target_len points
    # into the wrong window (rewind then restores fewer changes). Within a
    # normal session window this is exact.
    if not newer:
        return True, "nothing to rewind (journal already at target)"
    # preflight: refuse rather than half-rewind on an unrecoverable snapshot
    for rec in newer:
        if rec.get("existed") and rec.get("before_truncated"):
            return False, (
                f"cannot rewind past {rec.get('path')!r}: snapshot truncated "
                "(original >200k chars); use selective /revert instead"
            )
        try:
            resolved = Path(rec["path"]).resolve()
        except (OSError, ValueError):
            continue
        if _privileged_target(resolved, allowed):
            return False, (
                f"cannot rewind past {rec.get('path')!r}: it targets a privileged "
                "Saturday state file; restore it manually if needed"
            )
    undone = 0
    for rec in reversed(newer):  # newest first, mirroring /revert order
        target = Path(rec["path"])
        try:
            resolved = target.resolve()
        except (OSError, ValueError):
            continue
        if resolved != allowed and allowed not in resolved.parents:
            continue  # outside the workspace: never touch
        record_edit(workspace_root, "rewind", str(target))  # journal current state
        try:
            if rec.get("existed"):
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(rec["before"], encoding="utf-8")
            else:
                resolved.unlink(missing_ok=True)
            undone += 1
        except OSError as exc:
            return False, f"rewound {undone} before failing on {target.name}: {exc}"
    label = f"entries {target_len}..{len(entries) - 1}"
    return True, f"rewound {undone} file changes ({label}); workspace restored"


def load_entries(workspace_root: str | Path, limit: int = 10) -> list[dict]:
    """Most recent revertable entries last-edit-first. Includes creation
    tombstones (``existed: false``): reverting a create deletes the file."""
    try:
        lines = journal_path(workspace_root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and ("before" in rec or rec.get("existed") is False):
            out.append(rec)
        if len(out) >= limit:
            break
    return out


def restore_entry(workspace_root: str | Path, index: int, root: str | Path | None = None) -> tuple[bool, str]:
    """Restore entry ``index`` (as shown by load_entries, 0 = most recent).

    ``root`` bounds where a restore may write; when omitted the workspace root
    is used. Journal entries are model-influenced data, so a poisoned entry
    must not be able to overwrite files outside the workspace (or, say, the
    journal itself). The restored state is itself journaled first, so /revert
    is repeatable (undo of undo). Returns (ok, message)."""
    entries = load_entries(workspace_root, limit=MAX_ENTRIES)
    if index < 0 or index >= len(entries):
        return False, f"no journal entry {index} (0..{len(entries) - 1})"
    rec = entries[index]
    if rec.get("existed") and rec.get("before_truncated"):
        # WHY: writing back the capped "before" would silently destroy most of
        # the original file — refusing beats partial recovery
        return False, (
            f"cannot restore {rec['path']}: snapshot truncated; "
            f"original >200k chars not recoverable"
        )
    allowed = Path(root if root is not None else workspace_root).resolve()
    try:
        target = Path(rec["path"]).resolve()
    except (OSError, ValueError) as exc:
        return False, f"bad journal path {rec['path']!r}: {exc}"
    if target != allowed and allowed not in target.parents:
        return False, f"refusing to restore outside the workspace: {target}"
    if _privileged_target(target, allowed):
        return False, (
            f"refusing to restore {target}: it is a privileged Saturday state "
            "file; edit it manually if needed"
        )
    record_edit(workspace_root, "revert", str(target))  # journal current state
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if rec.get("existed"):
            target.write_text(rec["before"], encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    label = rec["path"]
    return True, f"restored {label} to pre-{rec['tool']} state"
