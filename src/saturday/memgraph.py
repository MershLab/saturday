"""Memory graph: everything Saturday knows, as nodes and edges.

Four layers over one graph, each built from a store that already exists:

  code     files and directories from the repo index (tools/repo_index.py)
  chat     past sessions from the transcript store
  facts    curated lines from MEMORY.md
  skills   installed procedures from the skills store

The interesting edges are the ones between layers - a session that touched a
file, a fact that came out of a session - because that is what makes this a
memory of the work rather than a diagram of the code.

Code edges come from the index's own postings: a file that uniquely DEFINES a
symbol is linked from every file whose text mentions it. It needs no import
resolution and works for every language the index reads. It is a lexical
signal, not a call graph - a mention counts whether it is a call, an unrelated
attribute or a word in a comment - so names defined by more than one file are
dropped rather than guessed at, and the edge is called `mentions`.
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

    # symbol edges: definer <- mentioner
    #
    # A lexical index cannot tell two same-named symbols apart, so a name that
    # more than one file defines cannot be attributed to any one of them. The
    # old code took the first definer and pointed every other file at it, which
    # invented an edge per rival definition: `run` is defined in 27 files here.
    #
    # Ambiguity is judged over the whole index, not the ranked slice, or a name
    # defined once above the size cut and again below it still looks unique.
    definers: dict[str, set[str]] = {}
    for rel, meta in files.items():
        for sym in (meta.get("symbols") or []):
            definers.setdefault(str(sym).lower(), set()).add(rel)
    for sym, owners in definers.items():
        if len(owners) != 1:
            continue
        owner = next(iter(owners))
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
                b.edge(rid, oid, "mentions", min(4.0, count))


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
    """Facts come from the memory index, not from re-splitting the file.

    The index already knows each note's salience and the relates_to /
    contradicts links between them; parsing the lines again here would show a
    poorer version of the same thing in the one view meant to explain it."""
    if not memory_file.is_file():
        return
    try:
        raw = memory_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    notes: list[dict] = []
    edges: list[tuple[int, int, str]] = []
    try:
        from saturday.memindex import MemoryIndex

        idx = MemoryIndex()
        try:
            idx.reindex(raw, scope="global")
            graph = idx.graph(scope="global")
        finally:
            idx.close()
        notes = graph["nodes"]
        edges = [(e["from"], e["to"], e["relation"]) for e in graph["edges"]]
    except Exception:
        # the index is optional scaffolding over a plain file; if it cannot be
        # built the notes themselves must still appear
        from saturday.memindex import parse_notes

        try:
            notes = [{"id": i, "slug": n["slug"], "text": n["text"], "salience": 0.5}
                     for i, n in enumerate(parse_notes(raw))]
        except Exception:
            return

    kept = {n["meta"]["path"] for n in b.nodes if n["kind"] == "file"}
    by_node_id: dict[int, int] = {}
    for note in notes:
        text = note["text"]
        salience = float(note.get("salience") or 0.5)
        fid = b.node(
            f"fact:{note['slug']}", kind="fact", label=text[:60], group="facts",
            # a surprising note pulls harder than one restating what is known
            weight=1.0 + 1.6 * salience,
            meta={"text": text, "slug": note["slug"], "salience": round(salience, 3)},
        )
        by_node_id[note["id"]] = fid
        for rel in _paths_in(text, kept):
            tid = b.index.get(f"file:{rel}")
            if tid is not None:
                b.edge(fid, tid, "about", 2.5)
        # a fact that names a symbol belongs next to the file defining it
        for word in set(w.lower() for w in _WORD_RE.findall(text)):
            owner = b.index.get(f"file:{word}")
            if owner is not None:
                b.edge(fid, owner, "about", 1.0)

    for src, dst, relation in edges:
        a, c = by_node_id.get(src), by_node_id.get(dst)
        if a is not None and c is not None:
            # a contradiction is the most informative link in the whole graph
            b.edge(a, c, relation, 4.0 if relation == "contradicts" else 2.0)


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


# Level 1 of the graph's level of detail. The default picture stays structural -
# folders, files, chats, facts, skills - because 1,400 callables against 110
# structural nodes drowns the folders the layout exists to show. Symbols arrive
# only for the one file a reader opened, the way an editor outline does.
SYMBOL_LIMIT = 120
SYMBOL_KINDS = ("class", "function", "method", "constant", "field")

# Only extensions the bundled ast extractor also covers map here beyond the
# obvious - no point offering LSP a language repo_index can't fall back for.
_LSP_LANGUAGE_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust",
    ".java": "java", ".rb": "ruby", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
}


def _flatten_lsp_symbols(symbols: list[dict], parent: str = "") -> list[dict]:
    """documentSymbol's nested {name, kind, line, children} into the same
    flat {name, kind, line, parent} shape repo_index's ast extractor
    produces, so the node-building loop below has one input shape regardless
    of source."""
    out = []
    for s in symbols:
        out.append({"name": s["name"], "kind": s["kind"], "line": s["line"], "parent": parent})
        child_parent = s["name"] if s["kind"] == "class" else parent
        out.extend(_flatten_lsp_symbols(s.get("children") or [], child_parent))
    return out


def _lsp_symdefs(workspace_root: str | Path, rel: str,
                 lsp_servers_cfg: dict) -> list[dict] | None:
    """documentSymbol for one file if a server is configured for its
    language, else None to signal "fall back to the index". Best-effort: any
    failure (server not installed, timeout, no server for this extension)
    falls back the same way a missing server does everywhere else in this
    codebase - LSP is an enhancement, never a requirement."""
    if not lsp_servers_cfg:
        return None
    language = _LSP_LANGUAGE_BY_EXT.get(Path(rel).suffix.lower())
    cmd = lsp_servers_cfg.get(language) if language else None
    if not cmd:
        return None
    try:
        from saturday.tools.lsp import _resolve_in_root, get_client

        # rel is caller-controlled the same as every other memgraph path, and
        # this is the one branch of expand_file that actually opens a file -
        # the same guard the LSP tools use elsewhere, not a new one
        path, err = _resolve_in_root(rel, str(workspace_root))
        if err:
            return None
        client = get_client(language, cmd, str(workspace_root))
        if client is None:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        client.did_open(str(path), text, language_id=language)
        symbols = client.document_symbol(str(path))
    except Exception:
        return None
    return _flatten_lsp_symbols(symbols) or None


def expand_file(workspace_root: str | Path | None, rel: str,
                index: dict | None = None, limit: int = SYMBOL_LIMIT,
                lsp_servers_cfg: dict | None = None) -> dict:
    """Symbol nodes for one file, addressed by id rather than by position.

    Prefers a configured language server's documentSymbol over the index's
    ast-derived symdefs when one is available for this file's language: LSP
    resolves real scope and covers languages the bundled ast extractor
    cannot touch at all. Falls back to the index silently otherwise.

    Returns edges as {source, target} node ids, not array offsets: the caller
    already holds a graph and has to splice these into its own numbering."""
    empty = {"nodes": [], "edges": [], "path": rel, "truncated": False}
    if not rel:
        return empty

    defs = None
    if workspace_root:
        defs = _lsp_symdefs(workspace_root, rel, lsp_servers_cfg or {})

    if defs is None:
        idx = index
        if idx is None:
            if not workspace_root:
                return empty
            try:
                from saturday.tools.repo_index import build_index

                idx = build_index(workspace_root)
            except Exception:
                return empty
        meta = (idx.get("files") or {}).get(rel)
        if not meta:
            return empty
        defs = [d for d in (meta.get("symdefs") or [])
                if isinstance(d, dict) and d.get("name")]
    if not defs:
        return empty

    truncated = len(defs) > limit
    defs = defs[:limit]

    file_id = f"file:{rel}"
    nodes: list[dict] = []
    edges: list[dict] = []
    # a class has to be seen before its methods can nest under it; symdefs is
    # already ordered by line, and a class always precedes its own body
    class_ids: dict[str, str] = {}

    for d in defs:
        kind = str(d.get("kind") or "function")
        if kind not in SYMBOL_KINDS:
            continue
        name, line = str(d["name"]), int(d.get("line") or 0)
        parent = str(d.get("parent") or "")
        nid = f"sym:{rel}:{name}:{line}"
        if kind == "class":
            class_ids.setdefault(name, nid)
        nodes.append({
            "id": nid, "kind": kind, "label": name, "group": rel,
            "weight": 2.0 if kind == "class" else 1.0,
            "meta": {"path": rel, "line": line, "parent": parent, "of": rel},
        })
        # nest under the enclosing class when it is in this same file, so a
        # class expands as a cluster instead of a flat spray off the file
        owner = class_ids.get(parent) if parent else None
        edges.append({"source": owner or file_id, "target": nid,
                      "kind": "defines", "w": 2.0})

    return {"nodes": nodes, "edges": edges, "path": rel, "truncated": truncated}
