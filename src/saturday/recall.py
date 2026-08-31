"""Cross-session semantic recall (Hermes parity: FTS5 session search).

The agent's working memory dies with the run. This module keeps a local
SQLite FTS5 index over every session transcript so ``memory_search`` can
recall what happened in PAST conversations. The index is rebuilt lazily when
the session directory's newest file is newer than the last index (no
hot-path writes); builds without FTS5 degrade to a LIKE scan over the same
rows. Stdlib-only.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

_INDEXED_ROLES = ("user", "assistant")
# tool-result lines are noise for recall: they are long, repetitive and
# never useful as a memory key
_TOOL_PREFIXES = ("[tool:", "tool:", "[command")


def default_db_path() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "recall.db"


def default_store_root() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "sessions"


def _record_text(rec: dict) -> str:
    text = rec.get("text") or rec.get("content")
    if isinstance(text, dict):
        text = text.get("text") or text.get("content")
    return text if isinstance(text, str) else ""


def _record_ts(rec: dict) -> float:
    try:
        return float(rec.get("ts") or rec.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _message_text(msg: dict) -> str:
    """Text of one chat message: str content, or the text parts of a
    vision-style part list (image-only parts contribute nothing)."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(p.get("text") or "").strip()
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    return ""


def _indexable_texts(rec: dict) -> list[tuple[str, str]]:
    """(role, text) pairs a transcript record contributes to the index.

    Handles BOTH storage shapes: SessionStore appends ``{"type": "messages",
    "messages": [{role, content}, ...]}`` (the only shape the real agent loop
    writes), and flat per-message records ``{"role", "text"}`` (older test
    fixtures / external writers)."""
    if rec.get("type") == "messages" and isinstance(rec.get("messages"), list):
        out: list[tuple[str, str]] = []
        for msg in rec["messages"]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            if role not in _INDEXED_ROLES:
                continue
            text = _message_text(msg)
            if not text or text.startswith(_TOOL_PREFIXES):
                continue
            out.append((role, text))
        return out
    role = str(rec.get("role") or rec.get("type") or "")
    if role not in _INDEXED_ROLES:
        return []
    text = _record_text(rec).strip()
    if not text or text.startswith(_TOOL_PREFIXES):
        return []
    return [(role, text)]


def _clean_fts(query: str) -> str:
    """Tokenizer-safe FTS5 query: alnum tokens, quoted (defeats operator chars)."""
    tokens = [t for t in re.findall(r"[A-Za-z0-9_]{2,}", query)]
    return " ".join('"' + t + '"' for t in tokens)


