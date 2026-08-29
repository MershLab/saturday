"""Pure helpers behind the desktop app surface (extracted from webui.py).

Nothing here touches HTTP or sockets: session hydration, cross-chat search,
title derivation, upload persistence and .env upserts. Keeping them separate
makes them unit-testable without a server and keeps webui.py transport-only.
Stdlib-only, like the rest of the core.
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
import time
from pathlib import Path


def _safe_sid(sid: str) -> str:
    """Session ids key filesystem paths (uploads); keep them charset-safe."""
    safe = "".join(c for c in str(sid or "") if c.isalnum() or c in "-_")[:64]
    return safe or "session"


def _title_from_text(text: str, cap: int = 60) -> str:
    """Readable session title: drop code fences/markdown noise, collapse space."""
    t = text or ""
    t = re.sub(r"```[\s\S]*?(?:```|$)", "\x00code\x00", t)
    t = re.sub(r"[#>*_`~\[\]]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:cap].replace("\x00", "").strip() or "(interactive)"


def _save_data_urls(sid: str, data_urls: list[str]) -> tuple[list[str], str | None]:
    uploads = Path(tempfile.gettempdir()) / "saturday-uploads" / _safe_sid(sid)
    uploads.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for i, du in enumerate(data_urls[:4]):
        m = re.match(r"^data:image/(png|jpe?g|gif|webp|bmp);base64,(.+)$", du or "", re.DOTALL)
        if not m:
            continue
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            continue
        if len(raw) > 8 * 1024 * 1024:
            return paths, f"image {i + 1} exceeds 8 MB"
        p = uploads / f"att-{int(time.time())}-{i}.{m.group(1).replace('jpeg', 'jpg')}"
        p.write_bytes(raw)
        paths.append(str(p))
    return paths, None


def search_sessions(store, query: str, limit: int = 20) -> list[dict]:
    """Full-text scan across ALL sessions' user/assistant messages.

    Scans every session regardless of age (a cap here once made old chats
    unsearchable); per-session parsing is the same bounded shape as hydration.
    Returns ranked matches (hit count desc) with a short snippet each.
    Local-first: nothing leaves the machine."""
    q = (query or "").strip().lower()
    if not q:
        return []
    out: list[dict] = []
    scanned = 0
    for row in store.list_sessions(limit=None):
        if scanned >= 2000:  # latency guard for extreme histories
            break
        data = store.load(row["id"])
        if not data:
            continue
        scanned += 1
        hits = 0
        snippet = ""
        for rec in data["records"]:
            if rec.get("type") != "messages":
                continue
            for m in rec.get("messages") or []:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                c = m.get("content")
                text = c if isinstance(c, str) else " ".join(
                    p.get("text") or "" for p in c or [] if isinstance(p, dict)
                )
                low = text.lower()
                start = 0
                while True:
                    i = low.find(q, start)
                    if i < 0:
                        break
                    hits += 1
                    if not snippet:
                        s = max(0, i - 50)
                        e = min(len(text), i + len(q) + 70)
                        snippet = (("…" if s else "") + text[s:e].replace("\n", " ") + ("…" if e < len(text) else ""))[:220]
                    start = i + len(q)
        if hits:
            out.append(
                {
                    "sid": row["id"],
                    "task": row.get("task") or "",
                    "project": row.get("project") or "",
                    "hits": hits,
                    "snippet": snippet,
                }
            )
    out.sort(key=lambda d: -d["hits"])
    return out[:limit]


def _env_upsert(path: Path, key: str, value: str) -> None:
    """Insert/update KEY=VALUE in a .env file, preserving other lines."""
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped and stripped.split("=", 1)[0].strip() == key:
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            # drop duplicate entries for the same key
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    _restrict_perms(path)


def _restrict_perms(path: Path) -> None:
    """.env carries provider API keys: owner-only on POSIX. Windows ACLs are
    out of scope for a stdlib-only core (the per-user profile dir already gates)."""
    import os

    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _items_from_messages(messages: list[dict]) -> list[dict]:
    """Render raw chat messages into UI items (user / assistant / tool results).

    Shared by transcript hydration and checkpoint fallback: interrupted runs
    never reach the post-run transcript append, so their only copy of the
    conversation is the per-step checkpoint."""
    items: list[dict] = []

    def last_assistant() -> dict | None:
        return next((it for it in reversed(items) if it["kind"] == "assistant"), None)

    # msg_idx lets the UI branch/truncate from an exact conversation position
    # (edit-&-resend, "branch from here"): /api/branch keep_messages counts
    # raw messages, tool roles included, so UI-side item counters won't do.
    for mi, m in enumerate(messages or []):
        role = m.get("role")
        if role == "user":
            c = m.get("content")
            text_parts: list[str] = []
            images = 0
            if isinstance(c, str):
                text_parts.append(c)
            elif isinstance(c, list):
                for part in c:
                    if not isinstance(part, dict):
                        continue
                    t = part.get("type")
                    if t == "text":
                        text_parts.append(str(part.get("text") or ""))
                    elif t == "image_url":
                        images += 1
            items.append({"kind": "user", "text": "\n".join(text_parts).strip(), "images": images, "msg_idx": mi})
        elif role == "assistant":
            from saturday.prompts.templates import split_reasoning

            reasoning = m.get("reasoning_content") or m.get("reasoning") or ""
            content = m.get("content")
            if isinstance(content, str) and content:
                r2, remainder = split_reasoning(content)
                reasoning = reasoning or r2
                content = remainder or ""
            elif not isinstance(content, str):
                content = ""
            calls = [
                {
                    "id": tc.get("id") or "",
                    "name": (tc.get("function") or {}).get("name", "?"),
                    "args_raw": (tc.get("function") or {}).get("arguments", "{}"),
                }
                for tc in m.get("tool_calls") or []
                if isinstance(tc, dict)
            ]
            item = {"kind": "assistant", "text": content.strip(), "reasoning": reasoning, "calls": calls, "results": {}}
            items.append(item)
        elif role == "tool":
            ok = True
            body = str(m.get("content") or "")
            wrapped = re.fullmatch(r"<tool_response>\n(.*)\n</tool_response>", body, re.DOTALL)
            if wrapped:
                inner = wrapped.group(1)
                # failed tool bodies carry a trailing "\n{RETRY_HINT}" prose
                # line after the JSON: parse only the leading JSON object
                try:
                    parsed = json.loads(inner)
                except (json.JSONDecodeError, AttributeError):
                    decoded = json.JSONDecoder()
                    try:
                        parsed, _ = decoded.raw_decode(inner.lstrip())
                    except (json.JSONDecodeError, ValueError):
                        parsed = None
                if isinstance(parsed, dict) and ("error" in parsed or "content" in parsed):
                    body = parsed.get("error") if parsed.get("error") is not None else parsed.get("content", "")
                    ok = parsed.get("error") is None
            target = last_assistant()
            if target is not None:
                target["results"][m.get("tool_call_id") or ""] = {"ok": ok, "body": body}
    return items


def hydrate_session(store, sid: str) -> dict | None:
    data = store.load(sid)
    if not data:
        return None
    messages: list[dict] = []
    for rec in data["records"]:
        if rec.get("type") == "messages":
            messages.extend(rec.get("messages") or [])
    items = _items_from_messages(messages)
    # Interrupted runs persist only via checkpoints (the transcript append
    # happens once a run finishes); without this fallback those chats
    # hydrate to zero items and render blank.
    if not items:
        items = _items_from_messages(store.load_checkpoint(sid) or [])
    has_checkpoint = store.load_checkpoint(sid) is not None
    return {"id": sid, "meta": data["meta"], "items": items, "resumable": has_checkpoint}


# -- custom slash commands (prompt library) -----------------------------------
# Warp-Drive/Continue-style saved prompts: each entry maps a "/name" command to
# a prompt template (with optional $ARGS substitution). Stored locally in
# CONFIG_DIR/commands.json; the web composer expands them client-side, so no
# agent-loop changes are needed and they work even while a run is queued.

MAX_CUSTOM_COMMANDS = 100


def custom_commands_path() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "commands.json"


def load_custom_commands() -> dict[str, dict]:
    """name -> {prompt, description}; invalid lines are skipped silently."""
    path = custom_commands_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for name, val in raw.items():
        key = str(name).strip().lstrip("/").lower()
        if not key or not isinstance(val, dict):
            continue
        prompt = str(val.get("prompt") or "").strip()
        if not prompt:
            continue
        out[key] = {
            "prompt": prompt[:8000],
            "description": str(val.get("description") or "")[:200],
        }
    return out


def save_custom_commands(cmds: dict[str, dict]) -> None:
    if len(cmds) > MAX_CUSTOM_COMMANDS:
        raise ValueError(f"too many commands (max {MAX_CUSTOM_COMMANDS})")
    path = custom_commands_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cmds, indent=2, ensure_ascii=False), encoding="utf-8")


# -- per-turn feedback (local RL-style reward signal) --------------------------


def append_feedback(entry: dict) -> None:
    """One JSONL line per rating in CONFIG_DIR/feedback.jsonl. Local-only."""
    from saturday.config import get_config_dir

    p = get_config_dir() / "feedback.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
