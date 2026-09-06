from __future__ import annotations

import re
from pathlib import Path

from saturday.tools.base import Tool

IGNORED_DIRS = {".git", ".saturday", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".idea", ".vscode"}


def recursive_pattern(pattern: str) -> str:
    """Make a bare filename pattern search the whole tree.

    `Path.glob("*.py")` matches only the top directory, but the schema says
    "glob filter like *.py", so a model asking for *.py was told the code did
    not exist whenever it lived in a subdirectory. A pattern that already
    contains a separator is left exactly as written."""
    p = str(pattern or "").strip()
    if not p:
        return "**/*"
    return p if ("/" in p or p.startswith("**")) else f"**/{p}"


def gitignore_filter(root: Path):
    """A predicate that is True for paths .gitignore excludes.

    A deliberate subset of gitignore: comments, negation, anchored and
    directory patterns. Nested .gitignore files and the full precedence rules
    are not implemented - the goal is to stop search drowning in build output
    the way a bare walk does, not to reimplement git. Anything not understood
    is simply not excluded, so the failure mode is showing too much rather
    than hiding a file the user needed."""
    import fnmatch

    rules: list[tuple[str, bool, bool]] = []   # (pattern, negated, dir_only)
    gi = root / ".gitignore"
    try:
        raw = gi.read_text(encoding="utf-8", errors="replace") if gi.is_file() else ""
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        if neg:
            line = line[1:]
        dir_only = line.endswith("/")
        rules.append((line.strip("/"), neg, dir_only))

    def ignored(rel_posix: str, is_dir: bool = False) -> bool:
        hit = False
        parts = rel_posix.split("/")
        for pat, neg, dir_only in rules:
            if dir_only and not is_dir and pat not in parts:
                continue
            if fnmatch.fnmatch(rel_posix, pat) or any(fnmatch.fnmatch(p, pat) for p in parts):
                hit = not neg
        return hit

    return ignored


def _resolve(root: str | None, rel: str | None) -> Path:
    base = Path(root).resolve() if root else Path.cwd()
    p = (base / (rel or "")).resolve()
    if root and p != base and base not in p.parents:
        raise ValueError("path escapes workspace root")
    return p


def guard(fn):
    def wrapper(self, args):
        try:
            return fn(self, args)
        except ValueError as exc:
            return False, str(exc)

    wrapper.__name__ = fn.__name__
    return wrapper


def is_privileged_path(raw: str) -> bool:
    """True when a relative target would let the agent rewrite its own config.

    ``.env`` (any depth) holds API keys / provider overrides, and the
    ``.saturday/`` state files below each shift what Saturday will execute,
    allow, or trust on a future run - a prompt-injected model must not be
    able to persist any of them via the (normally unasked) write tools:
      - mcp.json             names local commands that spawn on future runs
      - hooks.json           shell commands run on EVERY tool call
      - config.json          flips safety_mode / injects verify_command
      - approvals.json       the agent would write its own allow rules
      - schedules.json       cron entries firing unattended agent runs
      - trusted_projects.json  pre-approves projects (gates mcp.json loading)
      - projects.json        project workspaces + authorization scopes
      - usage.jsonl          the usage audit trail
      - file_journal.jsonl   the undo trail for /revert: rewriting it would
                             erase the ability to roll back agent edits
      - SOUL.md              the persistent identity block for every session"""
    p = str(raw or "").replace("\\", "/").lower()
    parts: list[str] = []
    for seg in p.split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        if seg.startswith("~"):
            continue
        parts.append(seg)
    if any(s == ".env" or s.startswith(".env.") for s in parts):
        return True
    return any(
        parts[i] == ".saturday" and parts[i + 1] in _PRIVILEGED_SATURDAY_FILES
        for i in range(len(parts) - 1)
    )


