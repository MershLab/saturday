"""Memory graph: everything Saturday knows, as nodes and edges.

Four layers over one graph, each built from a store that already exists:

  code     files and directories from the repo index (tools/repo_index.py)
  chat     past sessions from the transcript store
  facts    curated lines from MEMORY.md
  skills   installed procedures from the skills store

The interesting edges are the ones between layers - a session that touched a
file, a fact that came out of a session - because that is what makes this a
memory of the work rather than a diagram of the code.

Code edges come from the index's own postings: a file that DEFINES a symbol is
linked from every file whose text references it. That is a real call graph's
cheap cousin, it needs no import resolution, and it works for every language
the index reads rather than only Python.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# a term shared by half the repo says nothing about either file; a term in two
# or three files is what actually ties them together
MAX_DEF_FANOUT = 40
MAX_NODES = 4000
MAX_EDGES = 24000
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


class _Builder:
    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.index: dict[str, int] = {}
        self.edges: dict[tuple[int, int], dict] = {}

    def node(self, nid: str, *, kind: str, label: str, group: str = "",
             weight: float = 1.0, meta: dict | None = None) -> int:
        i = self.index.get(nid)
        if i is not None:
            self.nodes[i]["weight"] += weight
            return i
        i = len(self.nodes)
        self.index[nid] = i
        self.nodes.append({
            "id": nid, "kind": kind, "label": label,
            "group": group or kind, "weight": weight, "meta": meta or {},
        })
        return i

    def edge(self, a: int, b: int, kind: str, weight: float = 1.0) -> None:
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        e = self.edges.get(key)
        if e is None:
            self.edges[key] = {"s": key[0], "t": key[1], "kind": kind, "w": weight}
        else:
            e["w"] += weight

    def result(self, max_nodes: int = MAX_NODES) -> dict:
        edges = sorted(self.edges.values(), key=lambda e: -e["w"])[:MAX_EDGES]
        if len(self.nodes) > max_nodes:
            edges = self._trim(max_nodes, edges)
        return {"nodes": self.nodes, "edges": edges}

    def _trim(self, keep_n: int, edges: list[dict]) -> list[dict]:
        """Keep the most connected nodes and renumber the edges onto them.

        Degree, not weight: an isolated node is the one nobody misses, and a
        graph nobody can read is worse than a smaller one that they can."""
        degree = [0.0] * len(self.nodes)
        for e in edges:
            degree[e["s"]] += e["w"]
            degree[e["t"]] += e["w"]
        keep = sorted(range(len(self.nodes)), key=lambda i: -degree[i])[:keep_n]
        remap = {old: new for new, old in enumerate(sorted(keep))}
        self.nodes = [self.nodes[i] for i in sorted(keep)]
        self.index = {n["id"]: i for i, n in enumerate(self.nodes)}
        out = []
        for e in edges:
            s, t = remap.get(e["s"]), remap.get(e["t"])
            if s is not None and t is not None:
                out.append({"s": s, "t": t, "kind": e["kind"], "w": e["w"]})
        return out


def _dir_of(rel: str) -> str:
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return parent


def _add_code_layer(b: _Builder, index: dict, limit: int) -> None:
    files: dict = index.get("files") or {}
    postings: dict = index.get("postings") or {}
    if not files:
        return

    # rank by size so a capped graph keeps the substantial files
    ranked = sorted(files.items(), key=lambda kv: -(kv[1].get("len") or 0))[:limit]
    kept = {rel for rel, _ in ranked}

    dir_ids: dict[str, int] = {}
    for rel, meta in ranked:
        parent = _dir_of(rel)
        fid = b.node(
            f"file:{rel}", kind="file", label=rel.rsplit("/", 1)[-1],
            group=parent or "/", weight=1.0 + (meta.get("len") or 0) / 4000.0,
            meta={"path": rel, "lines": meta.get("len") or 0,
                  "symbols": (meta.get("symbols") or [])[:12]},
        )
        # directory nodes are what give the picture its bright hubs: every file
        # pulls on its folder, so folders end up dense and central
        if parent:
            did = dir_ids.get(parent)
            if did is None:
                did = dir_ids[parent] = b.node(
                    f"dir:{parent}", kind="dir", label=parent.rsplit("/", 1)[-1],
                    group=parent, weight=2.0, meta={"path": parent},
                )
            b.edge(fid, did, "contains", 2.0)

    # nest directories so the tree itself is connected
    for path, did in list(dir_ids.items()):
        parent = _dir_of(path)
        if parent and parent in dir_ids:
            b.edge(did, dir_ids[parent], "contains", 1.5)

    # symbol edges: definer <- referencer
    definers: dict[str, str] = {}
    for rel, meta in ranked:
        for sym in (meta.get("symbols") or []):
            definers.setdefault(sym.lower(), rel)
    for sym, owner in definers.items():
        hits = postings.get(sym)
        if not hits or len(hits) > MAX_DEF_FANOUT:
            continue
        oid = b.index.get(f"file:{owner}")
        if oid is None:
            continue
        for rel, info in hits.items():
            if rel == owner or rel not in kept:
                continue
            rid = b.index.get(f"file:{rel}")
            if rid is not None:
                try:
                    count = float(info[0])
                except (TypeError, ValueError, IndexError):
                    count = 1.0
                b.edge(rid, oid, "references", min(4.0, count))


def _session_records(path: Path):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


def _paths_in(text: str, kept: set[str]) -> set[str]:
    """Workspace-relative paths a transcript mentions, restricted to real files."""
    out: set[str] = set()
    if not text:
        return out
    for m in re.finditer(r"[\w./\-]+\.[A-Za-z0-9]{1,5}", text):
        cand = m.group(0).lstrip("./")
        if cand in kept:
            out.add(cand)
    return out


def _add_chat_layer(b: _Builder, store_root: Path, limit: int) -> None:
    if not store_root.is_dir():
        return
    kept = {n["meta"]["path"] for n in b.nodes if n["kind"] == "file"}
    try:
        files = sorted(store_root.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime)
    except OSError:
        return
    for path in files[:limit]:
        touched: set[str] = set()
        turns = 0
        first_user = ""
        for rec in _session_records(path):
            msgs = rec.get("messages") if rec.get("type") == "messages" else [rec]
            if not isinstance(msgs, list):
                continue
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(p.get("text") or "") for p in content
                        if isinstance(p, dict)
                    )
                if not isinstance(content, str):
                    continue
                turns += 1
                if msg.get("role") == "user" and not first_user:
                    first_user = content.strip()[:80]
                touched |= _paths_in(content, kept)
        if not turns:
            continue
        # a chat's pull on the graph is its size AND its freshness: an old
        # session should not dominate the picture forever on turn count alone
        try:
            from saturday.memscore import recency

            fresh = recency(path.stat().st_mtime, time.time())
        except Exception:
            fresh = 0.0
        sid = b.node(
            f"session:{path.stem}", kind="session",
            label=first_user or path.stem[:24], group="chat",
            weight=1.0 + min(6.0, turns / 8.0) + 2.0 * fresh,
            meta={"session": path.stem, "turns": turns, "files": len(touched),
                  "recency": round(fresh, 3)},
        )
        for rel in touched:
            fid = b.index.get(f"file:{rel}")
            if fid is not None:
                b.edge(sid, fid, "touched", 3.0)


def _add_fact_layer(b: _Builder, memory_file: Path) -> None:
    if not memory_file.is_file():
        return
    try:
        raw = memory_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    kept = {n["meta"]["path"] for n in b.nodes if n["kind"] == "file"}
    for n, line in enumerate(raw.splitlines()):
        text = line.strip().lstrip("-*# ").strip()
        if len(text) < 8:
            continue
        fid = b.node(
            f"fact:{n}", kind="fact", label=text[:60], group="facts",
            weight=1.6, meta={"text": text},
        )
        for rel in _paths_in(text, kept):
            tid = b.index.get(f"file:{rel}")
            if tid is not None:
                b.edge(fid, tid, "about", 2.5)
        # a fact that names a symbol belongs next to the file defining it
        for word in set(w.lower() for w in _WORD_RE.findall(text)):
            owner = b.index.get(f"file:{word}")
            if owner is not None:
                b.edge(fid, owner, "about", 1.0)


def _add_skill_layer(b: _Builder, skills_dir: Path) -> None:
    if not skills_dir.is_dir():
        return
    for path in sorted(skills_dir.glob("*"))[:200]:
        name = path.stem
        if not name or name.startswith("."):
            continue
        b.node(f"skill:{name}", kind="skill", label=name, group="skills",
               weight=2.0, meta={"name": name})


def build_graph(workspace_root: str | Path | None = None,
                store_root: Path | None = None,
                limit: int = MAX_NODES) -> dict:
    """Assemble the whole graph. Every layer is optional and best effort:
    a machine with no sessions still gets its codebase, and a workspace with
    no code still gets its chats."""
    from saturday.config import get_config_dir

    cfg_dir = get_config_dir()
    b = _Builder()

    if workspace_root:
        try:
            from saturday.tools.repo_index import build_index

            # files only: folders, chats, facts and skills come out of the
            # same budget, and result() trims whatever still overflows
            _add_code_layer(b, build_index(workspace_root), int(limit * 0.75))
        except Exception:
            pass  # an unreadable workspace must not empty the whole graph

    try:
        _add_chat_layer(b, Path(store_root) if store_root else cfg_dir / "sessions", 300)
    except Exception:
        pass
    try:
        _add_fact_layer(b, cfg_dir / "MEMORY.md")
    except Exception:
        pass
    try:
        _add_skill_layer(b, cfg_dir / "skills")
    except Exception:
        pass

    out = b.result(limit)
    counts: dict[str, int] = {}
    for n in out["nodes"]:
        counts[n["kind"]] = counts.get(n["kind"], 0) + 1
    out["stats"] = {"nodes": len(out["nodes"]), "edges": len(out["edges"]), "kinds": counts}
    return out
