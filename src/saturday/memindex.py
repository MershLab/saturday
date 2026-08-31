"""Derived index over MEMORY.md: nodes, links, salience, retrieval.

MEMORY.md stays the source of truth - human readable, git friendly, and
editable with no tooling. This is a rebuildable index over it, so deleting
memory.db loses nothing.

Two ideas from the literature are doing real work here:

* **Salience at write time** (not an LLM importance rating): a note's score is
  how much it adds to what is already stored, measured once when it is first
  seen and never recomputed from scratch.
* **A-MEM's back-linking** (Xu et al., 2502.12110): when a new note turns out
  to be about the same thing as an old one, the edge is added to BOTH. Without
  that step a memory graph is append-only and never corrects itself - old
  notes can only ever accumulate the links they were born with.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from saturday.memscore import (
    SalienceIndex,
    combine,
    diffuse,
    jaccard,
    minhash,
    normalize_relevance,
    recency,
)

SCHEMA_VERSION = 1
LINK_RE = re.compile(r"\[\[([^\]]{1,120})\]\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# A new note links back to old ones above this similarity. Chosen from
# measurement, not taste: over hand-labelled note pairs, genuinely related ones
# scored 0.17 to 0.42 and unrelated ones never exceeded 0.08, so the midpoint of
# that gap separates them with room on both sides.
#
# KNOWN LIMIT, stated rather than tuned away: two SHORT notes about one topic
# worded differently ("used helm charts" / "no longer use helm, argocd replaced
# it") share one content word and score ~0.05, so they do not link. Adding a
# content-word overlap signal was measured and REJECTED - it lifted related
# pairs to a 0.25 floor but lifted unrelated ones to a 0.25 ceiling too, so it
# separated strictly worse than shingles alone. Linking those genuinely needs
# semantics, which means embeddings, which is a dependency this does not take.
RELATE_AT = 0.125
# similar AND negated reads as a correction, not a restatement
NEGATION_RE = re.compile(
    r"\b(not|never|no longer|instead of|rather than|stop|stopped|dropped|"
    r"deprecated|replaced|superseded|wrong|incorrect|don't|doesn't|isn't)\b",
    re.IGNORECASE,
)
ARCHIVE_SALIENCE_FLOOR = 0.08
ARCHIVE_AGE_DAYS = 90.0


# a note about code usually names it: a path, or a dotted/qualified symbol
_PATH_RE = re.compile(r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|c|h|cpp|hpp|cs|sh|sql|ya?ml|toml|md)\b")
# camelCase starting lowercase is the common case and an earlier pattern,
# alternating "starts upper" with "all lower", could not match it at all
_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*\(\)")


def code_entity_of(text: str) -> str:
    """The code thing a note is about, or "" if it is not about code.

    A path wins over a symbol: it is checkable exactly, where a symbol needs a
    search that can be wrong in both directions."""
    m = _PATH_RE.search(text or "")
    if m:
        return m.group(0).lstrip("./")
    m = _SYMBOL_RE.search(text or "")
    return m.group(1) if m else ""


def slugify(text: str, limit: int = 48) -> str:
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return (s[:limit].rstrip("-") or "note")


def parse_notes(markdown: str) -> list[dict[str, Any]]:
    """Split MEMORY.md into notes.

    A note is a bullet or a paragraph - the units people actually write in.
    An explicit ``[[slug]]`` anywhere in the note names it; otherwise the slug
    comes from its own text, so re-parsing an unchanged file is stable."""
    notes: list[dict[str, Any]] = []
    block: list[str] = []

    def flush() -> None:
        if not block:
            return
        text = " ".join(b.strip() for b in block).strip()
        block.clear()
        if len(text) < 4:
            return
        refs = LINK_RE.findall(text)
        # a LEADING [[slug]] names the note; a link anywhere else merely points
        # at one. Conflating them made "see [[jwt-keys]]" claim that slug and
        # push the note actually defining it into a -2 suffix.
        lead = LINK_RE.match(text)
        name = lead.group(1) if lead else ""
        body = LINK_RE.sub(lambda m: m.group(1), text).strip()
        if lead:
            body = body[len(name):].strip(" :-\u2014").strip() or name
        others = [slugify(r) for r in refs if not (lead and r == name)]
        notes.append({"text": body, "refs": others, "slug": slugify(name or body)})

    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith("#"):
            flush()
            continue  # headings organize the file; they are not notes
        if re.match(r"^[-*+]\s+|^\d+\.\s+", stripped):
            flush()
            block.append(re.sub(r"^[-*+]\s+|^\d+\.\s+", "", stripped))
        else:
            block.append(stripped)
    flush()

    # a file can repeat a slug (two bullets starting the same way); keep them
    # distinct so neither silently overwrites the other
    seen: dict[str, int] = {}
    for n in notes:
        base = n["slug"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            n["slug"] = f"{base}-{seen[base]}"
    return notes


class MemoryIndex:
    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            from saturday.config import get_config_dir

            db_path = get_config_dir() / "memory.db"
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_nodes (
                  id INTEGER PRIMARY KEY,
                  slug TEXT NOT NULL,
                  text TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  last_touched REAL NOT NULL,
                  touch_count INTEGER DEFAULT 0,
                  salience REAL DEFAULT 0.5,
                  archived INTEGER DEFAULT 0,
                  code_entity TEXT,
                  UNIQUE (scope, slug)
                );
                CREATE TABLE IF NOT EXISTS memory_edges (
                  from_id INTEGER NOT NULL,
                  to_id INTEGER NOT NULL,
                  relation TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  PRIMARY KEY (from_id, to_id, relation)
                );
                CREATE INDEX IF NOT EXISTS memory_edges_to ON memory_edges(to_id);
                """
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ---------------------------------------------------------------- write

    def reindex(self, markdown: str, scope: str = "global") -> dict[str, int]:
        """Rebuild one scope from its MEMORY.md.

        Salience and created_at are PRESERVED for notes already known: they
        record what was true when the note first appeared, and recomputing
        them on every reparse would erase exactly the history they carry."""
        conn = self._connect()
        now = time.time()
        notes = parse_notes(markdown)

        prior = {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute(
                "SELECT slug, id, salience, created_at FROM memory_nodes WHERE scope=?", (scope,)
            )
        }
        sal = SalienceIndex()
        # seed with what is already stored so a genuinely new note is scored
        # against the corpus, not against an empty index
        for row in conn.execute("SELECT text FROM memory_nodes WHERE scope=?", (scope,)):
            sal.add(row[0])

        keep_slugs: set[str] = set()
        added = 0
        for note in notes:
            keep_slugs.add(note["slug"])
            known = prior.get(note["slug"])
            if known:
                node_id, salience, created = known
                conn.execute(
                    "UPDATE memory_nodes SET text=?, last_touched=?, touch_count=touch_count+1,"
                    " code_entity=? WHERE id=?",
                    (note["text"], now, code_entity_of(note["text"]), node_id))
            else:
                salience = sal.add(note["text"])
                cur = conn.execute(
                    "INSERT INTO memory_nodes(slug, text, scope, created_at, last_touched,"
                    " touch_count, salience, code_entity) VALUES(?,?,?,?,?,1,?,?)",
                    (note["slug"], note["text"], scope, now, now, salience,
                     code_entity_of(note["text"])))
                node_id = int(cur.lastrowid)
                added += 1
            note["id"] = node_id

        by_slug = {n["slug"]: n["id"] for n in notes}
        for note in notes:
            for ref in note["refs"]:
                target = by_slug.get(ref)
                if target and target != note["id"]:
                    self._edge(conn, note["id"], target, "relates_to", now)

        linked = 0
        for note in notes:
            if prior.get(note["slug"]):
                continue  # A-MEM linking is a write-time step, not a re-parse step
            linked += self._backlink(conn, note, scope, now)

        removed = 0
        if keep_slugs:
            placeholders = ",".join("?" * len(keep_slugs))
            removed = conn.execute(
                f"DELETE FROM memory_nodes WHERE scope=? AND slug NOT IN ({placeholders})",
                (scope, *keep_slugs)).rowcount
        else:
            removed = conn.execute("DELETE FROM memory_nodes WHERE scope=?", (scope,)).rowcount
        conn.execute("DELETE FROM memory_edges WHERE from_id NOT IN (SELECT id FROM memory_nodes)"
                     " OR to_id NOT IN (SELECT id FROM memory_nodes)")
        conn.commit()
        return {"notes": len(notes), "added": added, "removed": max(0, removed), "linked": linked}

    @staticmethod
    def _edge(conn, a: int, b: int, relation: str, now: float) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO memory_edges(from_id, to_id, relation, created_at)"
            " VALUES(?,?,?,?)", (a, b, relation, now))

    def _backlink(self, conn, note: dict, scope: str, now: float) -> int:
        """A-MEM: attach a new note to the old ones it is about.

        The edge lands on both nodes, which is the whole point - an old note
        gains a link it could not have had when it was written."""
        sig = minhash(note["text"])
        made = 0
        for node_id, text in conn.execute(
            "SELECT id, text FROM memory_nodes WHERE scope=? AND id<>?", (scope, note["id"])
        ).fetchall():
            sim = jaccard(sig, minhash(text))
            if sim < RELATE_AT:
                continue
            contradicts = bool(NEGATION_RE.search(note["text"])) and not NEGATION_RE.search(text)
            if contradicts:
                # a correction supersedes a plain relation: keeping both would
                # have the graph assert two different things about one pair
                conn.execute(
                    "DELETE FROM memory_edges WHERE relation='relates_to' AND "
                    "((from_id=? AND to_id=?) OR (from_id=? AND to_id=?))",
                    (note["id"], node_id, node_id, note["id"]))
                relation = "contradicts"
            else:
                existing = conn.execute(
                    "SELECT 1 FROM memory_edges WHERE relation='contradicts' AND "
                    "((from_id=? AND to_id=?) OR (from_id=? AND to_id=?)) LIMIT 1",
                    (note["id"], node_id, node_id, note["id"])).fetchone()
                if existing:
                    continue
                relation = "relates_to"
            self._edge(conn, note["id"], node_id, relation, now)
            self._edge(conn, node_id, note["id"], relation, now)
            made += 1
        return made

    # ----------------------------------------------------------------- read

    def search(self, query: str, k: int = 8, scope: str | None = None,
               include_archived: bool = False) -> list[dict[str, Any]]:
        """Rank by recency, relevance and salience, then diffuse one hop."""
        conn = self._connect()
        q = (query or "").strip().lower()
        where = ["1=1"]
        args: list[Any] = []
        if scope:
            where.append("scope=?")
            args.append(scope)
        if not include_archived:
            where.append("archived=0")
        rows = conn.execute(
            f"SELECT id, slug, text, scope, last_touched, salience, archived "
            f"FROM memory_nodes WHERE {' AND '.join(where)}", args).fetchall()
        if not rows:
            return []

        terms = [t for t in re.findall(r"[a-z0-9]+", q) if len(t) > 1]
        matched = []
        for r in rows:
            text = (r[2] or "").lower()
            hits = sum(1 for t in terms if t in text)
            if terms and not hits:
                continue
            matched.append((hits, r))
        if not matched:
            return []
        matched.sort(key=lambda m: -m[0])

        now = time.time()
        rel = normalize_relevance([0.0] * len(matched))
        base: dict[str, float] = {}
        info: dict[str, dict] = {}
        for (hits, r), relevance in zip(matched, rel):
            node_id, slug, text, scp, touched, salience, archived = r
            score = combine(recency(float(touched or 0.0), now), relevance, float(salience or 0.0))
            key = str(node_id)
            base[key] = score
            info[key] = {"id": node_id, "slug": slug, "text": text, "scope": scp,
                         "salience": round(float(salience or 0.0), 3),
                         "recency": round(recency(float(touched or 0.0), now), 3),
                         "archived": bool(archived), "matched": bool(hits)}

        edges = [(str(a), str(b)) for a, b in conn.execute(
            "SELECT from_id, to_id FROM memory_edges").fetchall()]
        spread = diffuse(base, edges)
        for key, score in spread.items():
            if key not in info:
                row = conn.execute(
                    "SELECT id, slug, text, scope, last_touched, salience, archived "
                    "FROM memory_nodes WHERE id=?", (int(key),)).fetchone()
                if not row or (row[6] and not include_archived):
                    continue
                info[key] = {"id": row[0], "slug": row[1], "text": row[2], "scope": row[3],
                             "salience": round(float(row[5] or 0.0), 3),
                             "recency": round(recency(float(row[4] or 0.0), now), 3),
                             "archived": bool(row[6]), "matched": False}
        out = [dict(info[key], score=round(spread[key], 4)) for key in info if key in spread]
        out.sort(key=lambda r: -r["score"])
        try:
            from saturday import attention

            # everything ranked is published, with the cutoff marked: the notes
            # that lost are the ones worth seeing when the answer is wrong
            attention.emit_ranked(attention.MEMORY, out, used=k,
                                  node_key="slug", label_key="text")
        except Exception:
            pass
        return out[:k]

    def graph(self, scope: str | None = None) -> dict[str, Any]:
        conn = self._connect()
        args: list[Any] = []
        where = "WHERE archived=0"
        if scope:
            where += " AND scope=?"
            args.append(scope)
        nodes = [
            {"id": r[0], "slug": r[1], "text": r[2], "scope": r[3],
             "salience": round(float(r[4] or 0.0), 3), "touch_count": r[5]}
            for r in conn.execute(
                f"SELECT id, slug, text, scope, salience, touch_count FROM memory_nodes {where}",
                args)
        ]
        ids = {n["id"] for n in nodes}
        edges = [
            {"from": a, "to": b, "relation": rel}
            for a, b, rel in conn.execute(
                "SELECT from_id, to_id, relation FROM memory_edges")
            if a in ids and b in ids
        ]
        return {"nodes": nodes, "edges": edges}

    # -------------------------------------------------------------- manage

    def stale(self, workspace: str | Path | None) -> list[dict[str, Any]]:
        """Notes about code that is no longer there.

        This is a verified fact, not the usual heuristic of "nothing touched
        this in N weeks". A note can be months old and perfectly true; what
        makes it stale is that the thing it describes is gone."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, slug, text, code_entity FROM memory_nodes "
            "WHERE archived=0 AND code_entity IS NOT NULL AND code_entity<>''"
        ).fetchall()
        if not rows or not workspace:
            return []
        root = Path(workspace)
        if not root.is_dir():
            return []
        known: set[str] | None = None
        out: list[dict[str, Any]] = []
        for node_id, slug, text, entity in rows:
            if "/" in entity or "." in entity.rsplit("/", 1)[-1]:
                exists = (root / entity).exists()
                if not exists:
                    exists = any(root.rglob(entity.rsplit("/", 1)[-1]))
            else:
                if known is None:
                    known = self._workspace_symbols(root)
                exists = entity.lower() in known
            if not exists:
                out.append({"id": node_id, "slug": slug, "text": text, "code_entity": entity})
        return out

    @staticmethod
    def _workspace_symbols(root: Path) -> set[str]:
        """Symbols the repo index already knows it defines."""
        try:
            from saturday.tools.repo_index import build_index

            index = build_index(root)
        except Exception:
            return set()
        names: set[str] = set()
        for meta in (index.get("files") or {}).values():
            for sym in (meta.get("symbols") or []):
                names.add(str(sym).lower())
        return names

    def consolidate(self, dry_run: bool = False,
                    workspace: str | Path | None = None) -> dict[str, Any]:
        """Archive notes that add nothing and have not been touched in months,
        and report the ones describing code that no longer exists.

        Never deletes: MEMORY.md is the truth, and a note dropped from the
        index would come straight back on the next reparse anyway."""
        conn = self._connect()
        now = time.time()
        cutoff = now - ARCHIVE_AGE_DAYS * 86400
        rows = conn.execute(
            "SELECT id, slug, salience, last_touched FROM memory_nodes WHERE archived=0"
        ).fetchall()
        doomed = [r for r in rows
                  if float(r[2] or 0.0) < ARCHIVE_SALIENCE_FLOOR and float(r[3] or 0.0) < cutoff]
        contradictions = conn.execute(
            "SELECT COUNT(*) FROM memory_edges WHERE relation='contradicts'").fetchone()[0]
        stale = self.stale(workspace) if workspace else []
        if not dry_run and doomed:
            conn.executemany("UPDATE memory_nodes SET archived=1 WHERE id=?",
                             [(r[0],) for r in doomed])
            conn.commit()
        return {"scanned": len(rows), "archived": [r[1] for r in doomed],
                "contradictions": contradictions, "stale": stale, "dry_run": dry_run}