def _is_privileged_target(root: str | None, raw: str, resolved: Path) -> bool:
    """Apply the privileged-file policy to both the spelling and target.

    The lexical check preserves the public helper's behavior, while the
    resolved check closes the symlink/junction bypass where ``safe.txt``
    actually points at ``.saturday/config.json``.
    """
    if is_privileged_path(raw):
        return True
    base = Path(root).resolve() if root else Path.cwd().resolve()
    try:
        relative = resolved.relative_to(base)
    except ValueError:
        return False
    return is_privileged_path(relative.as_posix())


_PRIVILEGED_SATURDAY_FILES = {
    "mcp.json",
    "file_journal.jsonl",
    "hooks.json",
    "config.json",
    "approvals.json",
    "schedules.json",
    "trusted_projects.json",
    "projects.json",
    "usage.jsonl",
    "soul.md",
}

_PRIVILEGED_WRITE_MSG = (
    "refusing to modify a privileged config file (.env, or Saturday state under "
    ".saturday/ such as hooks.json / config.json / approvals.json / mcp.json): "
    "edit it manually outside the agent session"
)


def reindent_for_span(text: str, start: int, old: str, new: str) -> str:
    """Shift ``new`` into the indentation the matched span actually sits at.

    The flexible matcher locates tokens, so the span begins at the first token
    and line 1 of ``new`` is spliced after the file's own indentation. Lines
    2..n arrived with whatever indentation the model used, which is routinely
    different - a body at 12 spaces edited with a 2 space snippet produced a
    file mixing both, with the replacement lines dedented out of their block.
    The syntax check ran after the file had already been written.

    Relative indentation inside ``new`` is preserved: every line after the
    first moves by the same delta."""
    line_start = text.rfind("\n", 0, start) + 1
    file_indent = text[line_start:start]
    if file_indent.strip():                     # match did not begin a line
        return new
    old_lines = str(old).strip("\n").split("\n")
    old_indent = old_lines[0][: len(old_lines[0]) - len(old_lines[0].lstrip())]
    delta = len(file_indent) - len(old_indent)
    if delta == 0:
        return new
    out = []
    for i, line in enumerate(str(new).split("\n")):
        if i == 0 or not line.strip():
            out.append(line)
        elif delta > 0:
            out.append(" " * delta + line)
        else:
            strip = min(-delta, len(line) - len(line.lstrip()))
            out.append(line[strip:])
    return "\n".join(out)


def flexible_match(text: str, old: str) -> tuple[int, int] | None:
    """Locate ``old`` in ``text`` tolerating whitespace differences.

    Exact matching fails constantly in practice: models emit normalized
    indentation, trailing spaces differ. Newlines are preserved as line
    boundaries — a multi-line old_string must NOT silently match the same
    tokens joined onto one line, which would rewrite a semantically
    different span. Returns the (start, end) span of a UNIQUE match, or
    None when zero or 2+ candidates exist (ambiguity must fail loudly)."""
    if not str(old or "").strip():
        return None
    lines = str(old).strip("\n").split("\n")
    line_patterns = []
    for line in lines:
        tokens = [re.escape(part) for part in line.split()]
        if not tokens:
            line_patterns.append(None)  # blank line: match exactly one blank line
            continue
        line_patterns.append(r"[^\S\n]+".join(tokens))
    parts: list[str] = []
    for i, pat in enumerate(line_patterns):
        if i:
            parts.append(r"[^\S\n]*\n[^\S\n]*")
        parts.append(pat if pat is not None else r"[^\S\n]*\n[^\S\n]*")
    try:
        rx = re.compile("".join(parts))
    except re.error:
        return None
    it = rx.finditer(text)
    first = next(it, None)
    if first is None or next(it, None) is not None:
        return None  # zero matches, or ambiguous (2+)
    return first.start(), first.end()


