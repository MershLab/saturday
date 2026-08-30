from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
AUDIT_SCHEMA = 1


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def record_hash(prev_hash: str, record: dict[str, Any]) -> str:
    """SHA-256 commitment: prev_hash + canonical record (hash field excluded)."""
    payload = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256((prev_hash + canonical_json(payload)).encode("utf-8")).hexdigest()


def verify_chain(records: list[dict[str, Any]], seed: str = GENESIS_HASH) -> dict[str, Any]:
    """Verify the hash chain over appended records.

    Legacy records (pre-chain, no ``hash`` field) are committed as a block: the
    first hashed record's chain seeds from their combined content, so tampering
    with legacy history still breaks the chain. Returns a status dict."""
    prev = seed
    legacy: list[int] = []
    legacy_payload: list[str] = []
    for i, rec in enumerate(records):
        h = rec.get("hash")
        if not h:
            legacy.append(i)
            legacy_payload.append(canonical_json({k: v for k, v in rec.items() if k != "hash"}))
            continue
        if legacy_payload:
            prev = record_hash(prev, {"legacy": legacy_payload})
            legacy_payload = []
        expected = record_hash(prev, rec)
        if expected != h:
            return {
                "ok": False,
                "broken_at": i,
                "records": len(records),
                "hashed": sum(1 for r in records if r.get("hash")),
                "legacy": len(legacy),
                "schema": AUDIT_SCHEMA,
            }
        prev = h
    if legacy_payload:
        prev = record_hash(prev, {"legacy": legacy_payload})
    return {
        "ok": True,
        "broken_at": None,
        "records": len(records),
        "hashed": sum(1 for r in records if r.get("hash")),
        "legacy": len(legacy),
        "head": prev,
        "schema": AUDIT_SCHEMA,
    }


