"""Repo index: zero-dependency lexical code search over the workspace.

An inverted index (term -> path -> [count, first_line]) over text/code files,
cached in <workspace>/.saturday/repo_index.json with mtime invalidation.
Identifiers are split (camelCase/snake_case) so `parseHermesToolCalls`,
`parse_hermes_tool_calls` and "hermes tool calls" all match. Scoring is
BM25-lite. This is honest agentic retrieval - not vector embeddings; it needs
no model, no network, and stays inside the stdlib constraint.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path

INDEX_NAME = "repo_index.json"
SKIP_DIRS = {
    ".git", ".saturday", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".pytest_cache", ".next", ".nuxt", "target", "vendor", ".tox", ".mypy_cache",
    ".ruff_cache", ".cache", "coverage", ".cargo", ".rustup", ".gradle", ".m2",
}
CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh",
    ".ps1", ".sql", ".html", ".css", ".ini", ".cfg", ".svg",
}
MAX_FILE_BYTES = 256_000
MAX_FILES = 5000
MAX_SYMBOLS_PER_FILE = 200

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_SPLIT_RE = re.compile(r"[_\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _terms(text: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        low = tok.lower()
        out.append(low)
        parts = [p.lower() for p in _SPLIT_RE.split(tok) if len(p) > 1]
        if len(parts) > 1:
            out.extend(parts)
    return out


# Records are plain JSON-serializable dicts with these keys and nothing else,
# so the index can be held by a file, a table, or anything in between without
# the extractor knowing which. Fields: name, kind, line, parent.
SYMBOL_KINDS = ("function", "method", "class", "constant", "field")


def _py_symdefs(raw: str) -> list[dict]:
    """Defined symbols in a Python file, with kind, line and enclosing scope.

    Walks the tree rather than ast.walk() because the parent scope is the whole
    point: it is what separates a module constant from a local. A binding under
    a Module or ClassDef has an identity other files can reach, so it is kept;
    one under a function is a local and is dropped. Locals outnumber everything
    else roughly two to one and nothing can ever link to them.

    Descends through if/try/with so conditionally defined names are still found.
    Syntax-error files yield nothing."""
    try:
        import ast

        tree = ast.parse(raw)
    except Exception:
        return []

    out: list[dict] = []

    def emit(name: str, kind: str, line: int, parent: str) -> bool:
        out.append({"name": name, "kind": kind, "line": line, "parent": parent})
        return len(out) < MAX_SYMBOLS_PER_FILE

    def bound_names(node) -> list[str]:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
        names = []
        for t in targets:
            # only bare names have a stable cross-file identity; a.b = 1 and
            # d["k"] = 1 do not define anything another file can reference
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
        return names

    def visit(node, parent_name: str, scope: str) -> bool:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if scope == "class" else "function"
                if not emit(child.name, kind, child.lineno, parent_name):
                    return False
                if not visit(child, child.name, "function"):
                    return False
            elif isinstance(child, ast.ClassDef):
                if not emit(child.name, "class", child.lineno, parent_name):
                    return False
                if not visit(child, child.name, "class"):
                    return False
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                if scope == "function":
                    continue  # a local: no identity outside its frame
                kind = "field" if scope == "class" else "constant"
                for name in bound_names(child):
                    if not emit(name, kind, child.lineno, parent_name):
                        return False
            elif not visit(child, parent_name, scope):
                return False
        return True

    visit(tree, "", "module")
    out.sort(key=lambda d: (d["line"], d["name"]))
    return out


def _py_symbols(raw: str) -> list[str]:
    """Defined def/class names in a Python file via stdlib ast (best effort).

    Symbol definitions are the strongest lexical signal for code retrieval —
    a query for `parse_hermes_tool_calls` should rank the file that DEFINES it
    above files that merely mention it. Syntax-error files yield nothing.

    Callables only: constants and fields are recorded in symdefs but kept out
    of this list, which feeds search boosting and the graph's ambiguity check."""
    return [d["name"] for d in _py_symdefs(raw)
            if d["kind"] in ("function", "method", "class")]