def _verify_note(path: Path, content: str) -> str:
    """Post-write verification: syntax-check Python files (stdlib ast only).

    Returns a warning appended to the tool output so the model can self-correct
    on its next step; never blocks the write itself (mid-refactor WIP is legal)."""
    if path.suffix.lower() not in (".py", ".pyw"):
        return ""
    try:
        import ast

        try:
            ast.parse(content)
            return ""
        except SyntaxError as exc:
            where = f"line {exc.lineno}" if exc.lineno else "offset ?"
            return f"\n[verify] WARNING: {path.name} has a Python syntax error ({where}): {exc.msg}"
    except Exception:
        return ""


def external_verify_note(command: str, path: Path, root: str | None, timeout: float = 30.0) -> str:
    """Run the user's configured verify command after a successful file write.

    ``{path}`` in the command is substituted with the written file's path,
    shell-quoted so a model-chosen filename can never inject extra commands.
    Output is appended to the tool result (never blocks, never raises) so the
    model sees test/lint failures on its very next step."""
    import os
    import shlex

    # WHY: raw str(path) into shell=True let "x; rm -rf ~.py" style filenames
    # execute; quote for the platform's default shell instead.
    if os.name == "nt":
        import subprocess

        quoted = subprocess.list2cmdline([str(path)])
    else:
        quoted = shlex.quote(str(path))
    cmd = command.replace("{path}", quoted)
    try:
        import subprocess

        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(root) if root else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
        status = "ok" if proc.returncode == 0 else f"exit={proc.returncode}"
        short = cmd if len(cmd) <= 60 else cmd[:57] + "..."
        note = f"\n[verify {status}] {short}"
        if tail:
            note += "\n" + tail
        return note
    except Exception as exc:
        return f"\n[verify] WARNING: verify command failed to run: {type(exc).__name__}: {exc}"


class ReadFile(Tool):
    name = "read_file"
    description = "Read a text file from the workspace with optional line range."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "1-indexed start line"},
            "limit": {"type": "integer", "description": "max lines to read"},
        },
        "required": ["path"],
    }

    def __init__(self, root: str | None = None) -> None:
        self.root = root

    @guard
    def run(self, args: dict) -> tuple[bool, str]:
        path = _resolve(self.root, args.get("path"))
        if not path.is_file():
            return False, f"not a file: {path}"
        data = path.read_text(encoding="utf-8", errors="replace")
        lines = data.splitlines()
        offset = max(int(args.get("offset") or 1), 1)
        limit = int(args.get("limit") or 2000)
        window = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{offset + i}: {line}" for i, line in enumerate(window))
        if len(numbered) > 60_000:
            numbered = numbered[:60_000] + "\n... [truncated]"
        return True, numbered or "(empty file)"