class SessionStore:
    """JSONL session persistence, mirroring dsh's default session-persistence-jsonl.

    Appended records carry a SHA-256 hash chain (each record commits to the
    previous), making the session history tamper-evident: any edit to a past
    record breaks verification. The initial meta header is outside the chain;
    mutable metadata is stored in an atomic sidecar so renames and project
    re-tags never rewrite transcript records."""

    _write_locks: dict[str, threading.RLock] = {}
    _write_locks_guard = threading.Lock()

    @classmethod
    def _lock_for_root(cls, root: Path) -> threading.RLock:
        try:
            key = str(root.resolve()).lower()
        except OSError:
            key = str(root).lower()
        with cls._write_locks_guard:
            lock = cls._write_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._write_locks[key] = lock
            return lock

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            from saturday.config import CONFIG_DIR

            root = CONFIG_DIR / "sessions"
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # path -> (mtime_ns, size, chain_head): appends are O(1) instead of
        # re-reading the whole file per record; the stat guard keeps external
        # edits / meta rewrites (rename, set_project) correct.
        self._head_cache: dict[Path, tuple[int, int, str]] = {}
        # path -> (main + sidecar stat stamp, meta_dict): read_meta/list_sessions
        # only need the header and optional sidecar; without this cache every
        # sidebar refresh re-opened EVERY session file (~1s for 150 files).
        self._meta_cache: dict[Path, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        # All stores pointing at the same root share this lock. Metadata
        # updates must not race with appends, even when the web UI and a
        # background helper each construct their own SessionStore instance.
        self._append_lock = self._lock_for_root(self.root)

    def _path(self, session_id: str) -> Path:
        safe = "".join(c for c in str(session_id) if c.isalnum() or c in "-_")
        if len(safe) > 64:
            import hashlib

            digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
            safe = safe[:55] + "-" + digest
        return self.root / f"{safe or 'session'}.jsonl"

    def create(self, meta: dict[str, Any]) -> str:
        with self._append_lock:
            session_id = meta.get("id") or time.strftime("%Y%m%d-%H%M%S")
            p = self._path(session_id)
            n = 2
            while p.exists():
                trimmed = re.sub(r"-\d+$", "", session_id)
                session_id = f"{trimmed}-{n}"
                p = self._path(session_id)
                n += 1
            header = {"type": "meta", "id": session_id, "created": time.time(), **{k: v for k, v in meta.items() if k != "id"}}
            p.write_text(json.dumps(header) + "\n", encoding="utf-8")
            stamp = self._metadata_stamp(p)
            if stamp is not None:
                with self._cache_lock:
                    self._meta_cache[p] = (stamp, header)
            return session_id

    def _compute_chain_head(self, p: Path) -> str:
        try:
            raw_lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            return GENESIS_HASH
        # Per-line parse (mirrors load()): a single torn trailing line must
        # not reset the whole chain head to GENESIS — that would make the
        # next append chain onto the wrong hash.
        lines: list[dict[str, Any]] = []
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                lines.append(obj)
        records = [r for r in lines[1:] if r.get("type") != "meta"]
        hashed = [r for r in records if r.get("hash")]
        if hashed:
            return str(hashed[-1]["hash"])
        if records:
            legacy = [canonical_json({k: v for k, v in r.items() if k != "hash"}) for r in records]
            return record_hash(GENESIS_HASH, {"legacy": legacy})
        return GENESIS_HASH

    def _chain_head(self, p: Path) -> str:
        try:
            st = p.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            with self._cache_lock:
                self._head_cache.pop(p, None)
            return GENESIS_HASH
        with self._cache_lock:
            cached = self._head_cache.get(p)
        if cached is not None and cached[0] == stamp[0] and cached[1] == stamp[1]:
            return cached[2]
        head = self._compute_chain_head(p)
        with self._cache_lock:
            self._head_cache[p] = (stamp[0], stamp[1], head)
        return head

    def append(self, session_id: str, record: dict[str, Any]) -> None:
        p = self._path(session_id)
        with self._append_lock:  # head-compute + write must be one atomic step
            if not p.is_file():
                p.write_text(
                    json.dumps({"type": "meta", "id": session_id, "created": time.time(), "implicit": True}) + "\n",
                    encoding="utf-8",
                )
            prev = self._chain_head(p)
            stamped = {"ts": time.time(), **record}
            stamped["hash"] = record_hash(prev, stamped)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(stamped, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # chain records survive power loss too
            try:
                st = p.stat()
                with self._cache_lock:
                    self._head_cache[p] = (st.st_mtime_ns, st.st_size, stamped["hash"])
            except OSError:
                pass

    def audit_verify(self, session_id: str) -> dict[str, Any] | None:
        """Verify the tamper-evident chain of a stored session."""
        data = self.load(session_id)
        if data is None:
            return None
        return verify_chain(data["records"])

    def audit_export(self, session_id: str) -> dict[str, Any] | None:
        """Full audit bundle: meta + records + chain verification status."""
        data = self.load(session_id)
        if data is None:
            return None
        return {
            "schema": AUDIT_SCHEMA,
            "session_id": session_id,
            "meta": data["meta"],
            "records": data["records"],
            "chain": verify_chain(data["records"]),
        }

    @staticmethod
    def _peek_first_line(p: Path, cap: int = 262_144) -> str:
        """First line only — meta headers live on line 1 and transcripts can
        be large, so never read whole files just for their meta."""
        try:
            with p.open("r", encoding="utf-8") as fh:
                return fh.readline(cap)
        except OSError:
            return ""

    @staticmethod
    def _meta_path(p: Path) -> Path:
        return p.with_suffix(".meta.json")

    @classmethod
    def _metadata_stamp(cls, p: Path) -> tuple[int, int, int, int] | None:
        try:
            st = p.stat()
        except OSError:
            return None
        try:
            mst = cls._meta_path(p).stat()
            return st.st_mtime_ns, st.st_size, mst.st_mtime_ns, mst.st_size
        except OSError:
            return st.st_mtime_ns, st.st_size, 0, 0

    @classmethod
    def _read_meta_override(cls, p: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(cls._meta_path(p).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def _write_meta_override(cls, p: Path, meta: dict[str, Any]) -> None:
        sidecar = cls._meta_path(p)
        tmp = sidecar.with_name(f"{sidecar.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(sidecar)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _meta_for_path(self, p: Path) -> dict[str, Any] | None:
        """Cached first-line meta for a known path (stat-stamp validated)."""
        stamp = self._metadata_stamp(p)
        if stamp is None:
            return None
        with self._cache_lock:
            cached = self._meta_cache.get(p)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            meta = json.loads(self._peek_first_line(p))
        except (json.JSONDecodeError, OSError, ValueError):
            return None
        if isinstance(meta, dict):
            override = self._read_meta_override(p)
            if override is not None:
                meta = override
            with self._cache_lock:
                self._meta_cache[p] = (stamp, meta)
            return meta
        return None

    def read_meta(self, session_id: str) -> dict[str, Any] | None:
        """First-line meta only (cheap; does not parse the whole file)."""
        p = self._path(session_id)
        if not p.is_file():
            return None
        return self._meta_for_path(p)

    def set_project(self, session_id: str, project_id: str) -> bool:
        """Tag (or untag with "") a session's metadata."""
        def update(meta: dict[str, Any]) -> None:
            pid = str(project_id or "")
            if pid:
                meta["project"] = pid
            else:
                meta.pop("project", None)
        return self._update_meta(session_id, update)

    def set_archived(self, session_id: str, archived: bool) -> bool:
        """Archive (hide from the default sidebar) or restore a session."""
        def update(meta: dict[str, Any]) -> None:
            if archived:
                meta["archived"] = True
            else:
                meta.pop("archived", None)
        return self._update_meta(session_id, update)

    def set_task(self, session_id: str, title: str) -> bool:
        """Update the session's display title (AI auto-naming)."""
        return self._update_meta(session_id, lambda meta: meta.__setitem__("task", str(title)[:120]))

    def _update_meta(self, session_id: str, update) -> bool:
        p = self._path(session_id)
        with self._append_lock:
            if not p.is_file():
                return False
            meta = self._meta_for_path(p)
            if meta is None:
                return False
            updated = dict(meta)
            update(updated)
            try:
                # Keep the append-only transcript untouched. A sidecar makes
                # metadata changes atomic and prevents a rename/archive race
                # from rewriting away a concurrent message record.
                self._write_meta_override(p, updated)
            except OSError:
                return False
            with self._cache_lock:
                self._meta_cache.pop(p, None)
            return True

    def load(self, session_id: str) -> dict[str, Any] | None:
        p = self._path(session_id)
        if not p.is_file():
            return None
        # Per-line parse: a crash mid-append can leave a torn final line, and
        # one bad line used to poison the WHOLE read (JSONDecodeError => total
        # data loss). Skip corrupt lines and report how many were dropped.
        meta: dict[str, Any] | None = None
        records: list[dict[str, Any]] = []
        skipped = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(rec, dict):
                skipped += 1
                continue
            if meta is None:
                meta = rec  # first record is the meta header (line 1 by convention)
            elif rec.get("type") != "meta":
                records.append(rec)
        if meta is None:
            return None
        override = self._read_meta_override(p)
        if override is not None:
            meta = override
        out: dict[str, Any] = {"meta": meta, "records": records}
        if skipped:
            out["skipped_lines"] = skipped
        return out

    def list_sessions(self, limit: int | None = None) -> list[dict[str, Any]]:
        """All sessions, newest first. UNCAPPED by default — a cap here once
        hid real chats behind a flood of files (landmine #8): hiding user data
        is never an acceptable default. Callers that want pagination can pass
        limit explicitly.

        Ordered by the ``created`` header field, not filesystem mtime: mtime
        resolution varies by OS/filesystem and can tie under rapid creation
        (observed on Windows CI), which broke "newest first" for sessions
        created within the same tick. ``created`` is written under this
        store's append lock, so it is strictly ordered for a given root even
        when mtime is not."""
        entries: list[tuple[float, Path, dict[str, Any]]] = []
        for p in self.root.glob("*.jsonl"):
            first = self._meta_for_path(p)
            if first is None:
                continue
            created = first.get("created")
            sort_key = float(created) if isinstance(created, (int, float)) else p.stat().st_mtime
            entries.append((sort_key, p, first))
        entries.sort(key=lambda e: e[0], reverse=True)
        if limit is not None:
            entries = entries[:limit]
        out = []
        for _, p, first in entries:
            out.append(
                {
                    "id": first.get("id", p.stem),
                    "task": str(first.get("task", ""))[:80],
                    "file": p.name,
                    "project": str(first.get("project", "") or ""),
                    "archived": bool(first.get("archived", False)),
                    "mtime": int(p.stat().st_mtime),
                }
            )
        return out

    def branch(self, session_id: str, keep_messages: int | None = None) -> str | None:
        """Fork a session: new id seeded with the first ``keep_messages``
        messages (default: everything except the final user exchange).

        Copied records keep their original hashes, so the copied prefix still
        verifies as a chain; the fork then appends its own records on top.
        The original session is untouched. Returns the new session id."""
        data = self.load(session_id)
        if not data:
            return None
        msgs: list[dict[str, Any]] = []
        for rec in data["records"]:
            if rec.get("type") == "messages":
                msgs.extend(rec.get("messages") or [])
        if not msgs:
            return None
        if keep_messages is None:
            keep = len(msgs) - 2 if msgs[-1].get("role") == "assistant" else len(msgs) - 1
        else:
            keep = max(0, min(int(keep_messages), len(msgs)))
        keep = max(keep, 1)
        task = str((self.read_meta(session_id) or {}).get("task") or "(branch)")
        new_sid = self.create({"task": f"{task[:70]} (branch)", "branched_from": session_id})
        self.append(new_sid, {"type": "messages", "messages": [dict(m) for m in msgs[:keep]]})
        ckpt = self.load_checkpoint(session_id)
        if ckpt is not None:
            self.save_checkpoint(new_sid, [dict(m) for m in ckpt[:keep]])
        return new_sid

    def history_messages(self, session_id: str) -> list[dict[str, Any]]:
        data = self.load(session_id)
        if not data:
            return []
        msgs: list[dict[str, Any]] = []
        for rec in data["records"]:
            if rec.get("type") == "messages":
                msgs.extend(rec.get("messages") or [])
        return msgs

    def save_checkpoint(self, session_id: str, messages: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> None:
        p = self._path(session_id).with_suffix(".checkpoint.json")
        # Unique tmp per call: concurrent saves sharing ONE fixed tmp path
        # could interleave writes / swap in each other's partial file.
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        payload = json.dumps(
            {"ts": time.time(), "messages": messages, "meta": meta or {}},
            ensure_ascii=False,
        )
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                # power-loss durability: without fsync the rename can hit disk
                # before the data does, restoring an EMPTY/old checkpoint
                os.fsync(fh.fileno())
            tmp.replace(p)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def load_checkpoint(self, session_id: str) -> list[dict[str, Any]] | None:
        data = self._read_checkpoint(session_id)
        return (data.get("messages") or None) if data else None

    def load_checkpoint_meta(self, session_id: str) -> dict[str, Any] | None:
        """Snapshot metadata (journal position, memory, tool state); None for
        legacy checkpoints saved before meta existed."""
        data = self._read_checkpoint(session_id)
        return (data.get("meta") or None) if data else None

    def _read_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        p = self._path(session_id).with_suffix(".checkpoint.json")
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            return None


class EphemeralSessionStore(SessionStore):
    """Drop-everything store for runs whose transcripts must not persist.

    Used by subagents (their work is captured in the parent's trajectory) and
    by tests. Nothing is written to disk and nothing appears in the user's
    session list."""

    def __init__(self) -> None:  # skip SessionStore.__init__: no dirs, no cache
        self.root = Path("-")  # never touched; keeps .root attribute contract
        self._head_cache = {}
        self._cache_lock = threading.Lock()
        self._counter = 0

    def _path(self, session_id: str) -> Path:
        return Path("-")

    def create(self, meta: dict[str, Any]) -> str:
        self._counter += 1
        return f"ephemeral-{uuid.uuid4().hex[:8]}"

    def append(self, session_id: str, record: dict[str, Any]) -> None:
        pass

    def read_meta(self, session_id: str) -> dict[str, Any] | None:
        return None

    def set_project(self, session_id: str, project_id: str) -> bool:
        return False

    def set_archived(self, session_id: str, archived: bool) -> bool:
        return False

    def set_task(self, session_id: str, title: str) -> bool:
        return False

    def load(self, session_id: str) -> dict[str, Any] | None:
        return None

    def list_sessions(self) -> list[dict[str, Any]]:
        return []

    def history_messages(self, session_id: str) -> list[dict[str, Any]]:
        return []

    def save_checkpoint(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        pass

    def load_checkpoint(self, session_id: str) -> list[dict[str, Any]] | None:
        return None
