"""Saturday desktop app surface: a polished local web UI in a native app window.

One core, many surfaces: this module drives the exact same Agent/loop/tools/safety
stack as the CLI and TUI (mirrors repl.py's approval wiring) behind a small HTTP
API, and launches an Edge/Chrome ``--app`` window so it feels like a desktop app.
Stdlib-only, like the rest of the core.
"""
from __future__ import annotations

import copy
import errno
import hmac
import json
import os
import re
import sys
import secrets
import socket
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue

ASSETS_DIR = Path(__file__).parent / "webui_assets"
DEFAULT_PORT = 8679
MAX_BODY = 32 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
KNOWLEDGE_PER_FILE_CHARS = 20_000
KNOWLEDGE_TOTAL_CHARS = 60_000

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}

# Session runtime machinery lives in session_runtime.py (approvals bridge,
# event bus, run-state machine); pure content helpers live in webui_support.py.
# Re-exported here so existing imports (tests, embedders) keep working.
from saturday.session_runtime import (  # noqa: E402,F401
    RunStopped,
    SessionRuntime,
    WebApprover,
    WebFileGate,
    _Bus,
    _SessionRuntime,
    _norm,
    install_web_surface as _install_web_surface,
)
from saturday.webui_support import (  # noqa: E402,F401
    _env_upsert,
    _safe_sid,
    _save_data_urls,
    _title_from_text,
    hydrate_session,
    search_sessions,
)