def _index_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / ".saturday" / INDEX_NAME


def _scan_files(root: Path) -> list[Path]:
    """os.walk, not Path.rglob: rglob yields every entry under root before any
    filtering runs, so it still descends into node_modules/.next/.git/etc and
    can spend most of its time enumerating files that were always going to be
    skipped. os.walk lets SKIP_DIRS prune dirnames in place, so those trees
    are never entered at all - the difference is an order of magnitude on a
    real JS/Python monorepo."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if len(files) >= MAX_FILES:
                return files
            p = Path(dirpath) / name
            if p.suffix.lower() not in CODE_EXTS:
                continue
            try:
                if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                    continue
                files.append(p)
            except OSError:
                continue
    return files


def build_index(workspace_root: str | Path, force: bool = False) -> dict:
    root = Path(workspace_root)
    ipath = _index_path(root)
    cache: dict = {"files": {}, "postings": {}}
    if not force and ipath.is_file():
        try:
            loaded = json.loads(ipath.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and "files" in loaded:
                cache = loaded
        except (OSError, json.JSONDecodeError):
            pass
    known: dict = cache.setdefault("files", {})
    # WHY: every known entry carries its own "terms" map so an incremental
    # rebuild can reuse unchanged files' postings; older caches stored only
    # mtime/len, so treat term-less entries as cold and re-index them below
    # instead of letting their hits vanish.
    for meta in known.values():
        if isinstance(meta, dict) and "terms" not in meta:
            meta["terms"] = {}
    seen_paths: set[str] = set()
    for p in _scan_files(root):
        rel = p.relative_to(root).as_posix()
        try:
            mtime = p.stat().st_mtime_ns
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # transient lock/unread: keep any cached entry below
        seen_paths.add(rel)
        prev = known.get(rel)
        is_py = p.suffix.lower() == ".py"
        # a cached entry is reusable only if it has terms AND (for Python) an
        # explicit symbols list — pre-symbol caches must re-index once to get
        # symbol boosting instead of silently staying unboosted forever
        cacheable = prev is not None and prev.get("mtime") == mtime and prev.get("terms")
        if is_py and not {"symbols", "symdefs"} <= set(prev or {}):
            cacheable = False
        if cacheable:
            continue  # cached terms survive into the merged postings below
        terms: dict[str, list] = {}
        total = 0
        for lineno, line in enumerate(raw.splitlines(), 1):
            line_terms = _terms(line)
            total += len(line_terms)
            for t in line_terms:
                entry = terms.get(t)
                if entry is None:
                    terms[t] = [1, lineno]
                else:
                    entry[0] += 1
        known[rel] = {"mtime": mtime, "len": max(1, total), "terms": terms}
        if is_py:
            # precompute split symbol terms so search_index never re-splits
            # per query; explicit [] keeps legacy caches cacheable
            symdefs = _py_symdefs(raw)
            known[rel]["symdefs"] = symdefs
            known[rel]["symbols"] = symbols = sorted(
                {d["name"] for d in symdefs
                 if d["kind"] in ("function", "method", "class")})
            known[rel]["symbol_terms"] = sorted(_symbol_term_set({"symbols": symbols}))
    # drop vanished files (their cached terms go with them)
    for rel in list(known.keys()):
        if rel not in seen_paths:
            del known[rel]
    # WHY: rebuild postings FROM the merged per-file terms rather than starting
    # empty and overwriting the cache's postings — otherwise the second run
    # (everything unchanged) persisted zero hits and repo_search went blind.
    postings: dict[str, dict[str, list]] = {}
    for rel, meta in known.items():
        for term, info in meta.get("terms", {}).items():
            postings.setdefault(term, {})[rel] = info
    cache["postings"] = postings
    cache["built"] = time.time()
    try:
        ipath.parent.mkdir(parents=True, exist_ok=True)
        ipath.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return cache


def _symbol_term_set(meta: dict) -> set[str]:
    """Full def/class names AND their camelCase/snake_case parts, lowercased —
    mirrors how query terms are split so `parse_hermes_tool_calls` matches a
    query for 'hermes tool calls'. Precomputed at index time into
    meta["symbol_terms"]; this fallback keeps legacy caches working."""
    stored = meta.get("symbol_terms")
    if stored is not None:
        return set(stored)
    out: set[str] = set()
    for sym in meta.get("symbols") or []:
        low = str(sym).lower()
        out.add(low)
        out.update(p.lower() for p in _SPLIT_RE.split(str(sym)) if len(p) > 1)
    return out


def search_index(workspace_root: str | Path, query: str, k: int = 8, index: dict | None = None) -> list[dict]:
    idx = index or build_index(workspace_root)
    docs = idx.get("files", {})
    postings = idx.get("postings", {})
    n_docs = max(1, len(docs))
    q_terms = [t for t in dict.fromkeys(_terms(query)) if t]
    scores: dict[str, float] = {}
    hits_line: dict[str, int] = {}
    for term in q_terms:
        matches = postings.get(term)
        if not matches:
            continue
        idf = math.log(1 + n_docs / len(matches))
        for path, (count, first_line) in matches.items():
            dl = docs.get(path, {}).get("len", 100)
            tf = count / (count + 1.2 * dl / 400)
            scores[path] = scores.get(path, 0.0) + idf * tf * (count ** 0.5)
            cur = hits_line.get(path)
            if cur is None or first_line < cur:
                hits_line[path] = first_line
    # definition boost (AST symbols): a file that DEFINES a queried identifier
    # ranks above files that merely mention it; boost scales with how rare the
    # symbol is across the workspace.
    sym_files: list[tuple[str, int]] = []
    for path, meta in docs.items():
        terms = _symbol_term_set(meta)
        n_hits = sum(1 for t in q_terms if t in terms)
        if n_hits:
            sym_files.append((path, n_hits))
    if sym_files:
        defining_paths = {p for p, _ in sym_files}
        sym_idf = math.log(1 + n_docs / len(defining_paths))
        for path, n_hits in sym_files:
            scores[path] = scores.get(path, 0.0) + 2.0 * sym_idf * n_hits
            cur = hits_line.get(path)
            line_terms = docs[path].get("terms", {})
            lines = [(line_terms.get(t) or [0, 0])[1] for t in q_terms if t in line_terms]
            best = min((v for v in lines if v), default=0)
            if best and (cur is None or best < cur):
                hits_line[path] = best
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
    return [{"path": path, "score": round(score, 4), "line": hits_line.get(path, 1)} for path, score in ranked]


def make_repo_search_tool(workspace_root_fn):
    """workspace_root_fn: zero-arg callable returning the active workspace."""

    class RepoSearchTool:
        name = "repo_search"
        description = (
            "Search across the whole workspace index (identifiers split "
            "camelCase/snake_case; files that DEFINE a queried def/class rank "
            "first via AST symbols; cached, incremental). Prefer this before many greps."
        )
        parameters = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "description": "max results (default 8)"},
            },
            "required": ["query"],
        }

        def run(self, args: dict) -> tuple[bool, str]:
            query = str(args.get("query") or "").strip()
            if not query:
                return False, "empty query"
            try:
                results = search_index(workspace_root_fn(), query, k=int(args.get("k") or 8))
            except Exception as exc:
                return False, f"{type(exc).__name__}: {exc}"
            if not results:
                return True, "(no matches)"
            # the view is told what retrieval already scored; nothing here is
            # computed twice, and nothing is computed only for the view
            try:
                from saturday import attention

                attention.emit_ranked(attention.CODE, results, used=len(results),
                                      node_key="path", label_key="path")
            except Exception:
                pass
            lines = [f"{r['path']}:{r['line']}  (score {r['score']})" for r in results]
            return True, "\n".join(lines)

    return RepoSearchTool()