class RecallIndex:
    def __init__(self, store_root: Path | None = None, db_path: Path | None = None) -> None:
        self.store_root = Path(store_root) if store_root else default_store_root()
        self.db_path = Path(db_path) if db_path else default_db_path()
        self._conn: sqlite3.Connection | None = None
        self._has_fts5: bool | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS recall_meta(k TEXT PRIMARY KEY, v TEXT);
                CREATE TABLE IF NOT EXISTS recall_rows(
                    id INTEGER PRIMARY KEY, session TEXT, ts REAL, role TEXT, text TEXT,
                    salience REAL DEFAULT 0
                );
                """
            )
            try:
                # an index built before salience existed keeps working; the
                # column is added in place and filled on the next rebuild
                self._conn.execute("ALTER TABLE recall_rows ADD COLUMN salience REAL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # already there
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts "
                    "USING fts5(id UNINDEXED, session UNINDEXED, text)"
                )
                self._has_fts5 = True
            except sqlite3.OperationalError:
                # build without FTS5: LIKE scan fallback below
                self._has_fts5 = False
        return self._conn

    def _snapshot_mtime(self) -> float:
        latest = 0.0
        try:
            for p in self.store_root.glob("*.jsonl"):
                try:
                    latest = max(latest, p.stat().st_mtime)
                except OSError:
                    continue
        except OSError:
            pass
        return latest

    def _store_mtime(self) -> float:
        conn = self._connect()
        row = conn.execute("SELECT v FROM recall_meta WHERE k='src_mtime'").fetchone()
        try:
            return float(row[0]) if row else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _is_stale(self) -> bool:
        return self._snapshot_mtime() > self._store_mtime()

    def rebuild(self) -> int:
        """(Re)index every transcript; returns row count. Cheap and idempotent."""
        conn = self._connect()
        rows: list[tuple[str, float, str, str]] = []
        for p in sorted(self.store_root.glob("*.jsonl")):
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                for role, text in _indexable_texts(rec):
                    rows.append((str(p.stem), _record_ts(rec), role, text))
        conn.execute("DELETE FROM recall_rows")
        if self._has_fts5:
            conn.execute("DELETE FROM recall_fts")
        # salience is measured ONCE, here, against everything indexed before it -
        # never at query time, where it would cost the same work on every search
        from saturday.memscore import SalienceIndex

        sal = SalienceIndex()
        conn.executemany(
            "INSERT INTO recall_rows(session, ts, role, text, salience) VALUES(?,?,?,?,?)",
            [(s_, t_, r_, x_, sal.add(x_)) for (s_, t_, r_, x_) in rows],
        )
        if self._has_fts5:
            with_ft = rows
            conn.executemany(
                "INSERT INTO recall_fts(id, session, text) VALUES(?,?,?)",
                [(i + 1, s, t) for i, (s, _, _, t) in enumerate(with_ft)],
            )
        conn.execute(
            "INSERT INTO recall_meta(k, v) VALUES('src_mtime', ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(self._snapshot_mtime()),),
        )
        conn.commit()
        return len(rows)

    def search(self, query: str, k: int = 6) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        conn = self._connect()
        if self._is_stale():
            self.rebuild()
        try:
            if self._has_fts5:
                tokens = _clean_fts(q)
                if tokens:
                    # over-fetch, then re-rank: text match alone answers "what
                    # mentions this" when the question is "what should I
                    # remember about this"
                    raw = conn.execute(
                        "SELECT r.session, r.ts, r.role, r.text, r.salience "
                        "FROM recall_fts f JOIN recall_rows r ON r.id = f.id "
                        "WHERE recall_fts MATCH ? "
                        "ORDER BY bm25(recall_fts) LIMIT ?",
                        (tokens, max(k, k * 5)),
                    ).fetchall()
                    return self._rank(raw, k)
            like = f"%{q}%"
            return [
                {"session": r[0], "ts": r[1], "role": r[2], "text": r[3]}
                for r in conn.execute(
                    "SELECT session, ts, role, text FROM recall_rows "
                    "WHERE text LIKE ? ORDER BY ts DESC LIMIT ?",
                    (like, k),
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            # malformed query / locked db: degrade to LIKE scan
            like = f"%{q}%"
            return [
                {"session": r[0], "ts": r[1], "role": r[2], "text": r[3]}
                for r in conn.execute(
                    "SELECT session, ts, role, text FROM recall_rows "
                    "WHERE text LIKE ? ORDER BY ts DESC LIMIT ?",
                    (like, k),
                ).fetchall()
            ]

    @staticmethod
    def _rank(raw: list, k: int) -> list[dict]:
        """Re-rank BM25 hits by recency, relevance and salience together."""
        from saturday.memscore import combine, normalize_relevance, recency

        if not raw:
            return []
        now = time.time()
        rel = normalize_relevance([0.0] * len(raw))  # BM25 order, positionally normalized
        out = []
        for (session, ts, role, text, salience), relevance in zip(raw, rel):
            score = combine(recency(float(ts or 0.0), now), relevance, float(salience or 0.0))
            out.append({"session": session, "ts": ts, "role": role, "text": text,
                        "score": round(score, 4)})
        out.sort(key=lambda r: -r["score"])
        return out[:k]

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None


def format_recall(results: list[dict], limit_text: int = 200) -> str:
    """Tool-facing render: one compact snippet per hit."""
    if not results:
        return "no past sessions mention that"
    lines = []
    for i, r in enumerate(results, 1):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])) if r["ts"] else "?"
        body = (r["text"] or "").replace("\n", " ").strip()
        if len(body) > limit_text:
            body = body[:limit_text] + "..."
        score = f" | score {r['score']:.2f}" if r.get("score") is not None else ""
        lines.append(f"{i}. [{when} | session {r['session']} | {r['role']}{score}] {body}")
    return "\n".join(lines)
