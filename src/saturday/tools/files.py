from __future__ import annotations

import re
from pathlib import Path

from saturday.tools.base import Tool

IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".idea", ".vscode"}


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
        path.write_text(content, encoding="utf-8")
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
        text = path.read_text(encoding="utf-8", errors="replace")
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
            updated = text[:start] + new + text[end:]
            fuzzy_note = " (matched via whitespace-flexible fallback)"
        elif count > 1:
            return False, f"old_string matches {count} times; add context to make it unique"
        else:
            updated = text.replace(old, new, 1)
        from saturday.tools.journal import record_edit

        record_edit(self.root or path.parent, "edit_file", str(path))
        path.write_text(updated, encoding="utf-8")
        note = _verify_note(path, updated)
        if self.verify_command and not note.startswith("\n[verify] WARNING"):
            note += external_verify_note(self.verify_command, path, self.root)
        return True, f"edited {path}{fuzzy_note}" + note


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
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    }

    def __init__(self, root: str | None = None) -> None:
        self.root = root

    @guard
    def run(self, args: dict) -> tuple[bool, str]:
        pattern = str(args.get("pattern") or "**/*")
        base = (Path(self.root) if self.root else Path.cwd()).resolve()
        matches: list[str] = []
        try:
            for p in base.glob(pattern):
                rp = _confined(base, p)
                if rp is None:
                    continue
                if any(part in IGNORED_DIRS for part in rp.parts):
                    continue
                matches.append(rp.relative_to(base).as_posix())
                if len(matches) >= 500:
                    break
        except (OSError, ValueError) as exc:
            return False, f"bad pattern: {exc}"
        return True, "\n".join(sorted(matches)) or "(no matches)"


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
            "include": {"type": "string", "description": "glob filter like *.py"},
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
        include = args.get("include") or "**/*"
        base = (Path(self.root) if self.root else Path.cwd()).resolve()
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
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{rel}:{i}: {line.strip()[:300]}")
                        if len(results) >= self.max_results:
                            return True, "\n".join(results) + "\n... [more results withheld]"
        except (OSError, ValueError) as exc:
            return False, f"bad include pattern: {exc}"
        return True, "\n".join(results) or "(no matches)"