class WriteFile(Tool):
    name = "write_file"
    description = "Create or overwrite a file in the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, root: str | None = None, verify_command: str = "") -> None:
        self.root = root
        self.verify_command = verify_command or ""

    @guard
    def run(self, args: dict) -> tuple[bool, str]:
        path = _resolve(self.root, args.get("path"))
        if _is_privileged_target(self.root, args.get("path") or "", path):
            return False, _PRIVILEGED_WRITE_MSG
        content = args.get("content", "")
        existed = path.exists()
        # journal EVERY write (creation tombstones included): skipping creates
        # meant /revert could not undo agent-created files at all
        from saturday.tools.journal import record_edit

        record_edit(self.root or path.parent, "write_file", str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        # preserve the ending style of a file being overwritten
        eol = "\n"
        if path.is_file():
            try:
                _, eol = read_source(path)
            except (UnicodeDecodeError, OSError):
                eol = "\n"
        write_source(path, content, eol)
        note = _verify_note(path, content)
        if self.verify_command and not note.startswith("\n[verify] WARNING"):
            # a failing syntax check already tells the story; the external hook
            # would only repeat it
            note += external_verify_note(self.verify_command, path, self.root)
        return True, f"{'overwrote' if existed else 'created'} {path} ({len(content)} bytes)" + note


class EditFile(Tool):
    name = "edit_file"
    description = "Replace an exact substring in a file. old_string must match exactly and uniquely."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, root: str | None = None, verify_command: str = "") -> None:
        self.root = root
        self.verify_command = verify_command or ""

    @guard
    def run(self, args: dict) -> tuple[bool, str]:
        path = _resolve(self.root, args.get("path"))
        if _is_privileged_target(self.root, args.get("path") or "", path):
            return False, _PRIVILEGED_WRITE_MSG
        if not path.is_file():
            return False, f"not a file: {path}"
        try:
            text, eol = read_source(path)
        except UnicodeDecodeError as exc:
            return False, str(exc.reason)
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if not old.strip():
            return False, "old_string is empty"
        count = text.count(old)
        fuzzy_note = ""
        if count == 0:
            # exact match failed: try whitespace-tolerant location before
            # giving up (reindented snippets, trailing spaces)
            span = flexible_match(text, old)
            if span is None:
                return False, "old_string not found"
            start, end = span
            shifted = reindent_for_span(text, start, old, new)
            updated = text[:start] + shifted + text[end:]
            fuzzy_note = " (matched via whitespace-flexible fallback)"
            if shifted != new:
                fuzzy_note = " (matched via whitespace-flexible fallback, re-indented to match the file)"
        elif count > 1:
            return False, f"old_string matches {count} times; add context to make it unique"
        else:
            updated = text.replace(old, new, 1)
        from saturday.tools.journal import record_edit

        record_edit(self.root or path.parent, "edit_file", str(path))
        write_source(path, updated, eol)
        note = _verify_note(path, updated)
        if self.verify_command and not note.startswith("\n[verify] WARNING"):
            note += external_verify_note(self.verify_command, path, self.root)
        return True, f"edited {path}{fuzzy_note}" + note