def _reveal_path(path: str) -> None:
    """Open a folder in the OS file manager (injectable for tests)."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------------------
# Slash commands (parity with repl.dispatch, minus terminal specifics).
# The registry, autocomplete list and aliases live in saturday.slash — the
# single source of truth shared with the REPL — and are re-exported here so
# existing imports (tests, embedders, /api/state) keep working.
from saturday.slash import SLASH_ALIASES, SLASH_COMMAND_LIST  # noqa: E402,F401


def handle_slash(rt: _SessionRuntime, line: str) -> list[dict]:
    """Return notice events for a slash command; [] when not a command."""
    if not line.startswith("/"):
        return []
    cmd, _, arg = line.partition(" ")
    cmd = SLASH_ALIASES.get(cmd.strip().lower(), cmd.strip().lower())
    arg = arg.strip()
    from saturday.slash import COMMANDS, SlashContext

    sc = COMMANDS.get(cmd if cmd.startswith("/") else f"/{cmd}")
    ctx = SlashContext.for_runtime(rt)
    if sc is None:
        ctx.out(f"unknown command /{cmd.split('/')[-1] if '/' in cmd else cmd}; try /help")
    else:
        sc.run_web(ctx, arg)
    events = [{"t": "notice", "s": s} for s in ctx.lines if s]
    if sc is not None and sc.web_event is not None:
        events.append(sc.web_event(ctx))
    return events


# ---------------------------------------------------------------------------
# Config patch validation (spec-driven; consumed by AppState.apply_config).
# Every entry validates one key of the settings PATCH. Returning _CFG_SKIP
# means "invalid-but-silent": keep the previous value, omit from applied.
# Raising ValueError surfaces the message verbatim as an HTTP 400.

_CFG_SKIP = object()


class _CfgApplyState:
    """Scratch state threaded through _CONFIG_FIELDS validators."""

    __slots__ = ("cfg", "known_families", "reg_names", "extra_applied")

    def __init__(self, cfg):
        self.cfg = cfg
        self.known_families: set[str] = set()  # filled outside the cfg lock
        self.reg_names: set[str] = set()  # filled outside the cfg lock
        self.extra_applied: list[str] = []  # coupling side effects, e.g. persona_mode


def _coerce_str_list(raw):
    if isinstance(raw, str):
        return [p for p in (s.strip() for s in raw.split(",")) if p]
    return raw


def _v_bool(patch, st, key):
    raw = patch.get(key)
    return bool(raw) if isinstance(raw, bool) else _CFG_SKIP


def _b_int_range(lo, hi):
    def v(patch, st, key):
        raw = patch.get(key)
        # isinstance(True, int) passes here deliberately: that pre-existing
        # acceptance quirk is observable behavior, not an oversight to fix
        if isinstance(raw, int) and lo <= raw <= hi:
            return int(raw)
        return _CFG_SKIP

    return v


def _b_float_range(lo, hi):
    def v(patch, st, key):
        raw = patch.get(key)
        if isinstance(raw, (int, float)) and lo <= raw <= hi:
            return float(raw)
        return _CFG_SKIP

    return v


def _b_int_range_opt(lo, hi):
    """Like _b_int_range but None clears the value back to its default."""
    def v(patch, st, key):
        if key not in patch:
            return _CFG_SKIP
        raw = patch[key]
        if raw is None:
            return None
        if isinstance(raw, int) and not isinstance(raw, bool) and lo <= raw <= hi:
            return raw
        return _CFG_SKIP

    return v


def _b_line_text(maxlen):
    """Presence-gated single-line text; overlong or multi-line raises."""

    def v(patch, st, key):
        if key not in patch:
            return _CFG_SKIP
        raw = str(patch[key] or "").strip()
        if len(raw) > maxlen or "\n" in raw:
            raise ValueError(f"{key} must be a single line of at most {maxlen} characters")
        return raw

    return v


def _v_provider(patch, st, key):
    raw = patch.get(key)
    if not raw:
        return _CFG_SKIP
    from saturday.config import PROVIDERS

    prov = str(raw)
    if prov not in PROVIDERS:
        raise ValueError(f"unknown provider '{prov}'")
    st.cfg.model = None  # force default-model resolution for the new provider
    return prov


def _v_model(patch, st, key):
    if key not in patch or not str(patch.get(key) or "").strip():
        return _CFG_SKIP
    return str(patch[key]).strip()


def _v_safety_mode(patch, st, key):
    from saturday.safety import KNOWN_MODES, normalize_mode

    raw = patch.get(key)
    if not isinstance(raw, str):
        return _CFG_SKIP
    candidate = normalize_mode(raw)  # accept aliases ("yolo"/"auto")
    return candidate if candidate in KNOWN_MODES else _CFG_SKIP


def _v_fallback_models(patch, st, key):
    if key not in patch:
        return _CFG_SKIP
    raw = _coerce_str_list(patch[key])
    if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
        raise ValueError("fallback_models must be a list or comma-separated string")
    cleaned = []
    for m in raw[:8]:
        if m.strip() and m.strip() not in cleaned:
            cleaned.append(m.strip())
    return cleaned


def _v_str_list(max_items: int, max_len: int):
    """Generic list-of-strings validator: comma-string or list in, deduped
    and length-capped list out. Unlike disabled_tools this checks no fixed
    vocabulary - the block-lists name providers/models/app categories the
    settings pane has no closed list of."""
    def v(patch, st, key):
        if key not in patch:
            return _CFG_SKIP
        raw = _coerce_str_list(patch[key])
        if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
            raise ValueError(f"{key} must be a list or comma-separated string")
        cleaned = []
        for m in raw[:max_items]:
            m = m.strip()[:max_len]
            if m and m not in cleaned:
                cleaned.append(m)
        return cleaned

    return v


def _v_disabled_tools(patch, st, key):
    if key not in patch:
        return _CFG_SKIP
    raw = _coerce_str_list(patch[key])
    if not isinstance(raw, list) or not all(isinstance(m, str) for m in raw):
        raise ValueError("disabled_tools must be a list or comma-separated string")
    cleaned = []
    for m in raw[:64]:
        m = m.strip()
        if not m or m in cleaned:
            continue
        if m not in st.known_families and m not in st.reg_names:
            raise ValueError(
                f"unknown tool or family '{m}' (families: {', '.join(sorted(st.known_families))})"
            )
        cleaned.append(m)
    return cleaned


def _v_max_run_tokens(patch, st, key):
    if key not in patch:
        return _CFG_SKIP
    raw = patch[key]
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= 10_000_000:
        raise ValueError("max_run_tokens must be an integer between 0 and 10000000 (0 = off)")
    return raw


def _v_auth_scopes(patch, st, key):
    if key not in patch:
        return _CFG_SKIP
    from saturday.projects import clean_scopes

    return clean_scopes(patch[key])


def _v_persona_extra(patch, st, key):
    raw = patch.get(key)
    if key not in patch or not isinstance(raw, str):
        return _CFG_SKIP
    return raw.strip()


def _v_persona_mode(patch, st, key):
    raw = patch.get(key)
    if raw not in ("agent", "assistant"):
        return _CFG_SKIP
    value = str(raw)
    if value == "assistant" and "desktop_background_only" not in patch:
        # an assistant works while the user works: default to
        # non-intrusive computer use unless explicitly overridden
        # in the same request
        st.cfg.desktop_background_only = True
        st.extra_applied.append("desktop_background_only")
    return value


def _v_provenance_marking(patch, st, key):
    if key not in patch:
        return _CFG_SKIP
    raw = str(patch[key] or "").strip().lower()
    if raw not in ("metadata", "visible", "off"):
        raise ValueError("provenance_marking must be metadata|visible|off")
    return raw


def _v_lsp_servers(patch, st, key):
    if key not in patch:
        return _CFG_SKIP
    raw_lsp = patch[key]
    if raw_lsp is None:
        raw_lsp = {}
    if not isinstance(raw_lsp, dict):
        raise ValueError('lsp_servers must be an object like {"python": ["pylsp"]}')
    cleaned_lsp: dict[str, list] = {}
    for lang, cmds in list(raw_lsp.items())[:16]:
        if (
            not isinstance(lang, str)
            or not isinstance(cmds, list)
            or not cmds
            or not all(isinstance(c, str) and c.strip() for c in cmds)
        ):
            raise ValueError(
                f"lsp_servers[{lang!r}] must map to a non-empty list of command strings"
            )
        cleaned_lsp[lang.strip()[:24]] = [c.strip()[:200] for c in cmds][:8]
    return cleaned_lsp


# Order matters: mirrors the historical block sequence byte-for-byte, so any
# reordering changes which error/side effect wins on multi-key patches.
_CONFIG_FIELDS = [
    ("provider", _v_provider),
    ("model", _v_model),
    ("safety_mode", _v_safety_mode),
    ("max_steps", _b_int_range(1, 200)),
    ("temperature", _b_float_range(0, 2)),
    ("max_tokens", _b_int_range(256, 65536)),
    ("fallback_models", _v_fallback_models),
    ("disabled_tools", _v_disabled_tools),
    ("desktop_background_only", _v_bool),
    ("destructive_guardrails", _v_bool),
    ("sandboxed", _v_bool),
    ("max_run_tokens", _v_max_run_tokens),
    ("plan_mode", _v_bool),
    ("auth_scopes", _v_auth_scopes),
    ("persona_extra", _v_persona_extra),
    ("persona_mode", _v_persona_mode),
    ("assistant_name", _b_line_text(40)),
    ("assistant_user_title", _b_line_text(40)),
    ("provenance_marking", _v_provenance_marking),
    ("verify_command", _b_line_text(500)),
    ("keep_reasoning_in_history", _v_bool),
    ("auto_title_sessions", _v_bool),
    ("suggest_followups", _v_bool),
    ("lsp_servers", _v_lsp_servers),
    # advanced execution/model knobs (runtime reads them; the settings pane
    # exposes them so users no longer need config.json for tuning)
    ("top_p", _b_float_range(0, 1)),
    ("request_timeout", _b_float_range(1, 600)),
    ("tool_timeout", _b_float_range(1, 600)),
    ("max_retries", _b_int_range(0, 8)),
    ("memory_max_chars", _b_int_range(1000, 50_000)),
    ("max_context_tokens", _b_int_range_opt(0, 10_000_000)),
    ("compact_above_tokens", _b_int_range_opt(0, 10_000_000)),
    ("stream", _v_bool),
    ("shell_allow_network", _v_bool),
    # unattended-runaway guards and the block-lists: previously config.json
    # or CLI-flag only, now reachable from the settings pane too
    ("max_wall_seconds", _b_int_range(0, 86400)),
    ("max_run_cost_usd", _b_float_range(0, 1000)),
    ("injection_guard", _v_bool),
    ("persist_approvals", _v_bool),
    ("blocked_apps", _v_str_list(32, 40)),
    ("blocked_providers", _v_str_list(16, 40)),
    ("blocked_models", _v_str_list(32, 120)),
    ("memory_nudge_interval", _b_int_range(0, 500)),
]

# Settings that stay project-owned on per-session cfg clones (re-derived from
# the project, never synced from the global config): scopes and persona come
# from the project; workspace_root is not settings-patchable at all.
_PROJECT_OWNED_CONFIG_FIELDS = frozenset({"auth_scopes", "persona_extra"})

# Every other settings key propagates to live per-session cfg clones.
_SHARED_CONFIG_FIELDS = tuple(k for k, _ in _CONFIG_FIELDS if k not in _PROJECT_OWNED_CONFIG_FIELDS)

# Keys captured INTO tool instances / registries at agent construction: a cfg
# setattr alone cannot reach them, so live runtimes must be rebuilt when they
# change (auth_scopes wires the registry, verify_command is baked into the
# file tools, lsp_servers into the LSP tools, memory_max_chars into
# WorkingMemory).
# blocked_apps and persist_approvals are baked into ApprovalPolicy at Agent
# construction time (see Agent.__init__), not read live per run like the
# rest of this session's new fields - a live runtime needs rebuilding to
# pick either one up.
_REBUILD_CONFIG_FIELDS = frozenset({
    "auth_scopes", "verify_command", "lsp_servers", "memory_max_chars",
    "blocked_apps", "persist_approvals",
})


# ---------------------------------------------------------------------------
# Chat worker


def _check_stop(rt: SessionRuntime) -> None:
    if rt.should_stop():
        raise RunStopped()


def _one_shot(cfg, prompt: str, *, max_tokens: int = 64, temperature: float = 0.3) -> str:
    """Tiny direct provider call (no agent loop, no tools) for UI utilities."""
    from saturday.llm.providers import build_client

    client = build_client(cfg)
    resp = client.chat([{"role": "user", "content": prompt}], max_tokens=max_tokens, temperature=temperature)
    msg = getattr(resp.message, "content", "")
    if isinstance(msg, list):
        msg = " ".join(p.get("text") or "" for p in msg if isinstance(p, dict))
    return str(msg or "").strip()


def _edit_result_diff(workspace_root: str, path: str) -> str | None:
    """What a just-completed write_file/edit_file actually changed, for the
    inline diff card in chat - best effort, never breaks the turn."""
    try:
        from saturday.editing import diff_after_edit
        from saturday.tools.journal import latest_before

        # must match tools.files._resolve()'s output exactly - that's the
        # path string the journal entry was written under
        base = Path(workspace_root).resolve()
        resolved = str((base / path).resolve()) if not Path(path).is_absolute() else str(Path(path).resolve())
        before = latest_before(workspace_root, resolved)
        return diff_after_edit(resolved, before)
    except Exception:
        return None


def _auto_title(app: "AppState", rt: SessionRuntime, user_text: str, final: str) -> None:
    """Rename a fresh session with a model-generated title (best effort)."""
    try:
        cur = (rt.store.read_meta(rt.sid) or {}).get("task")
        if cur and cur != _title_from_text(user_text):
            return  # user already renamed it (or a branch label) — never overwrite
        prompt = (
            "Invent a 3-6 word title for this conversation. Reply with the title only: "
            "no quotes, no trailing period.\n\nUser: "
            + user_text[:600]
            + "\nAssistant: "
            + (final or "")[:400]
        )
        title = _one_shot(rt.agent.cfg, prompt, max_tokens=24)
        title = title.strip().strip("\"'`").splitlines()[0][:60].strip() if title.strip() else ""
        if not title or not rt.store.set_task(rt.sid, title):
            return
        rt.bus.publish({"t": "title", "sid": rt.sid, "title": title})
    except Exception:
        pass  # best-effort: the truncated first message remains the fallback


def finish_and_announce(rt, event: dict) -> None:
    """End a run, then tell the client - in that order, always.

    session_runtime documents the invariant and notes the race shipped once:
    stream pumps exit on done/error only when the runtime is already idle, so
    publishing first leaves a viewer hanging until the next ping fails. Both
    the chat worker and the pipeline worker end runs, and the pipeline one had
    the order reversed. One helper so they cannot drift apart again.
    """
    rt.finish_run()
    rt.bus.publish(event)


def _delegate_turn(app: "AppState", rt: SessionRuntime, aid: str, prompt: str, emit_delta):
    """Run one turn inside an installed CLI agent instead of the model loop.

    Returns a Trajectory so every downstream step - finish_run, the done event,
    usage recording, the compact note - works unchanged. Tokens are zero on
    purpose: the delegate's spend is billed by its own vendor and Saturday has
    no honest number for it, and a fabricated one would flow into the cost
    surface as though it were measured.
    """
    from saturday.tools.external_agent import ExternalAgentTool
    from saturday.types import Trajectory, Usage

    bus = rt.bus
    bus.publish({"t": "notice", "s": f"delegated to {aid}"})
    tool = ExternalAgentTool()
    try:
        ok, out = tool.run({"agent": aid, "prompt": prompt, "timeout": 900})
    except Exception as exc:                       # a broken delegate is a turn, not a crash
        ok, out = False, f"{type(exc).__name__}: {exc}"
    text = (out or "").strip() or ("(no output)" if ok else "the delegate returned nothing")
    if not ok:
        text = f"{aid} failed: {text}"
    emit_delta(text)
    return Trajectory(
        task=prompt,
        system_prompt="",
        steps=[],
        final_answer=text,
        stop_reason="done" if ok else "error",
        usage=Usage(),
    )


def _run_chat(app: "AppState", rt: SessionRuntime, text: str, image_paths: list[str]) -> None:
    routed = ""          # set when this turn was auto-routed; used by the handler
    agent = rt.agent
    store = rt.store
    bus = rt.bus

    def emit_delta(d: str) -> None:
        _check_stop(rt)
        bus.publish({"t": "delta", "s": d})

    def emit_reason(d: str) -> None:
        _check_stop(rt)
        bus.publish({"t": "reason", "s": d})

    def emit_result(result) -> None:
        card, args = rt.take_pending_call(result.name)
        diff = None
        if result.ok and result.name in ("write_file", "edit_file") and args.get("path"):
            diff = _edit_result_diff(agent.cfg.workspace_root, str(args["path"]))
        bus.publish(
            {
                "t": "tool_result",
                "card": card,
                "name": result.name,
                "args": args,
                "ok": bool(result.ok),
                "output": result.output if result.ok else "",
                "error": None if result.ok else (result.error or result.output),
                "images": list(result.images or []),
                "diff": diff,
            }
        )

    def emit_step(n: int) -> None:
        _check_stop(rt)
        bus.publish({"t": "step", "n": n})

    try:
        # REPL parity (repl.py run loop): the compact note is prompt sugar —
        # injected ahead of the user text; the checkpoint remains the sole
        # continuity source and is never rewritten
        user_text = text
        note = getattr(rt, "history_note", None) or []
        if note:
            text = text + "\n\n(Conversation so far:\n" + "\n".join(note[-6:]) + ")"
        initial_history = None
        try:
            initial_history = store.load_checkpoint(rt.sid)
            agent.restore_checkpoint_meta(store.load_checkpoint_meta(rt.sid))
        except Exception:
            initial_history = None
        rt.pending_calls.clear()
        attachments = list(image_paths) + list(rt.pending_images)
        rt.pending_images.clear()
        delegate = ""
        routed = ""
        try:
            with app._cfg_lock:
                sel = app.session_models.get(rt.sid, "") or ""
                fallback = getattr(app, "default_agent", "") or ""
                # A selection made before this chat existed has no session to
                # live in. Adopt it here, BEFORE dispatching on it: resolving
                # it further down left the mode set but never acted on.
                if not sel and fallback:
                    sel = fallback if fallback == "auto" else "agent:" + fallback
                    app.session_models[rt.sid] = sel
            if sel == "auto":
                # The delegator proper: one ladder over installed CLI agents and
                # provider models, cheapest capable tier first, and what it
                # picks is recorded so the choice improves per kind of task.
                from saturday import routing

                choice = routing.route(task_kind="general")
                if choice:
                    kind, target = choice
                    routed = target
                    if kind == "agent":
                        delegate = target
                    else:
                        with app._cfg_lock:
                            app.session_models[rt.sid] = target
                        try:
                            agent = app.build_agent_for(rt)
                        finally:
                            with app._cfg_lock:
                                app.session_models[rt.sid] = "auto"  # stay on auto
                    bus.publish({"t": "notice", "s": f"auto \u2192 {kind}: {target}"})
                else:
                    bus.publish({"t": "notice",
                                 "s": "auto: nothing available to route to"})
            elif sel.startswith("agent:"):
                delegate = sel.split(":", 1)[1]
        except Exception:
            delegate = ""
        if delegate:
            traj = _delegate_turn(app, rt, delegate, user_text, emit_delta)
        else:
            traj = agent.run(
            text,
            attachments=attachments or None,
            on_text_delta=emit_delta,
            on_reasoning_delta=emit_reason,
            on_tool_result=emit_result,
            on_step_start=emit_step,
            initial_history=initial_history,
            session_id=rt.sid,
            )
        # STATE MACHINE INVARIANT: finish_run() (idle transition) happens BEFORE
        # the terminal event is published â€” the pump exits on done/error only
        # when the runtime is idle again, so publishing first would race and
        # can leave the client hanging on the stream.
        if routed:
            try:
                from saturday import routing

                ok_turn = traj.stop_reason == "done"
                routing.record(routed, "general", ok_turn)
                if not ok_turn and routing.looks_unusable(traj.final_answer or ""):
                    routing.mark_unusable(routed)
            except Exception:
                pass
        rt.finish_run()
        from saturday.provenance import apply_visible_footer

        # Cline/Goose-style cost surface: list-price estimate for THIS turn plus
        # the running session total; None (never a fake number) for unknown models
        cost = None
        cost_total = getattr(rt, "cost_usd", 0.0) or None
        try:
            from saturday.usage import estimate_cost_usd

            c = estimate_cost_usd(
                agent.cfg.provider, agent.cfg.model or "", traj.usage.prompt_tokens, traj.usage.completion_tokens
            )
            if c is not None:
                cost = round(c, 4)
                rt.cost_usd = float(getattr(rt, "cost_usd", 0.0) or 0.0) + c
                cost_total = round(rt.cost_usd, 4)
        except Exception:
            pass
        bus.publish(
            {
                "t": "done",
                "final": apply_visible_footer(
                    traj.final_answer or "", getattr(agent.cfg, "provenance_marking", "metadata")
                ),
                "stop_reason": traj.stop_reason,
                "steps": len(traj.steps),
                "tokens": traj.usage.total_tokens,
                "cost": cost,
                "cost_total": cost_total,
                "sid": rt.sid,
            }
        )
        try:
            from saturday.usage import record_usage

            record_usage(
                provider=agent.cfg.provider,
                model=agent.cfg.model or "?",
                session=rt.sid,
                steps=len(traj.steps),
                prompt_tokens=traj.usage.prompt_tokens,
                completion_tokens=traj.usage.completion_tokens,
                total_tokens=traj.usage.total_tokens,
                stop_reason=traj.stop_reason,
            )
        except Exception:
            pass
        # REPL parity: grow the ephemeral note after each completed turn so a
        # later /compact has per-turn summaries to fold (repl.py appends the
        # same shape). Plain attribute: SessionRuntime has no __slots__.
        try:
            rt.history_note = list(getattr(rt, "history_note", None) or [])
            rt.history_note.append(f"user: {user_text}")
            rt.history_note.append(f"agent: {(traj.final_answer or '')[:800]}")
        except AttributeError:
            pass
        # Zed/OpenHands/Goose parity: auto-name the session after its first
        # completed turn with one tiny background LLM call (never blocks the
        # reply; silently skipped on any failure or when disabled)
        if (
            traj.stop_reason == "done"
            and getattr(rt, "run_generation", 0) == 1
            and bool(getattr(agent.cfg, "auto_title_sessions", True))
            and user_text
        ):
            threading.Thread(target=_auto_title, args=(app, rt, user_text, traj.final_answer or ""), daemon=True, name="saturday-title").start()
    except RunStopped:
        rt.finish_run()
        bus.publish({"t": "done", "final": "", "stop_reason": "stopped", "steps": 0, "tokens": 0, "sid": rt.sid})
    except KeyboardInterrupt:
        rt.finish_run()
        bus.publish({"t": "done", "final": "", "stop_reason": "stopped", "steps": 0, "tokens": 0, "sid": rt.sid})
    except Exception as exc:
        # An auto-routed candidate that raised is the clearest usability signal
        # there is - better than any balance endpoint. Park it so the next turn
        # routes past it instead of repeating the same failure.
        if routed:
            try:
                from saturday import routing

                routing.record(routed, "general", False, note=str(exc)[:200])
                if routing.looks_unusable(f"{type(exc).__name__}: {exc}"):
                    routing.mark_unusable(routed)
                    bus.publish({"t": "notice",
                                 "s": f"{routed} is not usable right now - "
                                      "auto will route past it"})
            except Exception:
                pass
        rt.finish_run()
        bus.publish({"t": "error", "message": f"{type(exc).__name__}: {exc}", "sid": rt.sid})



# ---------------------------------------------------------------------------
# App state + HTTP server


class AppState:
    def __init__(
        self,
        cfg_overrides: dict | None = None,
        store_root: str | Path | None = None,
        projects_store=None,
    ) -> None:
        from saturday.config import AgentConfig
        from saturday.projects import ProjectStore
        from saturday.sessions import SessionStore

        self.cfg_overrides = cfg_overrides or {}
        self.tunnel = None  # live remote-access tunnel, when one is running
        self.memgraph_cache: dict[str, tuple[float, dict]] = {}
        self.memgraph_lock = threading.Lock()
        self._cfg_lock = threading.Lock()
        self.runtimes: dict[str, _SessionRuntime] = {}
        self.runtimes_lock = threading.Lock()
        # per-session model overrides (Cline/Amp parity): sid -> model id
        self.session_models: dict[str, str] = {}
        # delegate chosen before any session existed; the next chat inherits it
        self.default_agent: str = ""
        # last save_config failure, surfaced by the config routes (S11)
        self.config_persist_error: str = ""
        # keys apply_config has changed this session, so a config RELOAD (the
        # trust route rebuilds base_cfg from disk) can put them back instead
        # of silently reverting the user's in-session edits (S12)
        self.session_applied_keys: set[str] = set()
        self.store = SessionStore(root=store_root) if store_root else SessionStore()
        self.projects = projects_store if projects_store is not None else ProjectStore()
        self.base_cfg = AgentConfig.load(self.cfg_overrides)
        # Pending project-trust items: set by serve() when project .env,
        # .saturday/mcp.json, or executable .saturday/hooks.json exists but the
        # project has not yet been trusted non-interactively.
        # Cleared after the user makes a decision via POST /api/trust.
        self.pending_trust: list[dict] = []

    def make_agent(self):
        from saturday.agent.core import Agent

        with self._cfg_lock:
            cfg = self.base_cfg
        return Agent(cfg=cfg, session_store=self.store)

    # -- project context ---------------------------------------------------------
    def session_project(self, sid: str):
        """Project for a stored session, or None (unknown/deleted id included)."""
        meta = self.store.read_meta(sid) or {}
        pid = str(meta.get("project") or "")
        return self.projects.get(pid) if pid else None

    def _persona_for(self, cfg, proj) -> str:
        persona = getattr(cfg, "persona_extra", "") or ""
        parts = [persona.strip()]
        if proj is not None:
            instr = (proj.instructions or "").strip()
            if instr:
                parts.append(f"# Project: {proj.name}\n{instr}")
            knowledge = self._knowledge_block(proj)
            if knowledge:
                parts.append(knowledge)
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _knowledge_block(proj) -> str:
        """Reference-file contents injected for every chat in the project."""
        if not proj.files:
            return ""
        sections: list[str] = ["# Project reference files"]
        total = 0
        for fp in proj.files:
            try:
                txt = Path(fp).read_text(encoding="utf-8", errors="replace")
            except OSError:
                sections.append(f"--- {fp} (unreadable) ---")
                continue
            if total + len(txt) > KNOWLEDGE_TOTAL_CHARS:
                sections.append(f"--- {fp} omitted (project knowledge cap reached) ---")
                break
            txt = txt[:KNOWLEDGE_PER_FILE_CHARS]
            if len(txt) == KNOWLEDGE_PER_FILE_CHARS:
                txt += "\nâ€¦ [truncated]"
            total += len(txt)
            sections.append(f"--- {fp} ---\n{txt}")
        return "\n\n".join(sections)

    def _cfg_for_session(self, sid: str) -> tuple[object, str, str | None]:
        """(cfg, persona_extra, project_id) honoring the session's project."""
        with self._cfg_lock:
            cfg = self.base_cfg
            override = self.session_models.get(sid)
        # "agent:<id>" selects an external CLI agent, not a model. It must never
        # reach cfg.model: the LLM client would try to call a provider with a
        # model name that does not exist.
        if override and not override.startswith("agent:") and override != "auto":
            cfg = copy.copy(cfg)
            # the router names a model as "<provider>/<model>", since the
            # cheapest capable option is often not on the configured provider
            if "/" in override:
                head, rest = override.split("/", 1)
                from saturday.config import PROVIDERS as _P

                if head in _P:
                    cfg.provider, cfg.model = head, rest
                else:
                    cfg.model = override
            else:
                cfg.model = override
        proj = self.session_project(sid)
        if proj is None:
            return cfg, self._persona_for(cfg, None), None
        cfg = copy.copy(cfg)
        if proj.workspace:
            cfg.workspace_root = proj.workspace
        if proj.scopes:
            cfg.auth_scopes = dict(proj.scopes)
        return cfg, self._persona_for(cfg, proj), proj.id

    def session_workspace(self, sid: str) -> str | None:
        proj = self.session_project(sid)
        if proj is not None and proj.workspace:
            return proj.workspace
        return None

    def runtime_for(self, sid: str) -> _SessionRuntime:
        """Get or build this session's runtime, building OUTSIDE the lock.

        S9: constructing the Agent and its registry (plugins, MCP, every tool)
        used to happen while holding runtimes_lock - the same lock stop,
        approve and ask need - so a cold start blocked the user's ability to
        stop a run for as long as the build took. The build is now unlocked
        and the result is published under a double check, since another
        thread can create the same session's runtime meanwhile.
        """
        with self.runtimes_lock:
            rt = self.runtimes.get(sid)
            if rt is not None:
                rt.last_used = time.monotonic()
                return rt

        bus = _Bus()
        cfg, persona, pid = self._cfg_for_session(sid)
        agent = self._new_agent(cfg)
        agent.persona_extra = persona
        proj = self.projects.get(pid) if pid else None
        if proj is not None and proj.workspace:
            agent.memory_scope = proj.workspace
        agent._build_registry()
        built = _SessionRuntime(sid, agent, bus, project_id=pid)
        built.app = self
        _install_web_surface(built, agent)
        _install_attention_sink(built)

        with self.runtimes_lock:
            existing = self.runtimes.get(sid)
            if existing is not None:
                # someone else built it first; ours must not linger in the
                # process-wide attention sink list
                self.retire_runtime(built)
                existing.last_used = time.monotonic()
                return existing
            self._evict_idle_runtimes_locked()
            self.runtimes[sid] = built
            built.last_used = time.monotonic()
            return built

    # Long-lived desktop process: runtimes (agent + registry + bus) are
    # expensive and the map was unbounded. Beyond the cap, drop the
    # longest-idle NOT-BUSY runtimes — session state lives in the store and
    # is rehydrated on the next runtime_for(), so eviction is transparent.
    MAX_RUNTIMES = 48

    def _evict_idle_runtimes_locked(self) -> None:
        if len(self.runtimes) < self.MAX_RUNTIMES:
            return
        # A runtime with a live stream subscriber is NOT idle even when no run
        # is in flight: the tail holds that runtime's bus, so evicting it means
        # the next run publishes on the rebuilt runtime's new bus and the
        # viewer watches a dead one forever (S8).
        def watched(rt) -> bool:
            try:
                return bool(rt.bus.subs)
            except Exception:
                return False

        idle = sorted(
            (r for r in self.runtimes.values() if not r.busy and not watched(r)),
            key=lambda r: getattr(r, "last_used", 0.0),
        )
        for stale in idle[: len(self.runtimes) - self.MAX_RUNTIMES + 1]:
            self.runtimes.pop(stale.sid, None)
            self.retire_runtime(stale)

    @staticmethod
    def retire_runtime(rt) -> None:
        """Drop the process-wide references a runtime leaves behind.

        Dropping the dict entry was not enough: _install_attention_sink
        registers a closure over rt in attention._sinks, which nothing removed,
        so an evicted or deleted runtime stayed reachable through
        rt -> agent -> registry and rt -> bus buffer, and every retrieval event
        was still fanned out to it. MAX_RUNTIMES freed nothing on a long lived
        server, and the per-emit work grew with every session ever opened."""
        sink = getattr(rt, "_attn_sink", None)
        if sink is None:
            return
        from saturday import attention

        attention.remove_sink(sink)
        rt._attn_sink = None

    def _new_agent(self, cfg):
        from saturday.agent.core import Agent

        return Agent(cfg=cfg, persona_extra=getattr(cfg, "persona_extra", "") or "", session_store=self.store)

    def build_agent_for(self, rt: "_SessionRuntime") -> object:
        """Build this runtime's agent from its current selection, ignoring busy.

        _rebuild_runtime_agent starts with `if rt.busy: return`, which is right
        for a config change arriving from the UI mid-run. It is wrong for
        routing inside a turn, which happens *after* try_begin_run(): the
        rebuild returned silently, the old agent stayed in place, and auto
        published a route it had not taken while crediting the result to a
        model that never ran."""
        cfg, persona, pid = self._cfg_for_session(rt.sid)
        agent = self._new_agent(cfg)
        agent.persona_extra = persona
        proj = self.projects.get(pid) if pid else None
        if proj is not None and proj.workspace:
            agent.memory_scope = proj.workspace
        agent._build_registry()
        rt.project_id = pid
        rt.agent = agent
        rt._ctx_base = None
        _install_web_surface(rt, agent)
        return agent

    def _rebuild_runtime_agent(self, rt: _SessionRuntime) -> None:
        """Fresh agent for a runtime honoring its project (workspace/scopes/persona)."""
        if rt.busy:
            return
        self.build_agent_for(rt)

    def hello_fields(self) -> tuple[str, str]:
        """The (provider, model) the hello event announces.

        S10: every chat called the whole state_payload() for exactly these two
        strings, and that resolves an API key for every configured provider,
        stats every session to build the project list, parses usage.jsonl and
        loads the custom commands - all on the request thread, before the run
        even starts. This reads the two fields it needs.
        """
        with self._cfg_lock:
            return self.base_cfg.provider, self.base_cfg.model

    def state_payload(self) -> dict:
        from saturday import __version__
        from saturday.config import PROVIDERS
        from saturday.tools.base import ToolRegistry

        with self._cfg_lock:
            cfg = self.base_cfg
            provider = cfg.provider
            model = cfg.model
            safety_mode = cfg.safety_mode
            max_steps = cfg.max_steps
            temperature = cfg.temperature
            max_tokens = getattr(cfg, "max_tokens", 8192)
            fallback_models = list(getattr(cfg, "fallback_models", ()) or ())
            top_p = float(getattr(cfg, "top_p", 0.95) or 0.95)
            request_timeout = float(getattr(cfg, "request_timeout", 300.0) or 300.0)
            tool_timeout = float(getattr(cfg, "tool_timeout", 120.0) or 120.0)
            max_retries = int(getattr(cfg, "max_retries", 4) or 4)
            memory_max_chars = int(getattr(cfg, "memory_max_chars", 12_000) or 12_000)
            max_context_tokens = getattr(cfg, "max_context_tokens", None)
            compact_above_tokens = getattr(cfg, "compact_above_tokens", None)
            stream = bool(getattr(cfg, "stream", True))
            shell_allow_network = bool(getattr(cfg, "shell_allow_network", True))
            workspace_root = cfg.workspace_root
            bg_only = bool(cfg.desktop_background_only)
            persona_extra = getattr(cfg, "persona_extra", "") or ""
            persona_mode = getattr(cfg, "persona_mode", "agent") or "agent"
            guardrails = bool(getattr(cfg, "destructive_guardrails", True))
            sandboxed = bool(getattr(cfg, "sandboxed", False))
            max_run_tokens = int(getattr(cfg, "max_run_tokens", 0) or 0)
            plan_mode_global = bool(getattr(cfg, "plan_mode", False))
            assistant_name = getattr(cfg, "assistant_name", "") or ""
            assistant_user_title = getattr(cfg, "assistant_user_title", "") or ""
            provenance_marking = getattr(cfg, "provenance_marking", "metadata") or "metadata"
            verify_command = getattr(cfg, "verify_command", "") or ""
            mcp_names = sorted((cfg.mcp_servers or {}).keys())
        try:
            prof = PROVIDERS[provider]
            base_url = prof.resolve_base_url()
            has_key = bool(prof.resolve_api_key())
        except KeyError:
            base_url, has_key = "", False
        providers = [
            {"name": p.name, "default_model": p.resolve_default_model(), "has_key": bool(p.resolve_api_key())}
            for p in (PROVIDERS[k] for k in sorted(PROVIDERS))
        ]
        warnings: list[str] = []
        with self.runtimes_lock:
            agents = [rt.agent for rt in self.runtimes.values()]
        for ag in agents:
            warnings.extend(getattr(ag, "warnings", []) or [])
        projects = self.projects_payload()
        from saturday.config import CONFIG_DIR

        try:
            from saturday.usage import usage_summary

            usage = usage_summary()
        except Exception:
            usage = {"turns": 0, "total_tokens": 0, "days": [], "models": []}
        try:
            from saturday.usage import model_pricing

            pricing = model_pricing(provider, model)
        except Exception:
            pricing = None
        try:
            from saturday.webui_support import load_custom_commands

            custom_commands = load_custom_commands()
        except Exception:
            custom_commands = {}

        return {
            "version": __version__,
            "trust_pending": bool(self.pending_trust),
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "has_key": has_key,
            "providers": providers,
            "safety_mode": safety_mode,
            "max_steps": max_steps,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "request_timeout": request_timeout,
            "tool_timeout": tool_timeout,
            "max_retries": max_retries,
            "memory_max_chars": memory_max_chars,
            "max_context_tokens": max_context_tokens,
            "compact_above_tokens": compact_above_tokens,
            "stream": stream,
            "shell_allow_network": shell_allow_network,
            "fallback_models": fallback_models,
            "disabled_tools": sorted(ToolRegistry.expand_tool_names(getattr(cfg, "disabled_tools", []) or [])),
            "workspace_root": workspace_root,
            "background_only": bg_only,
            "persona_extra": persona_extra,
            "persona_mode": persona_mode,
            "guardrails": guardrails,
            "sandboxed": sandboxed,
            "max_run_tokens": max_run_tokens,
            "plan_mode": plan_mode_global,
            "approvals_allow": self.approval_rules(),
            "hooks": self.hooks_state(),
            "assistant_name": assistant_name,
            "assistant_user_title": assistant_user_title,
            "provenance_marking": provenance_marking,
            "verify_command": verify_command,
            "auth_scopes": dict(getattr(cfg, "auth_scopes", {}) or {}),
            "mcp_servers": mcp_names,
            "slash_commands": [list(c) for c in SLASH_COMMAND_LIST],
            "tool_names": sorted(self._registry_names()),
            "keep_reasoning_in_history": bool(getattr(cfg, "keep_reasoning_in_history", False)),
            "auto_title_sessions": bool(getattr(cfg, "auto_title_sessions", True)),
            "suggest_followups": bool(getattr(cfg, "suggest_followups", True)),
            "lsp_servers": dict(getattr(cfg, "lsp_servers", {}) or {}),
            "max_wall_seconds": int(getattr(cfg, "max_wall_seconds", 0) or 0),
            "max_run_cost_usd": float(getattr(cfg, "max_run_cost_usd", 0.0) or 0.0),
            "injection_guard": bool(getattr(cfg, "injection_guard", True)),
            "persist_approvals": bool(getattr(cfg, "persist_approvals", True)),
            "blocked_apps": list(getattr(cfg, "blocked_apps", []) or []),
            "blocked_providers": list(getattr(cfg, "blocked_providers", []) or []),
            "blocked_models": list(getattr(cfg, "blocked_models", []) or []),
            "memory_nudge_interval": int(getattr(cfg, "memory_nudge_interval", 0) or 0),
            "warnings": warnings,
            "projects": projects,
            "config_dir": str(CONFIG_DIR),
            "sessions_dir": str(self.store.root),
            "usage": usage,
            "pricing": list(pricing) if pricing else None,
            "custom_commands": custom_commands,
            "schedules_watcher": SCHEDULE_WATCHER_ON,
            "session_models": dict(self.session_models),
        }

    def approval_rules(self) -> list[str]:
        try:
            from saturday.approvals_store import load_rules

            return list(load_rules().get("allow") or [])
        except Exception:
            return []

    def hooks_state(self) -> dict:
        """Merged lifecycle hooks (global + project) for the Settings editor."""
        try:
            from saturday.user_hooks import load_hooks

            return load_hooks(getattr(self.base_cfg, "workspace_root", None))
        except Exception:
            return {"pre_tool_call": [], "post_tool_call": []}

    def projects_payload(self) -> list[dict]:
        counts: dict[str, int] = {}
        for row in self.store.list_sessions(limit=None):
            pid = row.get("project") or ""
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        out = []
        for p in self.projects.list():
            d = p.to_dict()
            d["sessions"] = counts.get(p.id, 0)
            out.append(d)
        return out

    def _registry_names(self) -> set[str]:
        """Concrete tool names the toggle UI can offer. Cached on SUCCESS only
        (a transient build failure must not permanently blank the settings
        checklist); invalidated when the MCP server set changes so tools from
        newly added servers become toggleable without a restart."""
        mcp_key = tuple(sorted((getattr(self.base_cfg, "mcp_servers", None) or {}).keys()))
        cached = getattr(self, "_reg_names_cache", None)
        if cached is not None and getattr(self, "_reg_names_mcp_key", None) == mcp_key:
            return cached
        try:
            names = set(self.make_agent()._build_registry().names())
            self._reg_names_cache = names
            self._reg_names_mcp_key = mcp_key
            return names
        except Exception:
            return getattr(self, "_reg_names_cache", None) or set()

    def workspace_path(self) -> Path:
        """The directory this app is actually working in.

        Several routes used Path(".") - the process's launch directory - which
        is only the workspace by coincidence. `saturday --workspace
        /projects/foo` started from a home directory recorded its project
        trust decision against the home directory instead.
        """
        root = getattr(self.base_cfg, "workspace_root", "") or "."
        try:
            return Path(root).expanduser().resolve()
        except OSError:
            return Path(root)

    def apply_config(self, patch: dict) -> list[str]:
        from saturday.config import PROVIDERS, save_config
        from saturday.tools.base import ToolRegistry

        # tool-name universe for validation, computed OUTSIDE the cfg lock
        # (building an agent under the lock deadlocks make_agent()). Cached on
        # SUCCESS only (a transient failure must not permanently reject every
        # concrete tool name) and invalidated when the MCP server set changes,
        # so tools from newly added servers become toggleable without a
        # restart.
        known_families = set(ToolRegistry.TOOL_FAMILIES)
        reg_names = self._registry_names()

        # Validate hooks BEFORE anything is written. They used to be validated
        # after the rest of the patch had already been persisted, so a bad
        # hooks value answered 400 while every other setting in the same save
        # had quietly gone to disk - the user saw a rejection and got a
        # partial apply.
        hooks_in = patch.get("hooks") if "hooks" in patch else None
        if hooks_in is not None:
            self._validate_hooks(hooks_in)

        applied: list[str] = []
        with self._cfg_lock:
            cfg = self.base_cfg
            st = _CfgApplyState(cfg)
            st.known_families = known_families
            st.reg_names = reg_names
            for key, validate in _CONFIG_FIELDS:
                value = validate(patch, st, key)
                if value is _CFG_SKIP:
                    continue
                setattr(cfg, key, value)
                applied.append(key)
                if st.extra_applied:
                    applied.extend(st.extra_applied)
                    st.extra_applied.clear()
            if cfg.model is None:
                # historical position was mid-table; nothing between there and
                # here observes cfg.model, so resolving after the loop yields
                # identical state before persistence/reload either way
                cfg.model = PROVIDERS[cfg.provider].resolve_default_model()
            persisted = {k: getattr(cfg, k) for k in applied}
            self.session_applied_keys.update(applied)
        self.config_persist_error = ""
        if persisted:
            try:
                save_config(persisted)
            except OSError as exc:
                # The change IS live for this session, so this is not a
                # failure to apply - but it was swallowed entirely, and the UI
                # reported "applied" for a setting that would be gone on the
                # next restart. Record it so the route can say so.
                self.config_persist_error = (
                    f"applied for this session but could not be saved: {exc}"
                )
        self._reload_runtime_state(applied)
        if hooks_in is not None:
            self._write_hooks(hooks_in)
        return applied

    def _reload_runtime_state(self, applied: list[str]) -> None:
        # live runtimes share the cfg object; rebuild approval policy so a safety
        # mode change takes effect on the next tool call without a restart.
        # Project runtimes hold a per-session cfg clone: sync the shared fields
        # onto it (workspace stays project-owned) and re-merge persona text.
        # DERIVED, not hand-maintained: every settings key propagates except
        # the genuinely project-owned ones, so a new _CONFIG_FIELDS entry can
        # never silently stop reaching project sessions (design review).
        from saturday.safety import ApprovalPolicy

        with self._cfg_lock:
            base = self.base_cfg
        with self.runtimes_lock:
            rts = list(self.runtimes.values())
        from saturday.approvals_store import load_rules as _load_approval_rules

        _rules = _load_approval_rules()
        fresh_allow = _rules.get("allow") or []
        fresh_deny = _rules.get("deny") or []
        for rt in rts:
            rcfg = rt.agent.cfg
            if rcfg is not base:
                for f in _SHARED_CONFIG_FIELDS:
                    setattr(rcfg, f, getattr(base, f, None))
                proj = self.projects.get(rt.project_id) if rt.project_id else None
                rt.agent.persona_extra = self._persona_for(base, proj)
                rcfg.auth_scopes = dict(proj.scopes) if proj is not None and proj.scopes else base.auth_scopes
            else:
                rt.agent.persona_extra = getattr(base, "persona_extra", "") or ""
            # re-attach the freshly persisted allow-rules so live sessions
            # honor approvals saved after this agent was constructed
            # blocked_apps has to come along. Rebuilding the policy without
            # it dropped the list from every live agent on ANY config save -
            # changing the temperature erased it - while cfg still said those
            # apps were blocked. Its own docstring calls it a set "an explicit
            # user set can never be erased by an agent"; a settings save was
            # erasing it. (S14: found by tracing which of these mutations
            # actually reach a running turn.)
            rt.agent.approval_policy = ApprovalPolicy.from_mode(
                getattr(rt.agent.cfg, "safety_mode", "ask"),
                allow_rules=fresh_allow,
                blocked_apps=list(getattr(rt.agent.cfg, "blocked_apps", []) or []),
            )
            # deny-rules reach live agents the same way allow-rules do; the
            # guard degrades safely if ApprovalPolicy lacks deny_rules yet
            # (safety.py integration ordering)
            if getattr(rt.agent.approval_policy, "deny_rules", None) is not None:
                rt.agent.approval_policy.deny_rules = list(fresh_deny)
            # live mode switch: gates captured auto_approve at construction
            from saturday.safety import is_autonomous

            rt.file_gate.auto_approve = is_autonomous(getattr(rt.agent.cfg, "safety_mode", "ask"))
            _install_web_surface(rt, rt.agent)
        if set(applied) & _REBUILD_CONFIG_FIELDS:
            # fields captured INTO tool instances at agent construction
            # (auth_scopes registry wiring, verify_command on the file tools,
            # lsp_servers command lists, memory_max_chars on working memory)
            # cannot be patched in place: rebuild agents so they take effect.
            # Busy runtimes are skipped and pick the change up on their next
            # natural rebuild.
            for rt in rts:
                self._rebuild_runtime_agent(rt)
        if applied:
            # persona/provider edits change the per-run system overhead the
            # live ctx meter caches; force a re-baseline
            for rt in rts:
                rt._ctx_base = None

    @staticmethod
    def _validate_hooks(hooks_in) -> dict[str, list[str]]:
        """Check a hooks patch and return it cleaned. Raises ValueError.

        Split out of _write_hooks so apply_config can reject a bad value
        BEFORE it persists the rest of the patch: validating afterwards meant
        a rejected save had already written every other setting in it.
        """
        valid = {"pre_tool_call", "post_tool_call"}
        if not isinstance(hooks_in, dict) or set(hooks_in.keys()) - valid:
            raise ValueError(f"hooks must be an object with keys: {', '.join(sorted(valid))}")
        cleaned: dict[str, list[str]] = {}
        for k in valid:
            v = hooks_in.get(k)
            if v is None:
                continue
            if not isinstance(v, list) or not all(isinstance(c, str) for c in v):
                raise ValueError(f"{k} must be a list of command strings")
            cmds = [c.strip() for c in v if c.strip()]
            if any(len(c) > 500 or "\n" in c for c in cmds):
                raise ValueError(f"{k} commands must be single lines of at most 500 chars")
            cleaned[k] = cmds
        return cleaned

    def _write_hooks(self, hooks_in) -> None:
        """Persist global lifecycle hooks (Settings > Hooks)."""
        cleaned = self._validate_hooks(hooks_in)
        from saturday.config import get_config_dir

        path = get_config_dir() / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
        merged = {**existing, **cleaned}
        path.write_text(json.dumps(merged, indent=2), encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: AppState = None  # injected
    token: str = ""  # injected; empty disables auth
    allowed_hosts: set[str] = set()  # injected Host-header allowlist (rebinding)
    allowed_origins: set[str] = set()  # injected Origin allowlist (CSRF)

    def handle(self) -> None:
        # keep-alive sockets routinely die mid-request when the window closes;
        # swallow the abort instead of spamming stderr via socketserver
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass
        except OSError as exc:
            if getattr(exc, "winerror", None) != 10053:
                raise

    # -- infra -----------------------------------------------------------------
    _stream_open = False  # set by _begin_stream; see _fail_500

    def log_message(self, fmt, *a):  # silence console (cp1252 landmine)
        pass

    def _token_ok(self) -> bool:
        if not self.token:
            return True
        # NOTE: the URL query is deliberately NOT an auth channel — a token in
        # ?k= leaks into browser history, window titles and Referer headers.
        # First-load ?k= links are exchanged for a cookie by _cookie_bootstrap
        # in do_GET; every other path must present the header or cookie.
        # constant-time compares: header/cookie bytes are attacker-controlled;
        # encode() sidesteps compare_digest's ASCII-only str restriction
        supplied = (self.headers.get("X-Saturday-Token") or "").encode("utf-8")
        if hmac.compare_digest(supplied, self.token.encode("utf-8")):
            return True
        cookie = self.headers.get("Cookie") or ""
        expected = f"df_token={self.token}".encode("utf-8")
        # exact segment match: a lookalike cookie (xdf_token=...) must not pass
        return any(
            hmac.compare_digest(part.strip().encode("utf-8"), expected)
            for part in cookie.split(";")
        )

    def _cookie_bootstrap(self) -> bool:
        """One-shot ?k=<token> exchange: Set-Cookie + location.replace('/') so
        the token never persists in history/Referer past the first hop. Only
        the exact GET /?k= form is honored; POSTs never authenticate via URL."""
        if not self.token:
            return False
        params = self._query_params()
        supplied = (params.get("k") or [""])[0]
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), self.token.encode("utf-8")):
            return False
        # The Set-Cookie below is what authenticates; this page only strips the
        # token out of the URL. It used to write the cookie from JavaScript
        # too, which is why the cookie could not be HttpOnly - and a token
        # readable from script is one XSS away from being stolen, on a
        # surface whose whole job is running commands. Nothing reads the
        # cookie from JavaScript; API calls carry the header instead.
        page = (
            "<!doctype html><meta charset=utf-8><title>Saturday</title>"
            "<script>location.replace('/');</script>"
            "<p style='font-family:monospace'>opening Saturday…</p>"
        ).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Set-Cookie",
                f"df_token={self.token}; Path=/; SameSite=Strict; HttpOnly",
            )
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        return True

    def _host_ok(self) -> bool:
        """Pin the Host header to the bound loopback address (DNS rebinding)."""
        allowed = type(self).allowed_hosts
        if not allowed:
            return True
        from saturday.utils.httpd import authority_allowed

        return authority_allowed(self.headers.get("Host") or "", allowed)

    def _origin_ok(self) -> bool:
        """Reject cross-origin mutating requests (drive-by CSRF from web pages)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        allowed = type(self).allowed_origins
        if not allowed:
            return True
        from saturday.utils.httpd import authority_allowed

        return authority_allowed(origin, allowed)

    def _route(self) -> str:
        return (self.path or "").split("?", 1)[0]

    def _query_params(self) -> dict[str, list[str]]:
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(self.path or "").query)

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                # say so, rather than only closing: a client that reuses a
                # socket the server has just dropped sees the next request
                # fail for no visible reason (S16)
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass  # client navigated away mid-response; nothing sensible to do

    def _fail_500(self, exc: BaseException) -> None:
        """Last resort for an exception a route did not expect.

        BaseHTTPRequestHandler answers an escaped exception by closing the
        socket with nothing on it, so the browser saw a network error with no
        status and no body and the fetch layer could not tell "server bug"
        from "server gone". Every do_* dispatch funnels through here instead.
        The traceback goes to stderr only under SATURDAY_DEBUG, matching how
        the rest of the server treats internal detail."""
        if os.environ.get("SATURDAY_DEBUG"):
            import traceback

            traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        if self._stream_open:
            # headers are already out; say it in-band so the client stops waiting
            self._stream_line({"t": "notice", "s": f"[server error] {type(exc).__name__}: {exc}"})
            self._stream_line({"t": "done", "final": "", "stop_reason": "error", "steps": 0, "tokens": 0})
            return
        self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _send_asset(self, name: str, status: int = 200) -> None:
        p = ASSETS_DIR / name
        if not p.is_file():
            self._send_json({"error": "asset missing"}, 404)
            return
        body = p.read_bytes()
        self.send_response(status)
        self.send_header("Content-Type", MIME.get(p.suffix, "application/octet-stream"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _begin_stream(self) -> None:
        # once this runs the status line is spent: a later failure can no
        # longer be reported as an HTTP error, only as a stream event
        self._stream_open = True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _stream_line(self, obj: dict) -> bool:
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            return False

    def _read_json(self) -> dict | None:
        # garbage Content-Length (attacker-controlled header) must 400, not
        # raise out of do_POST and kill the connection with a traceback
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            # S16: we cannot know how many bytes to skip, so this connection
            # can no longer be trusted to start at a request boundary. Left
            # keep-alive, the unread body was parsed as the NEXT request line.
            self.close_connection = True
            return None
        if n < 0 or n > MAX_BODY:
            # draining an oversize body is its own denial of service, so the
            # connection ends instead of being resynchronised
            self.close_connection = True
            return None
        if n == 0:
            return None  # no body to leave behind; the connection is fine
        try:
            body = self.rfile.read(n)
        except OSError:
            self.close_connection = True
            return None
        if len(body) < n:
            self.close_connection = True  # short read: the rest is still coming
            return None
        try:
            return json.loads(body or b"{}")
        except json.JSONDecodeError:
            return None  # body fully consumed; the connection is still in sync

    def _guard(self, *, check_origin: bool = False) -> bool:
        if not self._token_ok():
            self._send_json({"error": "unauthorized"}, 401)
            return False
        if not self._host_ok():
            self._send_json({"error": "rejected: Host header not allowed"}, 403)
            return False
        if check_origin and not self._origin_ok():
            self._send_json({"error": "rejected: cross-origin requests are not allowed"}, 403)
            return False
        return True

    # -- GET -------------------------------------------------------------------
    def do_GET(self):
        route = self._route()
        if route in ("/", "/index.html"):
            if "k=" in (self.path or "") and self._cookie_bootstrap():
                return
            if not self._guard():
                return
            self._send_asset("index.html")
            return
        if route in ("/app.css", "/app.js", "/favicon.svg"):
            self._send_asset(route.lstrip("/"))
            return
        if route == "/favicon.ico":
            # legacy browsers probe /favicon.ico directly; serve the SVG icon
            # rather than a JSON 404 (index.html pins the SVG for modern ones)
            self._send_asset("favicon.svg")
            return
        if not self._guard():
            return
        self._walk_routes(_GET_ROUTES, route)

    def _walk_routes(self, table, route: str) -> None:
        """Literal entries take no args; regex entries receive their capture groups."""
        for pat, fname in table:
            if isinstance(pat, str):
                if pat == route:
                    self._invoke(fname)
                    return
                continue
            m = pat.fullmatch(route)
            if m:
                self._invoke(fname, *m.groups())
                return
        self._send_json({"error": "not found"}, 404)

    def _invoke(self, fname: str, *args) -> None:
        """Call a route handler so that nothing escapes without a response."""
        try:
            getattr(self, fname)(*args)
        except Exception as exc:
            self._fail_500(exc)

    def _get_state(self) -> None:
        app = self.app
        self._send_json(app.state_payload())

    def _get_trust(self) -> None:
        """Return pending project-trust items for the browser trust modal."""
        app = self.app
        self._send_json({"pending": list(app.pending_trust), "workspace": str(app.workspace_path())})

    def _post_trust(self, payload: dict) -> None:
        """Record the user's trust/deny decision and reload config if trusted."""
        app = self.app
        decision = str(payload.get("decision") or "")
        if decision not in ("trust", "deny"):
            self._send_json({"error": "decision must be 'trust' or 'deny'"}, 400)
            return

        from saturday.utils.trust import record_decision

        # the workspace being worked in, not wherever the process was started:
        # `saturday --workspace /projects/foo` launched from a home directory
        # recorded the decision against the home directory, so the project
        # stayed untrusted and the home directory silently became trusted
        root = app.workspace_path()
        trusted = decision == "trust"
        record_decision(root, trusted=trusted)

        applied: list[str] = []
        if trusted:
            # Load the project .env now that trust is recorded and re-init config.
            from saturday.utils.env import reload_trusted_env
            from saturday.config import AgentConfig

            reload_trusted_env(root)
            with app._cfg_lock:
                new_cfg = AgentConfig.load(app.cfg_overrides)
                # Carry forward in-session apply_config changes. The comment
                # here promised this and the code did not do it: base_cfg was
                # simply replaced, so trusting a project threw away every
                # setting the user had changed in the UI since launch.
                for key in sorted(app.session_applied_keys):
                    if hasattr(new_cfg, key):
                        setattr(new_cfg, key, getattr(app.base_cfg, key))
                app.base_cfg = new_cfg
            applied = sorted({"provider", "model"} | app.session_applied_keys)
            app._reload_runtime_state(applied)

        app.pending_trust = []
        self._send_json({"ok": True, "trusted": trusted, "applied": applied})

    def _get_metrics(self) -> None:
        from saturday.usage import usage_summary

        try:
            days = min(90, max(1, int((self._query_params().get("days") or ["14"])[0])))
        except ValueError:
            days = 14
        self._send_json({"window_days": days, **usage_summary(limit_days=days)})

    def _post_remote(self, payload: dict) -> None:
        """Start or stop a tunnel so a phone can reach this instance."""
        from saturday import remote as rmt

        app = self.app
        if not payload.get("start"):
            tun = getattr(app, "tunnel", None)
            if tun is not None:
                tun.close()
                app.tunnel = None
            self._send_json({"ok": True, "running": False})
            return

        if getattr(app, "tunnel", None) is not None:
            t = app.tunnel
            self._send_json({"ok": True, "running": True, "url": t.url, "provider": t.provider})
            return

        provider = str(payload.get("provider") or "") or next(iter(rmt.available_providers()), "")
        if not provider:
            self._send_json({
                "ok": False,
                "error": "no tunnel provider installed",
                "hints": rmt.INSTALL_HINTS,
            }, status=400)
            return
        port = self.server.server_address[1]
        try:
            tun = rmt.start_tunnel(provider, port)
        except RuntimeError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.server.allow_host(tun.host)
        app.tunnel = tun
        url = tun.url + "/" + (f"?k={self.server.token}" if self.server.token else "")
        self._send_json({
            "ok": True, "running": True, "url": url, "provider": tun.provider,
            "warning": "TLS terminates at the provider" if provider == "cloudflared" else "",
        })

    def _get_remote(self) -> None:
        from saturday import remote as rmt

        tun = getattr(self.app, "tunnel", None)
        self._send_json({
            "running": tun is not None,
            "url": (tun.url + "/" + (f"?k={self.server.token}" if self.server.token else "")) if tun else "",
            "provider": tun.provider if tun else "",
            "available": rmt.available_providers(),
            "hints": rmt.INSTALL_HINTS,
        })

    def _get_agents(self) -> None:
        from saturday import routing

        kind = (self._query_params().get("task_kind") or ["general"])[0]
        rows = [
            {
                "agent": c.agent, "tier": c.tier, "tier_name": routing.TIER_NAMES[c.tier],
                "installed": c.installed, "enabled": c.enabled, "custom": c.custom,
                "success": c.ema_success if c.n else None, "runs": c.n,
                "caution": _agent_caution(c.agent),
            }
            for c in routing.candidates(kind)
        ]
        self._send_json({"agents": rows})

    def _post_agents(self, payload: dict) -> None:
        from saturday import routing

        action = str(payload.get("action") or "")
        if action == "add":
            from saturday.tools.external_agent import save_custom_agent

            name = str(payload.get("name") or "")
            binaries = payload.get("binaries") or []
            if isinstance(binaries, str):
                binaries = [b.strip() for b in binaries.split(",")]
            args = payload.get("args") or None
            try:
                save_custom_agent(
                    name, [str(b) for b in binaries], args, str(payload.get("install_hint") or "")
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._send_json({"ok": True})
            return
        if action == "remove":
            from saturday.tools.external_agent import remove_custom_agent

            name = str(payload.get("name") or "")
            removed = remove_custom_agent(name)
            routing.set_enabled(name, False)
            self._send_json({"ok": True, "removed": removed})
            return
        name = str(payload.get("agent") or "")
        if not name:
            self._send_json({"ok": False, "error": "agent required"}, status=400)
            return
        routing.set_enabled(name, bool(payload.get("enabled")))
        self._send_json({"ok": True, "enabled": sorted(routing.enabled_agents())})

    def _get_models(self) -> None:
        """Everything that can take a turn: provider models and CLI agents.

        The `providers` shape is unchanged for the settings model browser; the
        composer's model menu reads the same response, so the two cannot show
        different worlds. `unreachable` is reported rather than dropped: "no
        models" and "your key is wrong" are different problems and the menu
        should be able to say which."""
        from saturday import catalog
        from saturday.cli import _is_free_model

        q = self._query_params()
        only_free = (q.get("free") or ["0"])[0] not in ("0", "", "false")
        one = (q.get("provider") or [""])[0] or None
        refresh = (q.get("refresh") or ["0"])[0] not in ("0", "", "false")

        entries = catalog.providers(timeout=8.0, only=one, refresh=refresh)
        out, unreachable, notes = {}, [], []
        for p in entries:
            if not p.reachable:
                if p.configured:
                    unreachable.append({"provider": p.name, "detail": p.detail})
                continue
            # A listing is not an entitlement. When the key demonstrably cannot
            # run paid models, offering them is a promise the send will break,
            # so only the free tier is listed and the reason is said out loud.
            # an unusable key runs nothing here, so offer nothing and say why
            if not p.usable:
                notes.append({"provider": p.name, "note": p.note, "hidden": len(p.models)})
                continue
            free_only_here = only_free
            picked = [
                {"id": m, "free": _is_free_model(p.name, m)}
                for m in sorted(p.models)
                if not free_only_here or _is_free_model(p.name, m)
            ]

            if picked:
                out[p.name] = picked
        agents = [
            {"id": a.id, "available": a.available, "install_hint": a.install_hint,
             "caution": a.caution, "provider": a.provider, "model": a.model}
            for a in catalog.agents()
        ]
        self._send_json({"providers": out, "agents": agents,
                         "unreachable": unreachable, "notes": notes})

    def _post_models(self, payload: dict) -> None:
        """Wire chosen models into agents.json so auto-delegation can reach them."""
        from saturday.cli import _add_free_to_agents

        picks = payload.get("models") or {}
        if not isinstance(picks, dict):
            self._send_json({"ok": False, "error": "models must be {provider: [id,...]}"}, status=400)
            return
        added = _add_free_to_agents({str(k): [str(m) for m in v] for k, v in picks.items()})
        self._send_json({"ok": True, "added": added})

    def _get_sessions(self) -> None:
        app = self.app
        rows = app.store.list_sessions()
        with app.runtimes_lock:
            busy = {sid for sid, rt in app.runtimes.items() if rt.busy}
        for r in rows:
            r["busy"] = r["id"] in busy
        self._send_json({"sessions": rows})

    def _get_projects(self) -> None:
        app = self.app
        self._send_json({"projects": app.projects_payload()})

    def _get_export_all(self) -> None:
        app = self.app
        sessions = []
        for row in app.store.list_sessions(limit=None):
            data = app.store.load(row["id"])
            if data:
                sessions.append(data)
        self._send_json({"exported": len(sessions), "sessions": sessions})

    def _get_session(self, sid: str) -> None:
        app = self.app
        data = hydrate_session(app.store, sid)
        if data is None:
            self._send_json({"error": "not found"}, 404)
        else:
            self._send_json(data)

    def _get_search(self) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        q = unquote((qs.get("q") or [""])[0])
        try:
            lim = min(50, max(1, int((qs.get("limit") or ["20"])[0])))
        except ValueError:
            lim = 20
        self._send_json({"query": q, "results": search_sessions(self.app.store, q, lim)})

    def _get_context(self) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        sid = unquote((qs.get("sid") or [""])[0])
        # only attach to a REAL stored session; unknown sids must not mint
        # cached runtimes (agents/registries are expensive and permanent)
        if sid and not self.app.store._path(sid).is_file():
            self._send_json({"error": "unknown session"}, 404)
            return
        agent = self.app.runtime_for(sid).agent if sid else self.app.make_agent()
        history: list[dict] = []
        if sid:
            try:
                history = self.app.store.load_checkpoint(sid) or []
            except Exception:
                history = []
        try:
            bd = agent.context_breakdown(history)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        bd["sid"] = sid
        self._send_json(bd)

    def _send_image_file(self) -> None:
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        p = (qs.get("p") or [""])[0]
        path = Path(p)
        try:
            rp = path.resolve()
        except OSError:
            self._send_json({"error": "bad path"}, 400)
            return
        if rp.suffix.lower() not in IMAGE_EXTS or not rp.is_file():
            self._send_json({"error": "not an image"}, 404)
            return
        roots = []
        sid = (qs.get("sid") or [""])[0]
        for cand in (
            self.app.session_workspace(sid) if sid else None,
            self.app.base_cfg.workspace_root,
            str(Path(tempfile.gettempdir()) / "saturday-uploads"),
        ):
            if not cand:
                continue  # projectless session: fall through to the global roots
            try:
                roots.append(Path(cand).resolve())
            except OSError:
                continue
        if not any(rp == root or root in rp.parents for root in roots):
            self._send_json({"error": "outside sandbox"}, 403)
            return
        body = rp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(rp.suffix.lower(), "application/octet-stream"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ws_root(self, sid: str = ""):
        try:
            proj_ws = self.app.session_workspace(sid) if sid else None
            return Path(proj_ws or self.app.base_cfg.workspace_root).resolve()
        except OSError:
            return None

    def _ws_safe(self, rel: str, sid: str = ""):
        root = self._ws_root(sid)
        if root is None:
            return None
        p = Path(rel or ".")
        if not p.is_absolute():
            p = root / p
        try:
            rp = p.resolve()
        except OSError:
            return None
        if rp != root and root not in rp.parents:
            return None
        return rp

    def _ws_list(self) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        rel = unquote((qs.get("path") or [""])[0])
        sid = unquote((qs.get("sid") or [""])[0])
        rp = self._ws_safe(rel, sid)
        if rp is None or not rp.is_dir():
            self._send_json({"error": "not a directory"}, 404)
            return
        root = self._ws_root(sid)
        entries = []
        try:
            for child in sorted(rp.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
                if child.name.startswith("."):
                    continue
                # abs path lets the UI preview images via /api/file (which
                # enforces the same sandbox); mtime powers the modified column
                if child.is_dir():
                    entries.append({"name": child.name, "dir": True, "size": 0, "mtime": 0, "path": str(child)})
                else:
                    try:
                        st = child.stat()
                        sz, mt = st.st_size, int(st.st_mtime)
                    except OSError:
                        sz, mt = 0, 0
                    entries.append({"name": child.name, "dir": False, "size": sz, "mtime": mt, "path": str(child)})
        except OSError as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        try:
            shown = str(rp.relative_to(root)).replace("\\", "/")
        except ValueError:
            shown = ""
        self._send_json({"path": shown, "entries": entries[:500]})

    def _ws_read(self) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        rel = unquote((qs.get("path") or [""])[0])
        sid = unquote((qs.get("sid") or [""])[0])
        rp = self._ws_safe(rel, sid)
        if rp is None or not rp.is_file():
            self._send_json({"error": "not a file"}, 404)
            return
        size = rp.stat().st_size
        cap = 300_000
        try:
            raw = rp.read_bytes()[:cap]
        except OSError as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._send_json(
            {
                "path": rel.replace("\\", "/"),
                "size": size,
                "truncated": size > cap,
                "content": raw.decode("utf-8", errors="replace"),
            }
        )

    def _pump_bus(self, rt: _SessionRuntime, q: Queue, replay: list[dict] | None = None, first_event: dict | None = None) -> None:
        """Stream bus events to the client until the turn finishes.

        replay is the (already-fetched) list of buffered events to send before
        switching to the live queue — fetched via bus.subscribe_with_replay()
        so the snapshot and the queue subscription are atomic; a separate
        subscribe() + bus.replay() pair racing a fast publish() would double
        deliver one event (once from the replay snapshot, once from the live
        queue). first_event is written right after the response headers
        (per-response hello).
        """
        self._begin_stream()
        if first_event is not None and not self._stream_line(first_event):
            return
        if replay:
            for evt in replay:
                if not self._stream_line(evt):
                    return
        while True:
            try:
                evt = q.get(timeout=15)
            except Empty:
                if not self._stream_line({"t": "ping"}):
                    return
                continue
            if not self._stream_line(evt):
                return
            if evt.get("t") in ("done", "error"):
                # the worker guarantees idle-before-terminal-publish, so a
                # terminal event with the runtime still busy means another run
                # already started (queue-drain case): keep streaming
                if rt.is_idle:
                    return

    def _stream_tail(self, sid: str) -> None:
        """Live event tail. ``?from=run`` replays the in-flight turn from its
        first event (used when a user re-opens a session that is still
        running); without it the stream is live-only from now."""
        from urllib.parse import parse_qs, urlparse

        # S8: any id matching the route charset used to mint a runtime here -
        # an agent and a full registry, cached and permanent - for a session
        # that does not exist. /api/context already refuses exactly this.
        if not self.app.store._path(sid).is_file():
            self._send_json({"error": "unknown session"}, 404)
            return
        rt = self.app.runtime_for(sid)
        qs = parse_qs(urlparse(self.path).query)
        from_run = (qs.get("from") or [""])[0] == "run"
        first_event = None
        replay_from = None
        if from_run:
            first_event = {"t": "hello", "sid": rt.sid, "project": rt.project_id or ""}
            # replay only while a run is actually live; a stale run_start_seq
            # from a finished turn must never re-send a completed exchange
            replay_from = getattr(rt, "run_start_seq", None) if rt.busy else None
        q, replay = rt.bus.subscribe_with_replay(replay_from)
        try:
            self._pump_bus(rt, q, replay=replay, first_event=first_event)
        finally:
            rt.bus.unsubscribe(q)

    # -- DELETE ----------------------------------------------------------------
    def do_DELETE(self):
        route = self._route()
        if not self._guard(check_origin=True):
            return
        self._walk_routes(_DELETE_ROUTES, route)

    def _delete_all_sessions(self) -> None:
        app = self.app
        with app.runtimes_lock:
            rts = list(app.runtimes.values())
            app.runtimes.clear()
        for rt in rts:
            app.retire_runtime(rt)
        for rt in rts:
            if rt.busy:
                rt.request_stop()
                rt.approver.cancel_pending("all sessions cleared")
        # same discipline as _delete_session: a busy worker must finish its
        # final transcript append before we unlink, or it recreates the file
        # afterwards (zombie session rising from a "wiped" store)
        for rt in rts:
            for _ in range(100):  # up to ~10s per runtime
                if not rt.busy:
                    break
                time.sleep(0.1)
        removed = 0
        for p in list(app.store.root.glob("*.jsonl")):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
        for p in list(app.store.root.glob("*.checkpoint.json")) + list(app.store.root.glob("*.meta.json")):
            try:
                p.unlink()
            except OSError:
                pass
        # the button promises "sessions, checkpoints and projects" - every
        # session is already gone by this point, so there is nothing left to
        # untag; delete each project the same way _delete_project does
        projects_removed = 0
        for proj in app.projects.list():
            if app.projects.delete(proj.id):
                projects_removed += 1
        self._send_json({"ok": True, "removed": removed, "projects_removed": projects_removed})

    def _delete_session(self, sid: str) -> None:
        app = self.app
        with app.runtimes_lock:
            rt = app.runtimes.pop(sid, None)
        if rt is not None:
            app.retire_runtime(rt)
            was_busy = rt.busy
            if was_busy:
                rt.request_stop()
                rt.approver.cancel_pending("session deleted")
                # let the worker observe the stop and finish its final append,
                # otherwise it recreates the file after we unlink (zombie session)
                for _ in range(100):  # up to ~10s
                    if not rt.busy:
                        break
                    time.sleep(0.1)
        removed = []
        base = app.store._path(sid)
        for p in (base, base.with_suffix(".checkpoint.json"), base.with_suffix(".meta.json")):
            try:
                p.unlink(missing_ok=True)
                removed.append(p.name)
            except OSError:
                pass
        self._send_json({"ok": True, "removed": removed})

    def _delete_project(self, pid: str) -> None:
        app = self.app
        if not app.projects.delete(pid):
            self._send_json({"error": "unknown project"}, 404)
            return
        untagged = 0
        for row in app.store.list_sessions():
            if row.get("project") == pid:
                if app.store.set_project(row["id"], ""):
                    untagged += 1
        with app.runtimes_lock:
            for rt in app.runtimes.values():
                if rt.project_id == pid:
                    rt.project_id = None
        self._send_json({"ok": True, "untagged": untagged})

    def do_PATCH(self):
        route = self._route()
        if not self._guard(check_origin=True):
            return
        payload = self._read_json()
        if payload is None:
            self._send_json({"error": "bad request"}, 400)
            return
        m = _RE_PROJECT.fullmatch(route)
        if m:
            self._patch_project(m.group(1), payload)
            return
        self._send_json({"error": "not found"}, 404)

    def _patch_project(self, pid: str, payload: dict) -> None:
        app = self.app
        try:
            proj = app.projects.update(
                pid,
                name=payload.get("name"),
                instructions=payload.get("instructions"),
                workspace=payload.get("workspace"),
                color=payload.get("color"),
                files=payload.get("files"),
                scopes=payload.get("scopes"),
            )
        except KeyError:
            self._send_json({"error": "unknown project"}, 404)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        out = {"ok": True, "project": proj.to_dict()}
        # live runtimes for this project must pick up new workspace /
        # scopes / instructions / knowledge immediately (stale scopes are
        # security-relevant); busy runtimes resync on their next rebuild
        with app.runtimes_lock:
            targets = [rt for rt in app.runtimes.values() if rt.project_id == pid and not rt.busy]
        for rt in targets:
            app._rebuild_runtime_agent(rt)
        out["projects"] = app.projects_payload()
        self._send_json(out)

    # -- runs monitor (Warp/Cursor-style parallel-agents panel) -------------------

    def _get_runs(self) -> None:
        app = self.app
        with app.runtimes_lock:
            runtimes = dict(app.runtimes)
        out = []
        for r in app.store.list_sessions():
            rt = runtimes.get(r["id"])
            busy = bool(rt is not None and rt.busy)
            model = ""
            if rt is not None:
                model = getattr(getattr(rt.agent, "cfg", None), "model", "") or ""
            out.append(
                {
                    "id": r["id"],
                    "task": r.get("task", ""),
                    "project": r.get("project", "") or "",
                    "archived": bool(r.get("archived", False)),
                    "mtime": r.get("mtime", 0),
                    "busy": busy,
                    "started_at": (getattr(rt, "run_started_at", 0.0) or 0.0) if busy else 0.0,
                    "stopping": bool(rt is not None and rt.should_stop()),
                    "model": model,
                }
            )
        self._send_json({"runs": out})

    # -- archive -----------------------------------------------------------------

    def _post_archive(self, payload: dict) -> None:
        app = self.app
        sid = str(payload.get("session_id") or "")
        if not app.store.set_archived(sid, bool(payload.get("archived"))):
            self._send_json({"error": "unknown session"}, 404)
            return
        self._send_json({"ok": True, "sessions": app.store.list_sessions()})

    # -- git status chip (read-only) -----------------------------------------------

    def _get_git_status(self) -> None:
        import subprocess
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        sid = unquote((qs.get("sid") or [""])[0])
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root

        def git(*args: str):
            # read-only plumbing; core.fsmonitor disabled so a hostile repo
            # config cannot execute a helper binary from a status/diff probe
            try:
                p = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false", *args],
                    cwd=ws, capture_output=True, text=True, timeout=4,
                    encoding="utf-8", errors="replace",
                )
                return p if p.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return None

        if git("rev-parse", "--show-toplevel") is None:
            self._send_json({"available": False, "workspace": ws})
            return
        bp = git("rev-parse", "--abbrev-ref", "HEAD")
        branch = (bp.stdout or "").strip() if bp is not None else "?"
        branch = branch or "?"
        adds = dels = 0
        for out in (git("diff", "--numstat"), git("diff", "--cached", "--numstat")):
            if not out:
                continue
            for line in (out.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    adds += int(parts[0])
                    dels += int(parts[1])
        files: list[str] = []
        st = git("status", "--porcelain")
        for line in (st.stdout or "").splitlines():
            if len(line) > 3:
                files.append(line[3:].strip().strip('"'))
        self._send_json(
            {
                "available": True,
                "branch": branch,
                "changed": len(files),
                "adds": adds,
                "dels": dels,
                "files": files[:60],
                "workspace": ws,
            }
        )

    # -- POST ------------------------------------------------------------------
    def do_POST(self):
        if not self._guard(check_origin=True):
            return
        payload = self._read_json()
        # _read_json accepts any JSON value, but every _post_* handler starts
        # with payload.get(...) - a body of `[]` or `"x"` used to raise
        # AttributeError out of here and answer with a closed socket
        if not isinstance(payload, dict):
            self._send_json({"error": "bad request"}, 400)
            return
        fname = _POST_ROUTES.get(self._route())
        if fname is None:
            self._send_json({"error": "not found"}, 404)
            return
        self._invoke(fname, payload)

    def _post_plan(self, payload: dict) -> None:
        app = self.app
        sid = str(payload.get("session_id") or "")
        with app.runtimes_lock:
            rt = app.runtimes.get(sid)
        if rt is None:
            self._send_json({"error": "unknown session"}, 404)
            return
        want = payload.get("on")
        rt.agent.plan_mode = bool(want) if isinstance(want, bool) else not rt.agent.plan_mode
        self._send_json({"ok": True, "session_id": sid, "plan_mode": rt.agent.plan_mode})

    def _post_branch(self, payload: dict) -> None:
        app = self.app
        sid = str(payload.get("session_id") or "")
        keep = payload.get("keep")
        keep_arg = int(keep) if isinstance(keep, int) and keep >= 1 else None
        with app.runtimes_lock:
            busy_rt = app.runtimes.get(sid)
            if busy_rt is not None and busy_rt.busy:
                self._send_json({"error": "session busy - stop the run before branching"}, 409)
                return
        new_sid = app.store.branch(sid, keep_arg)
        if new_sid is None:
            self._send_json({"error": "nothing to branch"}, 400)
            return
        self._send_json({"ok": True, "session_id": new_sid, "branched_from": sid, "sessions": app.store.list_sessions()})

    # -- file-edit journal (Cline/Roo-style per-edit restore) --------------------

    def _get_memgraph(self) -> None:
        """Nodes and edges for the memory view. Cached per workspace: the repo
        index rebuild behind it is cheap but not free, and the view repolls."""
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        sid = unquote((qs.get("sid") or [""])[0])
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root
        fresh = (qs.get("refresh") or [""])[0] in ("1", "true")
        key = str(ws or "")
        with self.app.memgraph_lock:
            hit = None if fresh else self.app.memgraph_cache.get(key)
        if hit is not None and time.time() - hit[0] < 300.0:
            self._send_json(hit[1])
            return
        try:
            from saturday.memgraph import build_graph

            graph = build_graph(ws)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        graph["workspace"] = str(ws or "")
        with self.app.memgraph_lock:
            self.app.memgraph_cache[key] = (time.time(), graph)
        self._send_json(graph)

    def _get_memgraph_expand(self) -> None:
        """Level-1 detail: the symbols of one file, spliced into the open graph.

        Prefers a configured language server's documentSymbol over the index's
        ast-derived symbols when the session's config has one for this file's
        language (expand_file resolves and guards the path itself in that
        case - see memgraph._lsp_symdefs); falls back to the index otherwise,
        where `path` is only ever a dict key and is never opened."""
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        sid = unquote((qs.get("sid") or [""])[0])
        rel = unquote((qs.get("path") or [""])[0]).strip().replace("\\", "/")
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root
        empty = {"nodes": [], "edges": [], "path": rel, "truncated": False}
        if not rel or not ws:
            self._send_json(empty)
            return
        try:
            from saturday.memgraph import expand_file

            lsp_servers = dict(getattr(self.app.base_cfg, "lsp_servers", None) or {})
            self._send_json(expand_file(ws, rel, lsp_servers_cfg=lsp_servers))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _get_memgraph_focus(self) -> None:
        """Level-2 detail: callers and callees of one already-expanded symbol.

        No ast fallback exists for this - see memgraph.focus_symbol's own
        docstring - so an unconfigured or missing server answers empty rather
        than erroring, the same contract expand_file uses."""
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        sid = unquote((qs.get("sid") or [""])[0])
        rel = unquote((qs.get("path") or [""])[0]).strip().replace("\\", "/")
        name = unquote((qs.get("name") or [""])[0])
        try:
            line = int((qs.get("line") or ["0"])[0])
            column = int((qs.get("column") or ["0"])[0])
        except ValueError:
            line, column = 0, 0
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root
        empty = {"nodes": [], "edges": [], "path": rel, "truncated": False}
        if not rel or not ws or not name or not line:
            self._send_json(empty)
            return
        try:
            from saturday.memgraph import focus_symbol

            lsp_servers = dict(getattr(self.app.base_cfg, "lsp_servers", None) or {})
            self._send_json(focus_symbol(ws, rel, name, line, column,
                                          lsp_servers_cfg=lsp_servers))
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _get_browse(self) -> None:
        """Directory listing for the folder picker.

        Deliberately outside the workspace sandbox: its whole job is choosing
        which folder becomes the workspace. It returns directory NAMES only,
        never file contents, and the app is already bound to localhost behind
        a token - the same surface that has always accepted a typed path."""
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        raw = unquote((qs.get("path") or [""])[0])
        home = Path.home()
        try:
            here = Path(raw).expanduser().resolve() if raw else home
        except (OSError, RuntimeError):
            here = home
        if not here.is_dir():
            here = home

        dirs = []
        denied = False
        try:
            for child in sorted(here.iterdir(), key=lambda c: c.name.lower()):
                if child.name.startswith("."):
                    continue
                try:
                    if not child.is_dir():
                        continue
                    # a .git marker is how you spot the folder you actually want
                    # in a home directory full of folders you do not
                    repo = (child / ".git").exists()
                except OSError:
                    continue
                dirs.append({"name": child.name, "path": str(child), "repo": repo})
                if len(dirs) >= 500:
                    break
        except PermissionError:
            denied = True
        except OSError as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        crumbs = [{"name": p.name or str(p), "path": str(p)} for p in reversed(here.parents)]
        crumbs.append({"name": here.name or str(here), "path": str(here)})
        self._send_json({
            "path": str(here),
            "parent": str(here.parent) if here.parent != here else "",
            "crumbs": crumbs,
            "dirs": dirs,
            "denied": denied,
            "home": str(home),
            "is_repo": (here / ".git").exists(),
        })

    def _get_mcp(self) -> None:
        """Configured MCP servers, and optionally what each one actually serves.

        Listing is cheap and safe. Probing SPAWNS every configured server, so
        it is opt-in behind ?probe=1 and never runs on page load - the same
        split `saturday mcp` makes between reading config and connecting."""
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        probe = (qs.get("probe") or [""])[0] in ("1", "true")
        only = unquote((qs.get("server") or [""])[0])
        sid = unquote((qs.get("sid") or [""])[0])
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root

        warnings: list[str] = []
        servers: dict[str, dict] = {}
        project_aliases: set[str] = set()
        # NB: never chdir here - the handler is threaded and cwd is process
        # global, so a second request (or the agent) would see it move. Read
        # the workspace's file by path instead.
        trusted = False
        if ws:
            # separate concerns: an unreadable trust store must not blank the
            # server list, and an unreadable mcp.json must not imply trust
            try:
                from saturday.utils.trust import is_trusted

                trusted = is_trusted(ws)
            except Exception as exc:
                warnings.append(f"trust state unreadable: {type(exc).__name__}: {exc}")
            try:
                cfg_path = Path(ws) / ".saturday" / "mcp.json"
                if cfg_path.is_file():
                    from saturday.mcp_plugin import load_mcp_config

                    project = load_mcp_config(cfg_path, warnings=warnings)
                    project_aliases = set(project)
                    servers.update(project)
                    if not trusted and project:
                        warnings.append(
                            "this project is not trusted yet, so its MCP servers are listed "
                            "but will not be started"
                        )
            except Exception as exc:
                warnings.append(f"{type(exc).__name__}: {exc}")
        servers.update(self.app.base_cfg.mcp_servers or {})
        # the vendored structural index speaks MCP, so it appears here like any
        # other server rather than through a bespoke channel
        try:
            from saturday import codemem

            vendored = codemem.mcp_spec()
            if vendored and codemem.MCP_ALIAS not in servers:
                servers[codemem.MCP_ALIAS] = vendored
        except Exception:
            pass

        out = []
        for alias, spec in sorted(servers.items()):
            if only and alias != only:
                continue
            command = spec.get("command") or ""
            argv = [str(a) for a in (spec.get("args") or [])]
            from_project = alias in project_aliases
            row = {
                "alias": alias,
                "source": "project" if from_project else "global",
                "transport": "http" if spec.get("url") else "stdio",
                "command": (command + " " + " ".join(argv)).strip() or str(spec.get("url") or ""),
                "status": "unknown",
                "tools": [],
            }
            if probe:
                if from_project and not trusted:
                    row.update({"status": "blocked",
                                "error": "project not trusted; trust it to start its servers"})
                else:
                    row.update(self._probe_mcp(spec))
            out.append(row)
        self._send_json({"servers": out, "warnings": warnings, "probed": probe,
                         "workspace": str(ws or ""), "trusted": trusted})

    @staticmethod
    def _probe_mcp(spec: dict) -> dict:
        from saturday.mcp_client import McpHttpClient, McpStdioClient

        client = None
        try:
            if spec.get("url"):
                client = McpHttpClient(str(spec["url"]), headers=spec.get("headers"), call_timeout=12.0)
            else:
                argv = [str(spec["command"])] + [str(a) for a in (spec.get("args") or [])]
                client = McpStdioClient(command=argv, env=spec.get("env"), call_timeout=12.0)
            info = client.start()
            tools = client.list_tools()
            return {
                "status": "ok",
                "server_name": str(info.get("name") or ""),
                "server_version": str(info.get("version") or ""),
                "tools": [{"name": t.name, "description": (t.description or "")[:200]} for t in tools],
            }
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:400], "tools": []}
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _get_audit(self) -> None:
        """Chain verification for stored sessions.

        Saturday's claim is that history is tamper evident; that claim was
        only checkable from `saturday audit`. Verifying rereads whole
        transcripts, so it is button triggered, never part of page load."""
        from urllib.parse import parse_qs, unquote, urlparse

        qs = parse_qs(urlparse(self.path).query)
        sid = unquote((qs.get("sid") or [""])[0])
        want_bundle = (qs.get("export") or [""])[0] in ("1", "true")
        store = self.app.store

        if sid:
            if want_bundle:
                bundle = store.audit_export(sid)
                if bundle is None:
                    self._send_json({"error": "unknown session"}, 404)
                    return
                raw = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="saturday-audit-{sid}.json"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            status = store.audit_verify(sid)
            if status is None:
                self._send_json({"error": "unknown session"}, 404)
                return
            self._send_json({"sessions": [dict(status, id=sid)], "checked": 1, "tampered": 0 if status["ok"] else 1})
            return

        rows = []
        tampered = 0
        try:
            listing = store.list_sessions()
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        for r in listing[:400]:
            try:
                status = store.audit_verify(r["id"]) or {}
            except Exception as exc:
                status = {"ok": None, "error": f"{type(exc).__name__}: {exc}"}
            if status.get("ok") is False:
                tampered += 1
            rows.append({
                "id": r["id"],
                "task": r.get("task") or "",
                "ok": status.get("ok"),
                "records": status.get("records", 0),
                "hashed": status.get("hashed", 0),
                "legacy": status.get("legacy", 0),
                "broken_at": status.get("broken_at"),
            })
        self._send_json({"sessions": rows, "checked": len(rows), "tampered": tampered,
                         "truncated": len(listing) > 400})

    def _get_tools(self) -> None:
        """Every tool the agent can call, with what it does.

        The settings checklist has always known the NAMES; `saturday tools` was
        the only place that said what any of them actually do."""
        from saturday.tools.base import ToolRegistry

        disabled = set(ToolRegistry.expand_tool_names(
            getattr(self.app.base_cfg, "disabled_tools", []) or []))
        try:
            specs = self.app.make_agent()._build_registry().specs()
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        out = [{
            "name": sp["name"],
            "description": (sp.get("description") or "").strip(),
            "enabled": sp["name"] not in disabled,
        } for sp in sorted(specs, key=lambda x: x["name"])]
        self._send_json({"tools": out, "disabled": sorted(disabled)})

    def _get_doctor(self) -> None:
        """The same preflight checks `saturday doctor` runs.

        The user least able to read a terminal is the one most likely to have
        a broken setup, so this has to be answerable from the app. It probes
        the provider endpoint, so it is button triggered, not polled."""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        offline = (qs.get("offline") or [""])[0] in ("1", "true")
        from saturday.diagnostics import failure_count, run_checks

        try:
            checks = run_checks(self.app.base_cfg, offline=offline)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._send_json({"checks": checks, "failures": failure_count(checks)})

    def _get_update(self) -> None:
        """Is there a newer Saturday, and how would this install get it.

        Checking hits the GitHub API, so it is button triggered. Applying is
        deliberately NOT offered here: the update replaces the package this
        server is running from and then wants to relaunch, which is not
        something to do underneath a live HTTP request. The UI reports the
        version and the exact command instead."""
        from saturday import update as upd

        cur = upd.current_version()
        try:
            rel = upd.latest_release()
        except Exception as exc:
            self._send_json({"current": cur, "error": f"{type(exc).__name__}: {exc}"})
            return
        if rel is None:
            self._send_json({"current": cur,
                             "error": "update check failed (network unreachable or GitHub API error)"})
            return
        latest = rel.get("tag") or ""
        channel = upd.detect_channel()
        self._send_json({
            "current": cur,
            "latest": latest,
            "newer": bool(latest) and upd.is_newer(latest, cur),
            "channel": channel,
            "command": "saturday update --apply",
            "manual": upd.manual_update_hint(channel),
            "url": rel.get("url") or "",
        })

    def _get_update_history(self) -> None:
        """Past update attempts. Purely local (the JSONL receipt log), so
        unlike _get_update this never touches the network and is safe to
        load automatically when the About pane opens, not gated behind a
        button."""
        from saturday import update as upd

        self._send_json({"receipts": upd.read_receipts()})

    def _get_memory(self) -> None:
        """Search the memory index, or return its graph.

        MEMORY.md is the truth and this index is derived from it, so every
        request reindexes first - the file may have been edited by hand or by
        the agent since the last call, and a stale answer is worse than a
        slightly slower one."""
        from urllib.parse import parse_qs, unquote, urlparse

        from saturday.memindex import MemoryIndex
        from saturday.tools.memory import memory_path

        qs = parse_qs(urlparse(self.path).query)
        query = unquote((qs.get("q") or [""])[0])
        sid = unquote((qs.get("sid") or [""])[0])
        want_graph = (qs.get("graph") or [""])[0] in ("1", "true")
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root

        idx = MemoryIndex()
        try:
            sources = [("global", memory_path())]
            if ws:
                proj = Path(ws) / ".saturday" / "MEMORY.md"
                if proj.is_file():
                    sources.append((f"project:{Path(ws).resolve()}", proj))
            for scope, path in sources:
                text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                idx.reindex(text, scope=scope)
            if want_graph or not query:
                self._send_json(idx.graph())
                return
            self._send_json({"query": query,
                             "results": idx.search(query, k=int((qs.get("k") or ["8"])[0] or 8))})
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            idx.close()

    def _get_codemem(self) -> None:
        """Is structural code retrieval active, and what would enable it."""
        from saturday import codemem

        try:
            self._send_json(codemem.status())
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _post_codemem(self, payload: dict) -> None:
        """Install the pinned, checksum-verified binary - the GUI equivalent
        of `saturday codemem install`. Synchronous like the CLI: this is an
        explicit button press, never part of page load."""
        from saturday import codemem

        st = codemem.status()
        if not st["supported"]:
            self._send_json({"ok": False, "error": f"no pinned build for {st['platform']}"}, status=400)
            return
        if st["available"] and not bool(payload.get("force")):
            self._send_json({"ok": True, "already_installed": True, "path": st["binary"]})
            return
        try:
            path = codemem.install()
        except codemem.VerificationError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        except Exception as exc:
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        self._send_json({"ok": True, "path": str(path)})

    def _post_memory_consolidate(self, payload: dict) -> None:
        """Report stale and redundant notes; archive only when asked.

        Archiving mutates the index, so it never happens on a GET - a page
        load must not quietly change what the agent can recall."""
        from saturday.memindex import MemoryIndex
        from saturday.tools.memory import memory_path

        sid = str(payload.get("sid") or "")
        apply_it = bool(payload.get("apply"))
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root
        idx = MemoryIndex()
        try:
            sources = [("global", memory_path())]
            if ws:
                proj = Path(ws) / ".saturday" / "MEMORY.md"
                if proj.is_file():
                    sources.append((f"project:{Path(ws).resolve()}", proj))
            for scope, path in sources:
                text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                idx.reindex(text, scope=scope)
            self._send_json(idx.consolidate(dry_run=not apply_it, workspace=ws))
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            idx.close()

    def _get_skills(self) -> None:
        """Installed skills, or a search of the public topic index."""
        from urllib.parse import parse_qs, unquote, urlparse

        from saturday import skillhub

        qs = parse_qs(urlparse(self.path).query)
        query = unquote((qs.get("q") or [""])[0])
        # browse is its own mode, not "search with no words": an empty filter
        # means show the whole catalogue, which is what makes it a catalogue
        browsing = (qs.get("browse") or [""])[0] in ("1", "true") or bool(query)
        try:
            if browsing:
                self._send_json(skillhub.search(
                    query, limit=int((qs.get("limit") or ["60"])[0] or 60)))
                return
            self._send_json({"installed": skillhub.list_installed()})
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _post_skills(self, payload: dict) -> None:
        """Install, update or remove. All three write to disk, so none of them
        is reachable by loading a page."""
        from saturday import skillhub

        action = str(payload.get("action") or "")
        try:
            if action == "install":
                url = str(payload.get("url") or "")
                if not url:
                    self._send_json({"error": "url required"}, 400)
                    return
                out = skillhub.install(url, force=bool(payload.get("force")))
                self._send_json({"ok": True, "skill": out,
                                 "installed": skillhub.list_installed()})
                return
            if action == "remove":
                name = str(payload.get("name") or "")
                ok = skillhub.remove(name) if name else False
                self._send_json({"ok": ok, "installed": skillhub.list_installed()})
                return
            if action == "update":
                results = skillhub.update(str(payload.get("name") or "") or None)
                self._send_json({"ok": True, "results": results,
                                 "installed": skillhub.list_installed()})
                return
        except skillhub.SkillError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._send_json({"error": "unknown action"}, 400)

    def _get_pipelines(self) -> None:
        """List pipelines, or return one by ?name=."""
        from urllib.parse import parse_qs, unquote, urlparse

        from saturday import pipeline as P

        qs = parse_qs(urlparse(self.path).query)
        name = unquote((qs.get("name") or [""])[0])
        try:
            if name:
                data = P.load(name)
                self._send_json({"pipeline": data, "problems": P.validate(data)})
                return
            # the canvas enforces the same socket rules the engine does, so
            # it is served them rather than keeping a second copy in JS
            self._send_json({"pipelines": P.list_pipelines(),
                             "dir": str(P.pipelines_dir()),
                             "kinds": {k: {"in": list(i), "out": list(o)}
                                       for k, (i, o) in P.NODE_KINDS.items()},
                             "templates": P.templates()})
        except P.PipelineError as exc:
            self._send_json({"error": str(exc)}, 404)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _post_pipelines(self, payload: dict) -> None:
        """Save or validate. Saving refuses a graph that cannot run, which is
        the point of typing the sockets in the first place."""
        from saturday import pipeline as P

        action = str(payload.get("action") or "save")
        name = str(payload.get("name") or "")
        try:
            if action == "validate":
                self._send_json({"problems": P.validate(payload.get("pipeline") or {})})
                return
            if action == "save":
                path = P.save(name, payload.get("pipeline") or {})
                self._send_json({"ok": True, "path": str(path),
                                 "pipelines": P.list_pipelines()})
                return
            if action == "new":
                pipe = P.from_template(str(payload.get("template") or "one-agent"), name)
                P.save(name, pipe)
                self._send_json({"ok": True, "pipeline": pipe,
                                 "pipelines": P.list_pipelines()})
                return
            if action == "clear-cache":
                self._send_json({"ok": True, "cleared": P.clear_cache(name or None)})
                return
        except P.PipelineError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._send_json({"error": "unknown action"}, 400)

    def _post_pipeline_run(self, payload: dict) -> None:
        """Start a pipeline run in the background and return its session.

        A run spends real model calls, so it never blocks the request: it takes
        the same busy latch, publishes on the same bus and finishes with the
        same done event as a chat run, which means /api/stream/<sid> carries it
        with no new transport and no frontend change."""
        from saturday import pipeline as P

        app = self.app
        name = str(payload.get("name") or "")
        task = str(payload.get("input") or "").strip()
        if not name or not task:
            self._send_json({"error": "name and input are required"}, 400)
            return
        try:
            pipe = P.load(name)
        except P.PipelineError as exc:
            self._send_json({"error": str(exc)}, 404)
            return
        problems = P.validate(pipe)
        if problems:
            self._send_json({"error": "; ".join(problems)}, 400)
            return

        sid = str(payload.get("sid") or "")
        try:
            rt = app.runtime_for(sid) if sid else None
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        if rt is None:
            newsid = app.store.create({"task": f"pipeline: {name}", "surface": "app"})
            rt = app.runtime_for(newsid)
        if not rt.try_begin_run():
            self._send_json({"error": "session busy", "session_id": rt.sid}, 409)
            return

        use_cache = payload.get("cache", True) is not False
        cfg_overrides = dict(app.cfg_overrides)

        runner = _pipeline_agent_runner(cfg_overrides)

        def on_event(evt: dict) -> None:
            if evt.get("type") == "pipeline_node":
                label = evt.get("node") or ""
                kind = evt.get("kind") or ""
                suffix = " (cached)" if evt.get("cached") else ""
                rt.bus.publish({"t": "notice", "s": f"[{name}] {kind} {label}{suffix}"})

        def work() -> None:
            # the run moved off the request thread, and a fresh thread starts
            # with no context, so what named this run has to be reinstated here
            rt.run_thread = threading.current_thread()
            try:
                from saturday import attention

                attention.set_run(rt.sid)
            except Exception:
                pass
            final, stop = "", "pipeline"
            try:
                out = P.run(pipe, task, agent_runner=runner,
                            memory_lookup=_pipeline_memory_lookup(),
                            on_event=on_event, use_cache=use_cache)
                final = str(out.get("output") or "")
            except Exception as exc:
                stop = "error"
                rt.bus.publish({"t": "notice", "s": f"[{name}] failed: {type(exc).__name__}: {exc}"})
            finally:
                # STATE MACHINE INVARIANT (session_runtime.py): finish_run()
                # happens BEFORE the terminal event. Stream pumps exit on
                # done/error only once the runtime is idle, so publishing first
                # left a viewer hanging on the stream until the next ping
                # failed. The chat path already does this; the pipeline path
                # had it the wrong way round.
                finish_and_announce(rt, {"t": "done", "final": final, "stop_reason": stop,
                                          "steps": 0, "tokens": 0, "sid": rt.sid})

        threading.Thread(target=work, daemon=True,
                         name=f"saturday-pipeline-{name}").start()
        self._send_json({"ok": True, "session_id": rt.sid, "pipeline": name})

    def _get_journal(self) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        from saturday.tools.journal import load_entries

        qs = parse_qs(urlparse(self.path).query)
        sid = unquote((qs.get("sid") or [""])[0])
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root
        try:
            entries = load_entries(ws, limit=30)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        # ?entry=N returns the full record (including the `before` snapshot)
        # so the UI can render a compare diff before restoring
        entry_q = (qs.get("entry") or [""])[0]
        if entry_q != "":
            try:
                idx = int(entry_q)
            except ValueError:
                self._send_json({"error": "bad entry index"}, 400)
                return
            if idx < 0 or idx >= len(entries):
                self._send_json({"error": "no such entry"}, 404)
                return
            self._send_json({"entry": entries[idx], "index": idx})
            return
        slim = [
            {
                "index": i,
                "ts": e.get("ts"),
                "tool": e.get("tool"),
                "path": e.get("path"),
                "existed": bool(e.get("existed", True)),
                "chars": len(e.get("before") or ""),
            }
            for i, e in enumerate(entries)
        ]
        self._send_json({"workspace": ws, "entries": slim})

    def _post_journal_restore(self, payload: dict) -> None:
        from saturday.tools.journal import restore_entry

        sid = str(payload.get("session_id") or "")
        index = payload.get("index")
        if not isinstance(index, int) or index < 0:
            self._send_json({"error": "bad index"}, 400)
            return
        ws = self.app.session_workspace(sid) or self.app.base_cfg.workspace_root
        ok, msg = restore_entry(ws, index)
        # surface the manual intervention in the transcript when the session
        # already has a runtime (idle ones do); fire-and-forget otherwise
        with self.app.runtimes_lock:
            rt = self.app.runtimes.get(sid)
        if rt is not None and not rt.busy:
            rt.bus.publish({"t": "notice", "s": "[revert] " + msg})
        self._send_json({"ok": ok, "message": msg})

    # -- scheduled automations (cron) ---------------------------------------------

    def _get_schedules(self) -> None:
        from saturday.schedule import ScheduleStore, default_schedules_path

        rows = [
            {
                "id": s.id,
                "expr": s.expr,
                "task": s.task,
                "model": s.model,
                "provider": s.provider,
                "last_fired_minute": s.last_fired_minute,
                "created": s.created,
            }
            for s in ScheduleStore().list()
        ]
        self._send_json({"schedules": rows, "path": str(default_schedules_path()), "watcher": SCHEDULE_WATCHER_ON})

    def _post_schedules(self, payload: dict) -> None:
        from saturday.schedule import ScheduleStore

        action = str(payload.get("action") or "")
        store = ScheduleStore()
        if action == "add":
            expr = str(payload.get("expr") or "").strip()
            task = str(payload.get("task") or "").strip()
            sid = str(payload.get("id") or "").strip()
            if not task:
                self._send_json({"error": "task is required"}, 400)
                return
            try:
                store.add(sid, expr, task)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            start_schedule_watcher(self.app)
        elif action == "remove":
            if not store.remove(str(payload.get("id") or "")):
                self._send_json({"error": "no schedule with that id"}, 404)
                return
        else:
            self._send_json({"error": "action must be add or remove"}, 400)
            return
        rows = [
            {"id": s.id, "expr": s.expr, "task": s.task, "model": s.model, "provider": s.provider, "last_fired_minute": s.last_fired_minute, "created": s.created}
            for s in store.list()
        ]
        self._send_json({"ok": True, "schedules": rows, "watcher": SCHEDULE_WATCHER_ON})

    # -- external trigger: kick off a run without chat or the scheduler -----------

    def _post_trigger(self, payload: dict) -> None:
        """A third way in, beside chat and cron: something outside Saturday
        (a CI step, another service, a cron job Saturday doesn't own) POSTs a
        task here and gets a session id back immediately. The run itself is a
        real detached process, not a thread inside this server - same
        contract 'saturday run --detach' already uses (log under
        .saturday/bg/<id>.log, resumable via 'saturday chat --resume <id>',
        visible to 'saturday sessions'), so a webhook-started run is
        indistinguishable from one a person started by hand once it's
        running. This does not change WHO can reach Saturday - it goes
        through the same _guard() token/Origin check as every other POST
        route - only what a caller who is already allowed to reach it can
        ask for."""
        task = str(payload.get("task") or "").strip()
        if not task:
            self._send_json({"error": "task is required"}, 400)
            return
        session_id = str(payload.get("session") or "").strip() or time.strftime("hook-%Y%m%d-%H%M%S")
        # The id names a log file, so it must be a single safe path segment.
        # Taken raw it escaped the directory - session="../../../../home/u/.bashrc"
        # appended run output there - and the token that gates this route is the
        # same one a remote tunnel URL hands out.
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "-", session_id).strip(".-")[:80]
        if not safe_id:
            self._send_json({"error": "session must contain usable characters"}, 400)
            return
        session_id = safe_id
        # relative to the workspace, not the process cwd, so a webhook-started
        # run logs where `saturday run --detach` puts its logs
        ws = getattr(self.app.base_cfg, "workspace_root", "") or "."
        log_dir = Path(ws) / ".saturday" / "bg"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._send_json({"error": f"could not prepare log directory: {exc}"}, 500)
            return
        log_path = (log_dir / f"{session_id}.log").resolve()
        if not str(log_path).startswith(str(log_dir.resolve()) + os.sep):
            self._send_json({"error": "invalid session id"}, 400)
            return
        argv = [sys.executable, "-m", "saturday", "run", task, "--session", session_id]
        model = str(payload.get("model") or "").strip()
        provider = str(payload.get("provider") or "").strip()
        if model:
            argv += ["--model", model]
        if provider:
            argv += ["--provider", provider]
        try:
            with open(log_path, "ab") as log_fh:
                proc = subprocess.Popen(
                    argv,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=getattr(self.app.base_cfg, "workspace_root", None) or None,
                    close_fds=True,
                )
        except OSError as exc:
            self._send_json({"error": f"could not start run: {exc}"}, 500)
            return
        self._send_json({"ok": True, "session_id": session_id, "pid": proc.pid, "log": str(log_path)})

    # -- custom slash commands (prompt library) ------------------------------------

    def _post_commands(self, payload: dict) -> None:
        from saturday.webui_support import load_custom_commands, save_custom_commands

        cmds = payload.get("commands")
        if not isinstance(cmds, dict):
            self._send_json({"error": "commands must be an object"}, 400)
            return
        cleaned: dict[str, dict] = {}
        for name, val in cmds.items():
            key = str(name).strip().lstrip("/").lower()
            if not key:
                continue
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", key):
                self._send_json({"error": f"invalid command name: {key!r} (use a-z 0-9 _ -)"}, 400)
                return
            if not isinstance(val, dict) or not str(val.get("prompt") or "").strip():
                continue
            cleaned[key] = {
                "prompt": str(val["prompt"]).strip()[:8000],
                "description": str(val.get("description") or "")[:200],
            }
        try:
            save_custom_commands(cleaned)
        except (OSError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 500)
            return
        self._send_json({"ok": True, "commands": load_custom_commands()})

    # -- per-turn feedback (local reward signal) ------------------------------------

    def _post_feedback(self, payload: dict) -> None:
        from saturday.webui_support import append_feedback

        value = str(payload.get("value") or "")
        if value not in ("up", "down"):
            self._send_json({"error": "value must be up or down"}, 400)
            return
        append_feedback(
            {
                "ts": time.time(),
                "sid": str(payload.get("sid") or ""),
                "turn": int(payload.get("turn") or 0),
                "value": value,
                "model": str(payload.get("model") or ""),
                "note": str(payload.get("note") or "")[:2000],
            }
        )
        self._send_json({"ok": True})

    def _post_hooks(self, payload: dict) -> None:
        app = self.app
        if payload.get("read_only"):
            self._send_json({"hooks": app.hooks_state()})
            return
        # This validated and wrote hooks.json line for line the same way
        # AppState._write_hooks does, so the two rules could drift apart and
        # the same request would be accepted through one route and rejected
        # through the other. One implementation, two callers. (S19)
        try:
            app._write_hooks(payload.get("hooks"))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        except OSError as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._send_json({"ok": True, "hooks": app.hooks_state()})

    def _post_approvals_remove(self, payload: dict) -> None:
        app = self.app
        rule = str(payload.get("rule") or "")
        try:
            from saturday.approvals_store import remove_rule

            removed = remove_rule("allow", rule)
        except Exception as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json({"ok": removed, "approvals_allow": app.approval_rules()})

    def _post_approve(self, payload: dict) -> None:
        app = self.app
        aid = str(payload.get("id") or "")
        decision = str(payload.get("decision") or "")
        note = str(payload.get("note") or "")[:500]
        if decision not in ("allow", "always", "deny"):
            self._send_json({"error": "bad decision"}, 400)
            return
        resolved = False
        with app.runtimes_lock:
            rts = list(app.runtimes.values())
        for rt in rts:
            if rt.approver.resolve(aid, decision, note=note):
                resolved = True
                break
        self._send_json({"ok": resolved})

    # -- ask_user resolution --------------------------------------------------------

    def _post_ask(self, payload: dict) -> None:
        app = self.app
        aid = str(payload.get("id") or "")
        answer = str(payload.get("answer") or "")[:2000]
        if not aid:
            self._send_json({"error": "id required"}, 400)
            return
        resolved = False
        with app.runtimes_lock:
            rts = list(app.runtimes.values())
        for rt in rts:
            if rt.approver.resolve(aid, "answer", note=answer):
                resolved = True
                break
        self._send_json({"ok": resolved})

    # -- one-shot prompt enhancer (Bolt parity) ---------------------------------------

    def _post_enhance(self, payload: dict) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            self._send_json({"error": "empty text"}, 400)
            return
        if len(text) > 8000:
            self._send_json({"error": "text too long (max 8000 chars)"}, 400)
            return
        cfg = self.app.base_cfg
        prompt = (
            "Improve the following prompt for a coding/agent assistant. Make it specific, "
            "structured and actionable; keep the user's intent and language; do not answer "
            "the prompt. Reply with the improved prompt only.\n\n---\n" + text + "\n---"
        )
        try:
            improved = _one_shot(cfg, prompt, max_tokens=1200, temperature=0.4)
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return
        improved = improved.strip().strip("`").strip()
        if improved.startswith("json"):
            improved = improved[4:].strip()
        if not improved:
            self._send_json({"error": "empty response from model"}, 502)
            return
        self._send_json({"ok": True, "text": improved})

    # -- AI follow-up suggestions (Devin/Cursor parity) --------------------------------

    def _post_suggest(self, payload: dict) -> None:
        app = self.app
        sid = str(payload.get("session_id") or "")
        if not sid or not getattr(app.base_cfg, "suggest_followups", True):
            self._send_json({"ok": True, "suggestions": []})
            return
        data = hydrate_session(app.store, sid)
        if not data or not data.get("items"):
            self._send_json({"ok": True, "suggestions": []})
            return
        last_user = last_asst = ""
        for item in reversed(data["items"]):
            role = item.get("kind")
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if not last_asst and role == "assistant":
                last_asst = text
            elif not last_user and role == "user":
                last_user = text
            if last_user and last_asst:
                break
        if not last_asst:
            self._send_json({"ok": True, "suggestions": []})
            return
        clip = lambda s: (s[:1500] + "…") if len(s) > 1500 else s  # noqa: E731
        prompt = (
            "A coding agent just finished a turn in a session. Based on this "
            "exchange, propose 3 short, concrete follow-up prompts the user "
            "might send next (max 8 words each, imperative, no numbering, "
            "one per line, no quotes). Reply with the 3 lines only.\n\n"
            "User: " + clip(last_user) + "\n\nAssistant reply: " + clip(last_asst)
        )
        try:
            raw = _one_shot(app.base_cfg, prompt, max_tokens=80, temperature=0.4)
        except Exception:
            # suggestions are best-effort chrome — never surface model errors
            self._send_json({"ok": True, "suggestions": []})
            return
        out = []
        for line in raw.splitlines():
            line = line.strip().strip("-•*0123456789. ").strip()
            if line and 3 <= len(line) <= 80 and line not in out:
                out.append(line)
            if len(out) == 3:
                break
        self._send_json({"ok": True, "suggestions": out})

    def _post_stop(self, payload: dict) -> None:
        app = self.app
        sid = str(payload.get("session_id") or "")
        with app.runtimes_lock:
            rt = app.runtimes.get(sid)
        if rt is None:
            self._send_json({"ok": False, "error": "unknown session"})
            return
        rt.request_stop()
        rt.approver.cancel_pending("stop requested")
        self._send_json({"ok": True})

    def _post_config(self, payload: dict) -> None:
        app = self.app
        # session-scoped model override (Cline/Amp parity): with a session_id
        # the model applies to THIS chat only; global config is untouched
        sid = str(payload.get("session_id") or "")
        # Handled before the session branch on purpose: a chat that has not sent
        # its first message yet has no session id, and falling through to the
        # global path would write "agent:<id>" into cfg.model, pointing every
        # provider request at a model name that does not exist.
        if "model" in payload and str(payload.get("model") or "").strip() == "auto":
            # "auto" is a routing mode, not a model name: same reasoning as
            # "agent:<id>" below - it must never be persisted as cfg.model.
            with app._cfg_lock:
                if sid:
                    app.session_models[sid] = "auto"
                else:
                    app.default_agent = "auto"
            self._send_json({**app.state_payload(), "auto": True, "session_only": bool(sid)})
            return
        if "model" in payload and str(payload.get("model") or "").startswith("agent:"):
            from saturday import catalog

            aid = str(payload["model"]).split(":", 1)[1][:80]
            spec = {a.id: a for a in catalog.agents()}.get(aid)
            if spec is None:
                self._send_json({"error": f"unknown agent {aid!r}"}, 400)
                return
            if not spec.available:
                self._send_json({"error": f"{aid} is not installed. {spec.install_hint}".strip()}, 400)
                return
            with app._cfg_lock:
                if sid:
                    app.session_models[sid] = "agent:" + aid
                else:
                    # no session yet: remember it for the one this chat creates
                    app.default_agent = aid
            self._send_json({**app.state_payload(), "agent": aid, "session_only": bool(sid)})
            return
        if sid and "model" in payload and set(payload.keys()) <= {"session_id", "model"}:
            # No agent: handling here. Every "agent:<id>" selection, session
            # scoped or not, is answered by the branch above and returns
            # before this point, so the copy that used to sit here could
            # never run - two places to keep in step, one of them dead. (S19)
            model = str(payload.get("model") or "").strip()[:120]
            if model:
                with app.runtimes_lock:
                    rt = app.runtimes.get(sid)
                    busy = bool(rt and rt.busy)
                if busy:
                    self._send_json({"error": "session busy - stop the run before switching models"}, 409)
                    return
                with app._cfg_lock:
                    app.session_models[sid] = model
                with app.runtimes_lock:
                    rt = app.runtimes.get(sid)
                if rt is not None and not rt.busy:
                    app._rebuild_runtime_agent(rt)
            else:
                with app._cfg_lock:
                    app.session_models.pop(sid, None)
                with app.runtimes_lock:
                    rt = app.runtimes.get(sid)
                if rt is not None and not rt.busy:
                    app._rebuild_runtime_agent(rt)
            self._send_json({"ok": True, "session_id": sid, "model": model, "session_only": True})
            return
        try:
            applied = app.apply_config({k: v for k, v in payload.items() if k != "persist"})
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        out = app.state_payload()
        out["applied"] = applied
        if app.config_persist_error:
            out["persist_error"] = app.config_persist_error
        self._send_json(out)

    def _post_rename(self, payload: dict) -> None:
        app = self.app
        sid = str(payload.get("session_id") or "")
        title = _norm(str(payload.get("title") or ""))[:120]
        if not sid or not title:
            self._send_json({"error": "session_id and title required"}, 400)
            return
        with app.runtimes_lock:
            busy_rt = app.runtimes.get(sid)
            busy = bool(busy_rt and busy_rt.busy)
        if busy:
            self._send_json({"error": "session busy - stop the run before renaming"}, 409)
            return
        p = app.store._path(sid)
        if not p.is_file():
            self._send_json({"error": "unknown session"}, 404)
            return
        # store.set_task goes through _update_meta, which holds _append_lock and
        # writes the .meta.json sidecar. Rewriting line 1 of the .jsonl by hand
        # did neither: _meta_for_path prefers the sidecar, which auto-titling
        # creates by default, so the rename was silently lost - and the unlocked
        # rewrite could drop an append landing at the same moment.
        try:
            if not app.store.set_task(sid, title):
                self._send_json({"error": "unknown session"}, 404)
                return
        except OSError as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._send_json({"ok": True, "title": title})

    def _post_projects(self, payload: dict) -> None:
        app = self.app
        name = str(payload.get("name") or "")
        try:
            proj = app.projects.create(
                name,
                instructions=str(payload.get("instructions") or ""),
                workspace=str(payload.get("workspace") or ""),
                color=payload.get("color") or "",
                files=payload.get("files"),
                scopes=payload.get("scopes"),
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        out = {"ok": True, "project": proj.to_dict()}
        out["projects"] = app.projects_payload()
        self._send_json(out)

    def _post_assign(self, payload: dict) -> None:
        app = self.app
        sid = str(payload.get("session_id") or "")
        pid = str(payload.get("project_id") or "")
        if not sid:
            self._send_json({"error": "session_id required"}, 400)
            return
        with app.runtimes_lock:
            busy_rt = app.runtimes.get(sid)
            busy = bool(busy_rt and busy_rt.busy)
        if busy:
            self._send_json({"error": "session busy - stop the run before moving it"}, 409)
            return
        if pid and app.projects.get(pid) is None:
            self._send_json({"error": "unknown project"}, 404)
            return
        if not app.store.set_project(sid, pid):
            self._send_json({"error": "unknown session"}, 404)
            return
        with app.runtimes_lock:
            rt = app.runtimes.get(sid)
            if rt is not None and not rt.busy:
                app._rebuild_runtime_agent(rt)
        self._send_json({"ok": True, "session_id": sid, "project_id": pid})

    def _post_reveal(self, payload: dict) -> None:
        app = self.app
        target = str(payload.get("target") or "")
        try:
            if target == "config":
                from saturday.config import CONFIG_DIR

                path = str(CONFIG_DIR)
            elif target == "sessions":
                path = str(app.store.root)
            elif target == "workspace":
                path = str(Path(app.base_cfg.workspace_root))
            else:
                self._send_json({"error": "unknown target"}, 400)
                return
            Path(path).mkdir(parents=True, exist_ok=True)
            _reveal_path(path)
        except OSError as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        self._send_json({"ok": True, "path": path})

    def _post_onboard(self, payload: dict) -> None:
        app = self.app
        from saturday.config import CONFIG_DIR, PROVIDERS
        from saturday.llm.probe import probe_connection

        prov = str(payload.get("provider") or "").strip()
        key = str(payload.get("api_key") or "").strip()
        model = str(payload.get("model") or "").strip()
        prof = PROVIDERS.get(prov)
        if prof is None:
            self._send_json({"error": "unknown provider"}, 400)
            return
        needs_key = prof.name not in ("ollama", "vllm")
        if needs_key and not key:
            key = prof.resolve_api_key()  # "test current key" from Settings
        if needs_key and (not key or len(key) > 4096 or "\n" in key):
            self._send_json({"error": "API key required"}, 400)
            return
        # verify before saving: a bad key must fail here, not on the first chat
        ok, detail, models = probe_connection(prof, key if needs_key else "")
        if not ok and needs_key:
            self._send_json(
                {"ok": False, "error": "connection test failed: " + detail, "models": models, "probe": detail},
                200,
            )
            return
        try:
            if key:
                _env_upsert(CONFIG_DIR / ".env", prof.api_key_env, key)
                os.environ[prof.api_key_env] = key
        except OSError as exc:
            self._send_json({"error": f"could not write .env: {exc}"}, 500)
            return
        patch: dict = {"provider": prov}
        if model:
            patch["model"] = model
        try:
            applied = app.apply_config(patch)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        out = app.state_payload()
        out["applied"] = applied
        out["models"] = models
        out["probe"] = detail
        out["probe_ok"] = ok
        if app.config_persist_error:
            out["persist_error"] = app.config_persist_error
        self._send_json(out)

    def _handle_chat(self, payload: dict) -> None:
        app = self.app
        text = str(payload.get("text") or "").strip()
        if not text:
            self._send_json({"error": "empty message"}, 400)
            return
        sid = str(payload.get("session_id") or "").strip()
        pid = str(payload.get("project_id") or "").strip()
        if pid and app.projects.get(pid) is None:
            self._send_json({"error": "unknown project"}, 400)
            return
        try:
            rt = app.runtime_for(sid) if sid else None
        except Exception as exc:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return
        if rt is None:
            newsid = app.store.create({"task": _title_from_text(text), "surface": "app", "project": pid})
            rt = app.runtime_for(newsid)
        # atomic idle->running: a second concurrent chat on this session 409s
        if not rt.try_begin_run():
            self._send_json({"error": "session busy", "session_id": rt.sid}, 409)
            return
        # From here to worker.start() everything still runs on the request
        # thread, and several steps can raise: saving an image is an unguarded
        # mkdir/write_bytes, state_payload() stats every session so a delete
        # landing now raises out of the stat, and thread creation can be
        # refused. The run was already marked RUNNING, so an escape left the
        # session busy with nobody working on it and every later chat 409ing
        # until restart. Ownership passes to the worker at handed_off; until
        # then this frame ends the run itself, against the generation it began
        # so it cannot end a run some retry has since started.
        gen = rt.run_generation
        handed_off = None
        try:
            handed_off = self._start_chat_run(app, rt, payload, text)
        except Exception as exc:
            self._fail_500(exc)
        finally:
            if handed_off is None:
                rt.finish_run(gen)
        if handed_off is None:
            return
        q, replay, hello = handed_off
        try:
            self._pump_bus(rt, q, replay=replay, first_event=hello)
        finally:
            rt.bus.unsubscribe(q)

    def _start_chat_run(self, app, rt, payload: dict, text: str):
        """The body of a chat request up to the moment the worker takes over.

        Returns the (queue, replay, hello) the caller should pump once the
        worker owns finish_run(), or None when the request answered by itself
        (slash output, a rejected image) and already ended the run.
        """
        image_paths: list[str] = []
        data_urls = payload.get("images") or []
        if data_urls:
            image_paths, err = _save_data_urls(rt.sid, [str(u) for u in data_urls])
            if err:
                rt.finish_run()
                self._send_json({"error": err}, 400)
                return None
        try:
            notices = handle_slash(rt, text)
        except Exception as exc:
            # a crashing slash command must never leave the session stuck busy
            rt.finish_run()
            q = rt.bus.subscribe()
            try:
                self._begin_stream()
                if not self._stream_line({"t": "hello", "sid": rt.sid, "slash": True, "project": rt.project_id or ""}):
                    return None
                err = {"t": "notice", "s": f"[command error] {type(exc).__name__}: {exc}"}
                rt.bus.publish(err)
                self._stream_line(err)
                self._stream_line({"t": "done", "final": "", "stop_reason": "slash", "steps": 0, "tokens": 0, "sid": rt.sid})
            finally:
                rt.bus.unsubscribe(q)
            return None
        if notices:
            rt.finish_run()
            q = rt.bus.subscribe()
            try:
                self._begin_stream()
                if not self._stream_line({"t": "hello", "sid": rt.sid, "slash": True, "project": rt.project_id or ""}):
                    return None
                for evt in notices:
                    rt.bus.publish(evt)
                    if not self._stream_line(evt):
                        return None
                self._stream_line({"t": "done", "final": "", "stop_reason": "slash", "steps": 0, "tokens": 0, "sid": rt.sid})
            finally:
                rt.bus.unsubscribe(q)
            return None
        hello_provider, hello_model = app.hello_fields()
        start_seq = rt.bus.last_seq
        # remember where this run's events begin so a re-attaching viewer
        # (user switched sessions mid-run) can replay exactly the live turn
        rt.run_start_seq = start_seq
        rt.bus.publish({"t": "user", "text": text, "images": len(image_paths) + len(rt.pending_images), "sid": rt.sid})
        # Subscribe (and snapshot the replay) BEFORE starting the worker: the
        # worker can publish its first tool_start almost immediately (no real
        # network latency against a fast/local provider), and subscribing
        # after worker.start() raced that publish against this thread reaching
        # subscribe()/replay() — landing the same event in both the replay
        # snapshot and the live queue and double-delivering it to the client.
        q, replay = rt.bus.subscribe_with_replay(start_seq)
        hello = {
            "t": "hello",
            "sid": rt.sid,
            "provider": hello_provider,
            "model": hello_model,
            "project": rt.project_id or "",
        }
        worker = threading.Thread(target=_run_chat, args=(app, rt, text, image_paths), daemon=True)
        # the last statement that can raise: after it the worker, not this
        # frame, is responsible for ending the run
        worker.start()
        return q, replay, hello


# -- routing tables ------------------------------------------------------------
# Dispatch data for Handler.do_*: literals compare exact path; Patterns
# fullmatch and unpack capture groups into the handler's signature.
_RE_SESSION = re.compile(r"/api/session/([A-Za-z0-9_.\-]+)")
_RE_PROJECT = re.compile(r"/api/project/([A-Za-z0-9_.\-]+)")
_RE_STREAM = re.compile(r"/api/stream/([A-Za-z0-9_.\-]+)")

_GET_ROUTES = [
    ("/api/state", "_get_state"),
    ("/api/trust", "_get_trust"),
    ("/api/metrics", "_get_metrics"),
    ("/api/agents", "_get_agents"),
    ("/api/remote", "_get_remote"),
    ("/api/models", "_get_models"),
    ("/api/sessions", "_get_sessions"),
    ("/api/projects", "_get_projects"),
    ("/api/export/all", "_get_export_all"),
    (_RE_SESSION, "_get_session"),
    ("/api/ws", "_ws_list"),
    ("/api/wsfile", "_ws_read"),
    ("/api/search", "_get_search"),
    ("/api/context", "_get_context"),
    ("/api/journal", "_get_journal"),
    ("/api/memgraph", "_get_memgraph"),
    ("/api/memgraph/expand", "_get_memgraph_expand"),
    ("/api/memgraph/focus", "_get_memgraph_focus"),
    ("/api/browse", "_get_browse"),
    ("/api/mcp", "_get_mcp"),
    ("/api/audit", "_get_audit"),
    ("/api/tools", "_get_tools"),
    ("/api/doctor", "_get_doctor"),
    ("/api/update", "_get_update"),
    ("/api/update/history", "_get_update_history"),
    ("/api/memory", "_get_memory"),
    ("/api/codemem", "_get_codemem"),
    ("/api/skills", "_get_skills"),
    ("/api/pipelines", "_get_pipelines"),
    ("/api/schedules", "_get_schedules"),
    ("/api/runs", "_get_runs"),
    ("/api/git/status", "_get_git_status"),
    (_RE_STREAM, "_stream_tail"),
    ("/api/file", "_send_image_file"),
]
_POST_ROUTES = {
    "/api/trigger": "_post_trigger",
    "/api/trust": "_post_trust",
    "/api/plan": "_post_plan",
    "/api/branch": "_post_branch",
    "/api/hooks": "_post_hooks",
    "/api/approvals/remove": "_post_approvals_remove",
    "/api/approve": "_post_approve",
    "/api/agents": "_post_agents",
    "/api/remote": "_post_remote",
    "/api/models": "_post_models",
    "/api/stop": "_post_stop",
    "/api/config": "_post_config",
    "/api/rename": "_post_rename",
    "/api/projects": "_post_projects",
    "/api/assign": "_post_assign",
    "/api/reveal": "_post_reveal",
    "/api/chat": "_handle_chat",
    "/api/onboard": "_post_onboard",
    "/api/journal/restore": "_post_journal_restore",
    "/api/memory/consolidate": "_post_memory_consolidate",
    "/api/skills": "_post_skills",
    "/api/codemem": "_post_codemem",
    "/api/pipelines": "_post_pipelines",
    "/api/pipelines/run": "_post_pipeline_run",
    "/api/schedules": "_post_schedules",
    "/api/archive": "_post_archive",
    "/api/commands": "_post_commands",
    "/api/feedback": "_post_feedback",
    "/api/ask": "_post_ask",
    "/api/enhance": "_post_enhance",
    "/api/suggest": "_post_suggest",
}
_DELETE_ROUTES = [
    ("/api/sessions/all", "_delete_all_sessions"),
    (_RE_SESSION, "_delete_session"),
    (_RE_PROJECT, "_delete_project"),
]


def _install_attention_sink(rt) -> None:
    """Forward what retrieval scored onto this session's event stream.

    One process-wide sink would mix sessions together, so events carry the run
    they came from and each sink keeps only its own."""
    def sink(event: dict) -> None:
        # match on the run id, not the thread: tools execute on a pool, so the
        # thread raising an event is never the one that began the run
        if event.get("run") != rt.sid:
            return
        try:
            rt.bus.publish({"t": "attn", **event})
        except Exception:
            pass

    from saturday import attention

    rt._attn_sink = sink
    attention.add_sink(sink)


def _agent_caution(name: str) -> str:
    """Any warning attached to this delegate, shown where it is chosen."""
    try:
        from saturday.tools.external_agent import all_agents

        spec = all_agents().get(name)
        return getattr(spec, "caution", "") or "" if spec else ""
    except Exception:
        return ""


def _pipeline_agent_runner(cfg_overrides: dict):
    """Seam kept so a test can swap how a node executes; the behaviour itself
    is shared with the CLI so the two cannot drift."""
    from saturday import pipeline as P

    return P.make_runner(cfg_overrides)


def _pipeline_memory_lookup():
    """Memory nodes read the same index the memory tools and CLI read."""
    def lookup(query: str) -> str:
        try:
            from saturday.memindex import MemoryIndex
            from saturday.tools.memory import memory_path

            idx = MemoryIndex()
            try:
                path = memory_path()
                idx.reindex(path.read_text(encoding="utf-8", errors="replace")
                            if path.is_file() else "", scope="global")
                hits = idx.search(query, k=5)
            finally:
                idx.close()
            return "\n".join(f"- {h['text']}" for h in hits)
        except Exception:
            return ""
    return lookup



class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Exit quietly when the listening socket is closed underneath us.

        On Windows a socket closed from another thread (app-window teardown,
        or the server object being garbage-collected while a helper thread is
        still in select()) wakes this loop with WSAENOTSOCK/EBADF instead of
        setting the shutdown flag. Unhandled, that crashes the daemon thread
        (WinError 10038 in test output); for a local server, a dead listener
        always means "stop serving", so swallow exactly those signals.
        """
        try:
            super().serve_forever(poll_interval)
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror == 10038 or exc.errno in (errno.EBADF, errno.ENOTSOCK):
                return  # listener gone; serve_forever's finally already ran
            raise

    def __init__(self, address, app: AppState, token: str = "", extra_hosts: set[str] | None = None) -> None:
        # A dedicated Handler subclass per AppServer instance: the base
        # Handler's app/token/allowed_hosts/allowed_origins are class
        # attributes, and two AppServer instances alive in the same process
        # (a slow-to-close prior instance racing a new one, routine in tests)
        # would otherwise clobber each other's injected state for any
        # in-flight request on the older server.
        handler_cls = type(f"Handler_{id(self):x}", (Handler,), {})
        super().__init__(address, handler_cls)
        from saturday.utils.httpd import allowed_hosts, allowed_origins

        self.app = app
        handler_cls.app = app
        handler_cls.token = token
        self.token = token
        bound_host, bound_port = self.server_address[:2]
        # pin Host/Origin to the bound loopback (or bind) address so a rebinding
        # domain or a hostile web page can never reach the API
        hosts = allowed_hosts(bound_host, bound_port)
        # a tunnel forwards Host: <name>.trycloudflare.com, which the loopback pin rejects
        for extra in extra_hosts or ():
            hosts.add(extra)
        handler_cls.allowed_hosts = hosts
        handler_cls.allowed_origins = allowed_origins(handler_cls.allowed_hosts)

    def allow_host(self, authority: str) -> None:
        """Widen the Host allowlist post-bind; a tunnel only knows its name after the port exists."""
        from saturday.utils.httpd import allowed_origins

        cls = self.RequestHandlerClass
        cls.allowed_hosts = set(cls.allowed_hosts) | {authority}
        cls.allowed_origins = allowed_origins(cls.allowed_hosts)


# ---------------------------------------------------------------------------
# Native app-window launcher


# ---------------------------------------------------------------------------
# Scheduled-automations watcher (Manus/Goose-style cron, in-app)


SCHEDULE_WATCHER_ON = False
_watcher_lock = threading.Lock()


def start_schedule_watcher(app: "AppState") -> None:
    """Poll ~/.saturday/schedules.json and fire due entries as one-shot runs.

    Started lazily by serve() and whenever a schedule is added from the UI
    (opt out with SATURDAY_SCHEDULE_WATCHER=0). The CLI keeps its own
    `saturday schedule watch` foreground loop; this one serves the app."""
    global SCHEDULE_WATCHER_ON
    if os.environ.get("SATURDAY_SCHEDULE_WATCHER", "").strip() == "0":
        return
    with _watcher_lock:
        if SCHEDULE_WATCHER_ON:
            return

        def _loop() -> None:
            from saturday.schedule import ScheduleStore, _fire_and_log, default_schedules_path

            store = ScheduleStore()
            log_dir = default_schedules_path().parent / "logs"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            while True:
                try:
                    for s in store.due():
                        _fire_and_log(store, s, log_dir)
                except Exception:
                    pass  # a bad poll must never take the app down
                time.sleep(20.0)

        threading.Thread(target=_loop, daemon=True, name="saturday-schedules").start()
        SCHEDULE_WATCHER_ON = True


def find_app_browser() -> str | None:
    exe = shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")
    if exe:
        return exe
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LocalAppData", "")
    candidates = [
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    if lad:
        candidates.append(Path(lad) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def launch_app_window(url: str, width: int = 1220, height: int = 840) -> str | None:
    """Open url in a chromeless app window; falls back to the default browser.

    Uses its own --user-data-dir: without one, --app= against a browser
    that's already running the user's regular profile gets redirected
    through Chromium's single-instance IPC and opens as an ordinary tab in
    their existing session instead of a separate window - the profile,
    not the flag, is what Chromium keys the single-instance check on."""
    exe = find_app_browser()
    if exe is None:
        webbrowser.open(url)
        return None
    from saturday.config import get_config_dir

    profile_dir = get_config_dir() / "app-browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        exe,
        f"--app={url}",
        f"--window-size={width},{height}",
        f"--user-data-dir={profile_dir}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    subprocess.Popen(argv, creationflags=creationflags, close_fds=True)
    return exe


class _WindowControls:
    """JS bridge for the custom title bar (exposed via pywebview's js_api)."""

    def __init__(self, win=None) -> None:
        self._win = win
        self._maximized = False

    def win_min(self) -> bool:
        self._win.minimize()
        return True

    def win_max(self) -> bool:
        if self._maximized:
            self._win.restore()
        else:
            self._win.maximize()
        self._maximized = not self._maximized
        return self._maximized

    def win_close(self) -> bool:
        self._win.destroy()
        return True


def embedded_window_hint() -> str:
    """One actionable line telling the user how to get the embedded window."""
    if sys.platform == "darwin":
        return "install pyobjc:  uv tool install --force --with 'pywebview[cocoa]' saturday"
    if os.name == "nt":
        return "install the WebView2 runtime from microsoft.com/edge/webview2"
    return "add a GUI backend:  uv tool install --force --with 'pywebview[qt]' saturday"


def _patch_qt_permission_policy() -> None:
    """Stop the Qt backend aborting the app on any permission request.

    pywebview 6.2 answers featurePermissionRequested with a raw int, and PyQt6
    6.11 requires the PermissionPolicy enum, so the whole process dies with a
    TypeError the moment a page asks for notifications or the clipboard.
    Coercing the int back to the enum is upstream's own intent."""
    try:
        from PyQt6.QtWebEngineCore import QWebEnginePage

        import webview.platforms.qt as qt_backend
    except Exception:
        return  # not the Qt backend, or not installed: nothing to patch

    policy = QWebEnginePage.PermissionPolicy
    page = getattr(getattr(qt_backend, "BrowserView", None), "WebPage", None)
    if page is None or getattr(page, "_saturday_policy_patch", False):
        return

    def on_feature_permission_requested(self, url, feature) -> None:
        media = (QWebEnginePage.Feature.MediaAudioCapture,
                 QWebEnginePage.Feature.MediaVideoCapture,
                 QWebEnginePage.Feature.MediaAudioVideoCapture)
        allow = policy.PermissionGrantedByUser if feature in media else policy.PermissionDeniedByUser
        try:
            self.setFeaturePermission(url, feature, allow)
        except Exception:
            pass  # a refused permission must never take the window down

    try:
        page.onFeaturePermissionRequested = on_feature_permission_requested
        page._saturday_policy_patch = True
    except Exception:
        pass


def launch_embedded_window(url: str, width: int, height: int) -> bool:
    """Frameless embedded window with a custom title bar (pywebview/WebView2).

    Blocks until the window closes. Returns False when pywebview is missing or
    anything fails, so callers keep the browser-window fallback — the desktop
    app must never refuse to start just because an optional canvas is absent.
    """
    try:
        import webview
    except Exception as exc:
        print(f"[saturday] embedded window unavailable ({type(exc).__name__})", flush=True)
        return False
    if not os.environ.get("SATURDAY_DEBUG"):
        # pywebview probes every backend in turn and logs a full traceback for
        # each one it cannot import. Missing backends are the normal case on a
        # machine that has one of them, so the tracebacks are noise, not news;
        # the single hint line below is what the user can actually act on.
        import logging

        logging.getLogger("pywebview").setLevel(logging.CRITICAL)
    _patch_qt_permission_policy()
    try:
        controls = _WindowControls()
        win = webview.create_window(
            "Saturday",
            url,
            width=width,
            height=height,
            frameless=True,
            easy_drag=False,  # dragging is the title bar's drag-region, not the whole page
            resizable=True,
            js_api=controls,
        )
        controls._win = win
        webview.start()
    except Exception as exc:
        print(f"[saturday] no embedded window ({type(exc).__name__}); "
              f"using a browser window instead.", flush=True)
        print(f"[saturday] to embed it, {embedded_window_hint()}", flush=True)
        if os.environ.get("SATURDAY_DEBUG"):
            import traceback

            traceback.print_exc()
        return False
    return True


# ---------------------------------------------------------------------------
# Entry point


def _port_in_use(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "") else host, port)) == 0
    finally:
        probe.close()


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    open_window: bool = True,
    width: int = 1220,
    height: int = 840,
    token: str | None = None,
    cfg_overrides: dict | None = None,
    env_path: str | None = None,
    tunnel_provider: str | None = None,
) -> int:
    from saturday.utils.env import load_env_file

    if _port_in_use(host, port):
        # Windows SO_REUSEADDR would let a second listener bind silently and the
        # browser window would then talk to whichever process won the race â€”
        # typically a STALE server running old code. Refuse loudly instead.
        message = (
            f"port {port} is already serving - another Saturday app instance is likely "
            f"running (possibly a stale one). Close it and relaunch, or use --port <other>."
        )
        print(f"Saturday app :: REFUSED: {message}", flush=True)
        if open_window:
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(0, f"Saturday did not start.\n\n{message}", "Saturday", 0x30)
            except Exception:
                pass
        return 2
    if env_path:
        # Explicit --env: user-directed, always trusted — load immediately.
        load_env_file(env_path)
        pending = []
    else:
        # Implicit CWD .env: check for untrusted items before loading.
        # If any exist the browser will show the trust modal; if already trusted
        # (or the global override is set) load_env_file handles it as normal.
        from saturday.utils.trust import pending_trust_items
        pending = pending_trust_items()
        if not pending:
            # Nothing pending: already trusted or no project files present.
            load_env_file(None)
    app = AppState(cfg_overrides=cfg_overrides)
    app.pending_trust = pending
    start_schedule_watcher(app)
    if token is None:
        token = secrets.token_hex(16)
    try:
        srv = AppServer((host, port), app, token=token)
    except OSError as exc:
        # authoritative check: the probe above can race with listen backlogs
        print(f"Saturday app :: REFUSED: port {port} could not be bound ({exc}) - "
              f"another instance is likely running; close it or use --port <other>.", flush=True)
        if open_window:
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0, f"Saturday did not start.\n\nPort {port} is already in use - "
                       f"another instance is likely running. Close it and relaunch, or use --port <other>.",
                    "Saturday", 0x30)
            except Exception:
                pass
        return 2
    bound_host, bound_port = srv.server_address[:2]
    display_host = "127.0.0.1" if bound_host in ("0.0.0.0", "") else bound_host
    url = f"http://{display_host}:{bound_port}/"
    auth_qs = f"?k={token}" if token else ""
    print(f"Saturday app  ::  {url}", flush=True)

    tunnel = None
    if tunnel_provider:
        from saturday import remote as _remote

        try:
            tunnel = _remote.start_tunnel(tunnel_provider, bound_port)
        except RuntimeError as exc:
            print(f"Saturday app :: REFUSED: {exc}", flush=True)
            srv.server_close()
            return 2
        srv.allow_host(tunnel.host)
        remote_url = f"{tunnel.url}/{auth_qs}"
        print(f"remote        ::  {remote_url}", flush=True)
        print(f"tunnel        ::  {tunnel.provider}"
              + ("  (TLS terminates at the provider)" if tunnel.provider == "cloudflared" else ""),
              flush=True)
        for line in _remote.qr_lines(remote_url):
            print(line, flush=True)
    if not token:
        # parity with `saturday serve`'s --no-token warning: this endpoint can
        # drive a full-capability agent, so an open bind must be a visible choice
        print("WARNING        : auth disabled (--no-token); anyone able to reach "
              "this port can run commands on this machine.", flush=True)
    print(f"workspace      ::  {app.base_cfg.workspace_root}", flush=True)
    print(f"provider/model ::  {app.base_cfg.provider} / {app.base_cfg.model}", flush=True)
    if open_window:
        # the embedded window's event loop blocks, so the HTTP server runs in a
        # helper thread for that mode; the browser fallback serves in-line
        server_thread = threading.Thread(target=srv.serve_forever, daemon=True, name="saturday-http")
        server_thread.start()
        if launch_embedded_window(url + auth_qs, width=width, height=height):
            print("window         ::  embedded (custom title bar)", flush=True)
            print("Ctrl-C or the close button stops the app.", flush=True)
            srv.shutdown()
            srv.server_close()
            return 0
        srv.shutdown()  # stop the helper thread; the main loop below takes over
        exe = launch_app_window(url + auth_qs, width=width, height=height)
        print(f"window         ::  {(Path(exe).stem if exe else 'default browser')}", flush=True)
    else:
        print(f"open           ::  {url}{auth_qs}", flush=True)
    print("Ctrl-C stops the app.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel is not None:
            tunnel.close()
        srv.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(serve())
