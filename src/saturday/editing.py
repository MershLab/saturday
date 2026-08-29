"""File-edit domain logic shared by every surface (REPL + web UI).

Both surfaces gate ``write_file``/``edit_file`` with a unified-diff preview,
so the edit-tool list and the diff renderer live HERE instead of in one
surface's module (the old ``saturday.repl`` import from ``session_runtime``
made the web surface depend on the terminal surface). Stdlib-only."""
from __future__ import annotations

import difflib
from pathlib import Path

FILE_EDIT_TOOLS = ("write_file", "edit_file")
DIFF_MAX_LINES = 60


def norm(text: str) -> str:
    """Whitespace-collapse a command/path for approval-memory keys."""
    return " ".join((text or "").split())


# session-runtime legacy alias (imports kept stable for embedders/tests)
_norm = norm


def render_file_diff(tool_name: str, args: dict, root: str | None = None) -> str | None:
    """Unified diff preview for write_file/edit_file; None when it can't be shown.

    ``root`` is the workspace root the edit will resolve against — previews
    MUST read the same file bytes the tool will (relative paths resolve
    against the workspace, not necessarily the process CWD)."""
    path = str(args.get("path") or "")
    if not path:
        return None
    p = Path(root) / path if (root and not Path(path).is_absolute()) else Path(path)
    try:
        old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    except OSError:
        return None
    if tool_name == "write_file":
        new = str(args.get("content") or "")
    elif tool_name == "edit_file":
        old_str = args.get("old_string")
        new_str = args.get("new_string")
        if old_str is None or not str(old_str).strip():
            return f"(preview unavailable: empty or missing old_string for {p.name})"
        # mirror EditFile's rules exactly: an approval must never show a diff
        # the tool would then refuse to apply (or apply differently)
        count = old.count(str(old_str))
        if count == 1:
            new = old.replace(str(old_str), str(new_str if new_str is not None else ""), 1)
        else:
            from saturday.tools.files import flexible_match

            span = flexible_match(old, str(old_str)) if count == 0 else None
            if span is None:
                reason = f"matches {count} times" if count > 1 else "not found in current contents"
                return f"(preview unavailable: old_string {reason} in {p.name})"
            old_str = old[span[0]:span[1]]
            new = old.replace(old_str, str(new_str if new_str is not None else ""), 1)
    else:
        return None
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            lineterm="",
        )
    )
    if not lines:
        return "(no changes)"
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"... {len(lines) - DIFF_MAX_LINES} more diff lines"]
    return "\n".join(lines)