def read_source(path) -> tuple[str, str]:
    """Read a text file faithfully: (text with LF endings, dominant EOL).

    `read_text(errors="replace")` turned every byte it could not decode into
    U+FFFD, and `write_text` then wrote that back - so one edit to an
    unrelated token on a Latin-1 source permanently destroyed the bytes it
    never touched. Refusing is the only honest option: a tool that cannot
    represent the file must not rewrite it.

    Line endings are normalised to LF for matching and restored on write, so
    a CRLF checkout does not come back as a whole-file diff.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnicodeDecodeError(
            exc.encoding, exc.object, exc.start, exc.end,
            f"{path} is not valid UTF-8 (byte {exc.start}); refusing to edit it "
            "rather than replace the bytes it cannot decode",
        ) from None
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return text.replace("\r\n", "\n"), ("\r\n" if crlf and crlf >= lf else "\n")


def write_source(path, text: str, eol: str = "\n") -> None:
    """Write text back with its original line endings, as bytes.

    `write_text` applies universal newlines, which silently converted every
    CRLF file to LF."""
    data = text.replace("\r\n", "\n")
    if eol != "\n":
        data = data.replace("\n", eol)
    path.write_bytes(data.encode("utf-8"))


class ListDir(Tool):
    name = "list_dir"
    description = "List files and directories at a path."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "relative to workspace root; default '.'"}},
        "required": [],
    }

    def __init__(self, root: str | None = None) -> None:
        self.root = root

    @guard
    def run(self, args: dict) -> tuple[bool, str]:
        path = _resolve(self.root, args.get("path") or ".")
        if not path.exists():
            return False, f"path not found: {path}"
        entries = []
        try:
            for e in sorted(path.iterdir(), key=lambda x: x.name):
                suffix = "/" if e.is_dir() else ""
                entries.append(e.name + suffix)
        except PermissionError as exc:
            return False, str(exc)
        return True, "\n".join(entries) or "(empty)"


def _confined(base: Path, p: Path) -> Path | None:
    """Resolved match path when it stays inside ``base``, else None.

    WHY: glob joins '..' components lexically (and follows symlinked entries),
    so a pattern like '../*.py' yields matches that only FAIL the check when
    resolved — without this, grep/glob read outside the workspace."""
    try:
        rp = p.resolve()
    except OSError:
        return None
    if rp != base and base not in rp.parents:
        return None
    return rp


class GlobTool(Tool):
    name = "glob"
    description = "Find files matching a glob pattern (e.g. src/**/*.py)."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "limit": {"type": "integer", "description": "max results (default 500)"},
        },
        "required": ["pattern"],
    }

    def __init__(self, root: str | None = None, max_results: int = 500) -> None:
        self.root = root
        self.max_results = max_results

    @guard
    def run(self, args: dict) -> tuple[bool, str]:
        pattern = recursive_pattern(args.get("pattern") or "**/*")
        base = (Path(self.root) if self.root else Path.cwd()).resolve()
        # a cap the caller can raise, not a wall it cannot see past
        cap = max(1, int(args.get("limit") or self.max_results))
        ignored = gitignore_filter(base)
        matches: list[str] = []
        truncated = False
        try:
            for p in base.glob(pattern):
                rp = _confined(base, p)
                if rp is None:
                    continue
                if any(part in IGNORED_DIRS for part in rp.parts):
                    continue
                rel = rp.relative_to(base).as_posix()
                if ignored(rel, rp.is_dir()):
                    continue
                matches.append(rel)
                if len(matches) >= cap:
                    truncated = True
                    break
        except (OSError, ValueError) as exc:
            return False, f"bad pattern: {exc}"
        if not matches:
            return True, "(no matches)"
        out = "\n".join(sorted(matches))
        if truncated:
            # silence here read as "this is everything", which is how a model
            # concludes code does not exist
            out += f"\n... stopped at {cap} match{'' if cap == 1 else 'es'}; pass limit= to raise it"
        return True, out


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with a regex. Returns matching lines with file:line. "
        "Binary files are skipped automatically."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regex"},
            "include": {"type": "string", "description": "glob filter like *.py (searches all subdirectories)"},
            "limit": {"type": "integer", "description": "max matches (default 200)"},
            "ignore_case": {"type": "boolean", "description": "case-insensitive matching (default false)"},
        },
        "required": ["pattern"],
    }

    def __init__(self, root: str | None = None, max_results: int = 200) -> None:
        self.root = root
        self.max_results = max_results

    @staticmethod
    def _is_binary(path: Path) -> bool:
        """Null-byte sniff of the first 8 KB (ripgrep's heuristic, stdlib-only)."""
        try:
            with path.open("rb") as fh:
                return b"\x00" in fh.read(8192)
        except OSError:
            return True

    @guard
    def run(self, args: dict) -> tuple[bool, str]:
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            rx = re.compile(args["pattern"], flags)
        except re.error as exc:
            return False, f"bad regex: {exc}"
        include = recursive_pattern(args.get("include") or "**/*")
        base = (Path(self.root) if self.root else Path.cwd()).resolve()
        cap = max(1, int(args.get("limit") or self.max_results))
        ignored = gitignore_filter(base)
        results: list[str] = []
        try:
            matches = base.glob(include)
            for p in matches:
                rp = _confined(base, p)
                if rp is None or not rp.is_file():
                    continue
                if any(part in IGNORED_DIRS for part in rp.parts):
                    continue
                try:
                    if rp.stat().st_size > 2_000_000 or self._is_binary(rp):
                        continue
                    text = rp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                try:
                    rel = rp.relative_to(base).as_posix()
                except ValueError:
                    continue
                if ignored(rel):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{rel}:{i}: {line.strip()[:300]}")
                        if len(results) >= cap:
                            return True, "\n".join(results) + (
                                f"\n... stopped at {cap} match{'' if cap == 1 else 'es'}; pass limit= to raise it")
        except (OSError, ValueError) as exc:
            return False, f"bad include pattern: {exc}"
        return True, "\n".join(results) or "(no matches)"
