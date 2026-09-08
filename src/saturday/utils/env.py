from __future__ import annotations

import os
import sys
from pathlib import Path


# Keys that control harness security or policy posture: repo content must never
# set these, however trusted the project is. Trusting a project means trusting
# its provider config, not handing it the approval gate. Both readers below
# filter against this one list; it used to be copied into each of them, and a
# key added to one stayed allowed in the other.
#
# The test for membership is whether the key can weaken a gate, not whether it
# sounds dangerous: SATURDAY_YOLO sets safety_mode=autonomous and
# SATURDAY_WORKSPACE moves the file confinement root, so both belong here even
# though neither mentions trust or sandboxing.
BLOCKED_PROJECT_KEYS = frozenset({
    "SATURDAY_TRUST_ALL_PROJECTS",
    "SATURDAY_HOME",
    "SATURDAY_APPROVAL_TTL",
    "SATURDAY_GUARDRAILS",
    "SATURDAY_SANDBOXED",
    "SATURDAY_BACKGROUND_ONLY",
    "SATURDAY_ALLOW_LOCAL_FETCH",
    "SATURDAY_VERIFY_CMD",
    "SATURDAY_PROVENANCE",
    "SATURDAY_INJECTION_GUARD",
    "SATURDAY_BLOCKED_APPS",
    "SATURDAY_YOLO",
    "SATURDAY_WORKSPACE",
})


def load_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs into os.environ (existing env always wins).

    An explicit ``path`` is user-directed and always trusted. The implicit
    candidates are the CWD ``.env`` (gated behind project trust - a cloned
    repo must not get to set provider/base-URL vars silently) and the
    user-level ``~/.saturday/.env`` (always trusted).

    Project-scoped files may NOT set harness-CONTROL keys (denylist below):
    letting repo content define e.g. SATURDAY_TRUST_ALL_PROJECTS would let a
    cloned repo upgrade its own trust and silently disable every future
    gate (MCP spawn approval, SSRF guard, guardrails). Benign config like
    SATURDAY_MODEL stays allowed once the project is trusted. User-controlled
    scopes (explicit path, user-global file) keep everything allowed."""
    from saturday.utils.trust import ensure_trusted

    if path:
        # (file, trusted, scope): explicit path = user-directed content.
        candidates = [(Path(path), True, "user")]
    else:
        from saturday.config import get_config_dir

        cwd_env = Path(".env")
        candidates = [
            (cwd_env, ensure_trusted(cwd_env.parent, what=f".env in this directory ({cwd_env.resolve().parent})"), "project"),
            (get_config_dir() / ".env", True, "user"),
        ]
    loaded: dict[str, str] = {}
    for candidate, trusted, scope in candidates:
        if not candidate.is_file():
            continue
        if not trusted:
            print(f"[saturday] skipped {candidate} (untrusted)", file=sys.stderr)
            continue
        for raw in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if scope == "project" and key in BLOCKED_PROJECT_KEYS:
                # repo content must never control harness gates
                continue
            if key and key not in os.environ:
                os.environ[key] = value
            loaded[key] = value
    return loaded


def reload_trusted_env(cwd: Path | str | None = None) -> dict[str, str]:
    """Re-run env loading for the CWD project after trust has been recorded.

    Unlike load_env_file(None) this skips the trust gate (the caller has already
    written the trust decision via record_decision()) and reads the file directly.
    Existing os.environ keys are never overwritten (same semantics as the main
    loader).  Returns the newly applied key/value pairs."""
    root = Path(cwd or ".").resolve()


    env_path = root / ".env"
    loaded: dict[str, str] = {}
    if not env_path.is_file():
        return loaded
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in BLOCKED_PROJECT_KEYS:
            continue
        if key and key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded
