"""First-use trust gating for per-project config files.

``.env`` and ``.saturday/mcp.json`` in the working directory can redirect
API traffic or spawn local processes, so a repo must be explicitly trusted
before Saturday honors them. Decisions are remembered per project root in
``<CONFIG_DIR>/trusted_projects.json``; non-interactive runs fail closed.

Set ``SATURDAY_TRUST_ALL_PROJECTS=1`` to skip prompts (CI/automation).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

TRUST_ENV = "SATURDAY_TRUST_ALL_PROJECTS"


def _store_path() -> Path:
    from saturday.config import CONFIG_DIR

    return CONFIG_DIR / "trusted_projects.json"


def _key(root: Path) -> str:
    try:
        resolved = str(Path(root).resolve()).lower()
    except OSError:
        resolved = str(root).lower()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def _read_store() -> dict:
    p = _store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_store(data: dict) -> None:
    p = _store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def _interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stderr.isatty())
    except Exception:
        return False


def is_trusted(root: Path | str) -> bool:
    """Has this project already been approved? A read-only check.

    ensure_trusted() prompts and records; callers that only need to REPORT
    the current state (the web UI listing project config) must not prompt,
    and must not record a decision as a side effect of rendering a page."""
    if os.environ.get(TRUST_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return _key(Path(root)) in set(_read_store().get("approved") or [])


def ensure_trusted(root: Path | str, what: str, detail: list[str] | None = None) -> bool:
    """True when the project may load its local config files.

    Prompts once interactively and records the decision; fails closed in
    non-interactive contexts (with a stderr hint) unless the env override is
    set."""
    override = os.environ.get(TRUST_ENV, "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    k = _key(Path(root))
    store = _read_store()
    approved = set(store.get("approved") or [])
    denied = set(store.get("denied") or [])
    if k in approved:
        return True
    # A past DENY is not permanent: the decline message below promises
    # "re-run to change your mind", so fall through and re-prompt (an
    # interactive user can flip to trusted; non-interactive still fails closed).
    if not _interactive():
        print(
            f"[saturday] ignoring untrusted project config ({what}); "
            f"run once in a terminal to approve it, or set {TRUST_ENV}=1",
            file=sys.stderr,
        )
        return False
    print(f"[saturday] this project asks Saturday to load {what}:")
    for line in (detail or [])[:10]:
        print(f"  - {line}")
    try:
        answer = input(f"Trust '{Path(root)}' and load it? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt, OSError):
        answer = ""
    if answer in ("y", "yes"):
        approved.add(k)
        store["approved"] = sorted(approved)
        # Drop any stale deny entry so the store reflects the flip to trusted.
        store["denied"] = sorted(denied - {k})
        _write_store(store)
        return True
    denied.add(k)
    store.setdefault("approved", [])
    store["denied"] = sorted(denied)
    _write_store(store)
    print(f"[saturday] not trusted; {what} will stay ignored (re-run to change your mind)", file=sys.stderr)
    return False


def pending_trust_items(cwd: Path | None = None) -> list[dict]:
    """Return project config items that exist in *cwd* but are not yet trusted.

    Each entry: {"kind": "env"|"mcp"|"hooks", "path": str, "detail": [str]}
    An empty list means either all items are already trusted or none are present.
    This function is read-only and never prompts."""
    root = Path(cwd or ".").resolve()
    override = os.environ.get(TRUST_ENV, "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return []  # global override active; nothing is pending
    k = _key(root)
    store = _read_store()
    approved = set(store.get("approved") or [])
    if k in approved:
        return []  # already trusted

    items: list[dict] = []

    # CWD .env
    env_path = root / ".env"
    if env_path.is_file():
        items.append({"kind": "env", "path": str(env_path), "detail": []})

    # .saturday/mcp.json
    mcp_path = root / ".saturday" / "mcp.json"
    if mcp_path.is_file():
        detail: list[str] = []
        try:
            import json as _json
            data = _json.loads(mcp_path.read_text(encoding="utf-8"))
            raw = data.get("servers") if isinstance(data.get("servers"), dict) else data
            if isinstance(raw, dict):
                for alias, spec in list(raw.items())[:10]:
                    if isinstance(spec, dict):
                        if "command" in spec:
                            entry = (f"{alias}: {spec['command']} "
                                     + " ".join(str(a) for a in (spec.get("args") or []))).strip()
                        elif "url" in spec:
                            entry = f"{alias}: {spec['url']}"
                        else:
                            entry = f"{alias}: (invalid)"
                        detail.append(entry)
        except Exception:
            pass
        items.append({"kind": "mcp", "path": str(mcp_path), "detail": detail})

    # .saturday/hooks.json: executable project config needs the same trust
    # decision even though it does not spawn a long-lived MCP process.
    hooks_path = root / ".saturday" / "hooks.json"
    if hooks_path.is_file():
        items.append({"kind": "hooks", "path": str(hooks_path), "detail": []})

    return items


def record_decision(root: Path | str, *, trusted: bool) -> None:
    """Persist a trust or deny decision without an interactive prompt.

    Used by the web UI POST /api/trust handler so the browser can serve as the
    trust gate when there is no TTY available."""
    k = _key(Path(root))
    store = _read_store()
    approved = set(store.get("approved") or [])
    denied = set(store.get("denied") or [])
    if trusted:
        approved.add(k)
        denied.discard(k)
    else:
        denied.add(k)
        approved.discard(k)
    store["approved"] = sorted(approved)
    store["denied"] = sorted(denied)
    _write_store(store)
