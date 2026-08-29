"""Shared slash-command registry for both agent surfaces.

Single source of truth for every "/" command, consumed by:
  - the terminal REPL      (repl.Repl.dispatch)
  - the web chat surface   (webui.handle_slash)

Refactoring contract: this extraction is behavior-preserving. Wherever the two
surfaces historically diverged, the divergence is PRESERVED and marked with a
``WEB-DIVERGES`` comment (or implemented as separate per-surface handlers on
the command entry) rather than silently reconciled. Colors passed to
``SlashContext.out`` apply only on the REPL surface; web notices are plain
strings, exactly as before.

Handlers must not import saturday.repl or saturday.webui at module level;
domain imports (journal, usage, safety, ...) stay lazy inside handlers, same
as in the dispatchers this module replaces.
"""

from __future__ import annotations

import time

# SINGLE SOURCE OF TRUTH for the chat "/" autocomplete. The frontend used to
# keep its own hardcoded copy and drifted (new commands worked but never
# showed up). This list is served via /api/state -> info.slash_commands;
# SLASH_ALIASES is derived from it so they can't diverge again.
# Order/description strings are served to the browser verbatim: do not reword.
SLASH_COMMAND_LIST = [
    ["/help", "show available commands"],
    ["/tools", "list registered tools"],
    ["/sessions", "list saved sessions"],
    ["/model", "show or switch model"],
    ["/compact", "compact older context"],
    ["/todo", "show current plan"],
    ["/memory", "show pinned working memory"],
    ["/reset", "clear working memory + context"],
    ["/attach", "queue an image path"],
    ["/images", "list queued images"],
    ["/context", "show context-window breakdown"],
    ["/plan", "toggle plan mode (read-only)"],
    ["/revert", "undo a journaled file edit"],
    ["/rewind", "roll files back to checkpoint state"],
    ["/toggle", "enable/disable tools for this session"],
    ["/metrics", "usage metrics (turns, tokens, outcomes)"],
    ["/branch", "fork this conversation"],
    ["/yolo", "toggle fully-autonomous mode"],
    ["/jobs", "list background jobs (status/output)"],
    ["/goals", "show the active session goal"],
    ["/skills", "list learned, reusable skills"],
]
SLASH_ALIASES = {name: name[1:] for name, _ in SLASH_COMMAND_LIST}

# Shared help text (moved here from repl.py: the web surface renders /help
# from this registry too, and importing it from the REPL surface inverted the
# layering). repl.py re-exports it for compatibility.
HELP_TEXT = """commands:
  /help                 this list
  /tools                list registered tools
  /sessions             list saved sessions
  /model [name]         show or switch model
  /compact              collapse older turns into a summary note
  /todo                 show current todo/plan state
  /memory               show pinned working memory
  /reset                clear working memory + rolling context
  /attach <image>       queue an image for the next message
  /images               list queued images
  /context              show context-window breakdown (tokens by section)
  /metrics              usage + completion-health metrics (local, 14 days)
  /plan                 toggle PLAN MODE (read-only tools; agent outputs a plan only)
  /yolo                 toggle FULLY AUTONOMOUS: no approval prompts (hardline + deny rules still block)
  /rewind [n]           roll FILES back to the last checkpoint (or undo n newest edits); conversation untouched
  /jobs                 list background jobs started via shell run_in_background
  /goals                show the active session goal and its status
  /skills               list learned, reusable skills (agent saves these itself)
  /revert [n]           list recent file edits, or restore the n-th (0 = latest)
  /branch [n]           fork this conversation into a new session (first n messages)
  /toggle <name|family> enable/disable a tool for this session (families: web, browser, computer_use, shell, python, file_writes, subagents, memory)
  exit / quit           leave (Ctrl-C also works)
tips: end a line with \\ to continue on the next line; Up-arrow recalls history."""

# Web-surface /help: one 'command — description' line per entry (see _cmd_help).
# The web composer renders these as an aligned command grid; the em-dash
# separator is the parse contract with the frontend.
WEB_HELP_TEXT = """commands:
  /help — this list
  /tools — list registered tools
  /sessions — list saved sessions
  /model [name] — show or switch model
  /compact — collapse older turns into a summary note
  /todo — show current todo/plan state
  /memory — show pinned working memory
  /reset — clear working memory + rolling context
  /attach <image> — queue an image for the next message
  /images — list queued images
  /context — show context-window breakdown (tokens by section)
  /metrics — usage + completion-health metrics (local, 14 days)
  /plan — toggle PLAN MODE (read-only tools; agent outputs a plan only)
  /yolo — toggle FULLY AUTONOMOUS: no approval prompts (hardline + deny rules still block)
  /rewind [n] — roll FILES back to the last checkpoint (or undo n newest edits); conversation untouched
  /jobs — list background jobs started via shell run_in_background
  /goals — show the active session goal and its status
  /skills — list learned, reusable skills (agent saves these itself)
  /revert [n] — list recent file edits, or restore the n-th (0 = latest)
  /branch [n] — fork this conversation into a new session (first n messages)
  /toggle <name|family> — enable/disable a tool for this session (families: web, browser, computer_use, shell, python, file_writes, subagents, memory)
  exit / quit — leave
tips: type / in the composer for command autocomplete."""


class SlashContext:
    """Adapter giving one handler shape to both surfaces.

    ``out(text, color=None)`` appends an output line; ``color`` applies only
    on the REPL surface (web notices are plain). Session objects are exposed
    as lazy properties: some tests drive dispatch() with bare ``Repl.__new__``
    instances carrying only an ``_output`` stub, so constructors must not
    touch attributes a given command never reads.
    """

    __slots__ = ("is_web", "repl", "rt", "lines")

    def __init__(self, *, is_web: bool, repl=None, rt=None):
        self.is_web = is_web
        self.repl = repl  # Repl instance (terminal only)
        self.rt = rt  # SessionRuntime instance (web only)
        self.lines: list[str] = []

    @classmethod
    def for_repl(cls, repl) -> "SlashContext":
        return cls(is_web=False, repl=repl)

    @classmethod
    def for_runtime(cls, rt) -> "SlashContext":
        return cls(is_web=True, rt=rt)

    # -- surface objects (lazy; see class docstring) ---------------------------
    @property
    def agent(self):
        return self.rt.agent if self.is_web else self.repl.agent

    @property
    def store(self):
        return self.rt.store if self.is_web else self.repl.store

    @property
    def app(self):
        return getattr(self.rt, "app", None) if self.is_web else None

    @property
    def _pending(self) -> list[str]:
        return self.rt.pending_images if self.is_web else self.repl.pending_images

    @property
    def checkpoint_store(self):
        """Store used for checkpoint reads/writes. The REPL always read these
        through agent.session_store (never the injectable repl.store); the web
        runtime's rt.store IS agent.session_store. Kept distinct from
        ``store`` (used by /sessions, /branch) to preserve that exactly."""
        return self.rt.store if self.is_web else self.agent.session_store

    # -- output ----------------------------------------------------------------
    def out(self, text: str, color: str | None = None) -> None:
        if color is None or self.is_web:
            self.lines.append(text)
        else:
            from saturday.ui import paint

            self.lines.append(paint(text, color))

    # -- session identity (surface policies differ by design) -------------------
    def sid(self) -> str:
        """Context sid for checkpoint reads/writes."""
        if self.is_web:
            return self.rt.sid
        return getattr(self.repl, "_sid", "") or ""

    def branch_sid(self) -> str:
        """/branch source sid. WEB-DIVERGES: the runtime always has one."""
        if self.is_web:
            return self.rt.sid
        return getattr(self.repl, "_sid", "") or getattr(self.repl, "resumed_id", "") or ""

    def ckpt_sid_or_none(self):
        """Checkpoint-file target sid; falsy means 'no live session'."""
        if self.is_web:
            return self.rt.sid
        return getattr(self.repl, "_sid", None)

    # -- rolling summary note -----------------------------------------------------
    def note(self) -> list[str]:
        if self.is_web:
            return list(getattr(self.rt, "history_note", None) or [])
        return list(getattr(self.repl, "history_note", None) or [])

    def set_note(self, value: list[str]) -> None:
        if self.is_web:
            self.rt.history_note = value
        else:
            self.repl.history_note = value

    def clear_note_inplace(self) -> None:
        if self.is_web:
            self.rt.history_note = []  # parity with repl /reset: note is ephemeral too
        else:
            self.repl.history_note.clear()

    def file_gate_auto_approve(self, value: bool) -> None:
        """Live mode switch: gates captured auto_approve at construction."""
        if self.is_web:
            self.rt.file_gate.auto_approve = value
        else:
            self.repl.file_gate.auto_approve = value

    def load_checkpoint_history(self) -> list[dict]:
        if self.is_web:
            return self.store.load_checkpoint(self.rt.sid) or []
        history: list[dict] = []
        if getattr(self.repl, "_sid", None):
            try:
                history = self.checkpoint_store.load_checkpoint(self.repl._sid) or []
            except Exception:
                history = []
        return history

    # -- registries -----------------------------------------------------------------
    def tool_for(self, name: str):
        """Jobs/goals/skills lookup. WEB-DIVERGES: pre-extraction, the REPL
        read effective_registry() while the web read _build_registry(); both
        choices are preserved instead of being silently unified."""
        reg = self.agent.effective_registry() if not self.is_web else self.agent._build_registry()
        return reg.get(name)


class SlashCommand:
    __slots__ = ("name", "desc", "run_repl", "run_web", "web_event")

    def __init__(self, name, desc, run, run_web=None, web_event=None):
        self.name = name  # "/help" style key
        self.desc = desc  # short description served via /api/state
        self.run_repl = run
        self.run_web = run_web or run
        self.web_event = web_event  # optional callable(ctx)->dict, web only


# ---------------------------------------------------------------------------
# commands


def _cmd_help(ctx, arg):
    # the web surface renders notices in a narrow chat column, where a wide
    # two-column ASCII table wraps into rubble — emit one 'cmd — desc' line
    # per command there (the frontend upgrades it to a structured grid)
    ctx.out(WEB_HELP_TEXT if getattr(ctx, "is_web", False) else HELP_TEXT)


def _cmd_tools(ctx, arg):
    names = ctx.agent.effective_registry().names()
    disabled = sorted(ctx.agent.disabled_tools)
    ctx.out(f"{len(names)} tools active: " + ", ".join(names), "dim")
    if disabled:
        ctx.out(f"disabled this session: {', '.join(disabled)}", "dim")


def _cmd_sessions(ctx, arg):
    rows = ctx.store.list_sessions()
    if not rows:
        ctx.out("(no sessions yet)")
    for r in rows:
        line = f"  {r['id']}  {r['task'] or '(interactive)'}"
        if not ctx.is_web:  # WEB-DIVERGES: terminal also shows the store file
            line += f"  [{r['file']}]"
        ctx.out(line)


def _cmd_model_repl(ctx, arg):
    # WEB-DIVERGES: persistence strategy differs (direct cfg write + best-effort
    # save_config vs the web's apply_config which reaches runtime clones too).
    if arg:
        ctx.agent.cfg.model = arg
        try:
            from saturday.config import save_config

            save_config({"model": arg})
        except OSError:
            pass  # persistence is best-effort in the REPL surface
        ctx.out(f"model -> {arg} (saved)")
    profile = ctx.agent.cfg.profile()
    ctx.out(f"provider={ctx.agent.cfg.provider} model={ctx.agent.cfg.model} base_url={profile.resolve_base_url()}")


def _cmd_model_web(ctx, arg):
    if arg:
        # route through apply_config so the change persists and reaches all
        # runtime clones (a bare cfg write would silently diverge)
        try:
            if ctx.app is not None:
                ctx.app.apply_config({"model": arg})
            else:
                ctx.agent.cfg.model = arg
        except ValueError as exc:
            ctx.out(f"[model error] {exc}")
    profile = ctx.agent.cfg.profile()
    ctx.out(f"provider={ctx.agent.cfg.provider} model={ctx.agent.cfg.model} base_url={profile.resolve_base_url()}")


def _model_event(ctx):
    return {"t": "config", "provider": ctx.agent.cfg.provider, "model": ctx.agent.cfg.model}


def _cmd_compact_repl(ctx, arg):
    n = len(ctx.note())
    keep = ctx.note()[-6:]
    dropped = n - len(keep)
    ctx.set_note(["[earlier conversation compacted: %d turns]" % dropped] + keep if dropped else keep)
    ctx.out(f"[compacted {dropped} older turn(s); {len(ctx.note())} kept]")


def _cmd_compact_web(ctx, arg):
    # DIVERGENCE FIX: this used to REWRITE the persisted checkpoint down to
    # its last 8 messages — permanent context loss the REPL never does (its
    # /compact only folds an ephemeral history_note). Mirror the REPL:
    # checkpoint stays full (next-turn seeding is untouched) and older
    # turns are summarized into the history_note, display/context sugar
    # that _run_chat injects into the next prompt like repl.py does.
    msgs = ctx.store.load_checkpoint(ctx.sid()) or []
    note = list(getattr(ctx.rt, "history_note", None) or [])
    keep = note[-6:]
    dropped_notes = len(note) - len(keep)
    if dropped_notes > 0:
        note = ["[earlier conversation compacted: %d turns]" % dropped_notes] + keep
    if len(msgs) > 8:
        for m in msgs[:-8]:
            role = str(m.get("role") or "?") if isinstance(m, dict) else "?"
            content = str(m.get("content") or "") if isinstance(m, dict) else str(m)
            snippet = " ".join(content.split())[:200]
            if snippet:
                note.append(f"{role}: {snippet}")
        ctx.out(f"[compacted: summarized {len(msgs) - 8} older message(s) into the session note; full history kept]")
    else:
        ctx.out(f"[nothing to compact ({len(msgs)} checkpoint messages)]")
    ctx.rt.history_note = note


def _cmd_todo(ctx, arg):
    reg = ctx.agent._build_registry()
    plan = getattr(reg.get("todo"), "plan", None)
    ctx.out(plan.render() if plan is not None else "(todo tool not loaded)")


def _cmd_memory(ctx, arg):
    mem = getattr(ctx.agent, "memory", None)
    ctx.out(mem.render() if mem is not None else "(memory unavailable)")


def _cmd_reset_repl(ctx, arg):
    from saturday.agent.memory import WorkingMemory

    ctx.agent.memory = WorkingMemory(max_chars=getattr(ctx.agent.cfg, "memory_max_chars", 12_000))
    ctx.clear_note_inplace()
    # WEB-DIVERGES: the REPL additionally resets seeded startup context and
    # only deletes a checkpoint when a session id exists at all
    ctx.repl.initial_history = None
    sid = ctx.ckpt_sid_or_none()
    if sid:
        ckpt = ctx.checkpoint_store._path(sid).with_suffix(".checkpoint.json")
        try:
            ckpt.unlink(missing_ok=True)
        except OSError:
            pass
    ctx.out("[memory cleared]")


def _cmd_reset_web(ctx, arg):
    from saturday.agent.memory import WorkingMemory

    ctx.agent.memory = WorkingMemory(max_chars=getattr(ctx.agent.cfg, "memory_max_chars", 12_000))
    ckpt = ctx.store._path(ctx.rt.sid).with_suffix(".checkpoint.json")
    try:
        ckpt.unlink(missing_ok=True)
    except OSError:
        pass
    ctx.clear_note_inplace()
    ctx.out("[memory cleared]")


def _cmd_attach(ctx, arg):
    if arg:
        ctx._pending.append(arg)
        ctx.out(f"[queued {len(ctx._pending)} image(s)]")
    else:
        ctx.out("usage: /attach <image-path>")


def _cmd_images(ctx, arg):
    ctx.out("\n".join(ctx._pending) or "(no queued images)")


def _cmd_plan(ctx, arg):
    ctx.agent.plan_mode = not ctx.agent.plan_mode
    state = "ON - read-only tools, the agent will produce a plan only" if ctx.agent.plan_mode else "OFF - full capability"
    ctx.out(f"[plan mode {state}]", "cyan")


def _cmd_toggle(ctx, arg):
    ok, msg, _ = ctx.agent.toggle_tool(arg)
    ctx.out(f"[toggle] {msg}" if ok else f"[toggle error] {msg}", "cyan" if ok else "red")


def _cmd_revert(ctx, arg):
    from saturday.tools import journal

    root = getattr(ctx.agent.cfg, "workspace_root", None) or "."
    if arg.isdigit():
        ok, msg = journal.restore_entry(root, int(arg))
        ctx.out(("[revert] " if ok else "[revert failed] ") + msg, "green" if ok else "red")
    else:
        entries = journal.load_entries(root, limit=10)
        if not entries:
            ctx.out("(no journaled file edits in this workspace)")
        for i, e in enumerate(entries):
            ts = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            marker = ""
            if not ctx.is_web:  # WEB-DIVERGES: terminal marks partial restores
                marker = " (truncated)" if e.get("before_truncated") else " (full)"
            ctx.out(f"  [{i}] {ts} {e['tool']} {e['path']}{marker}", "dim")
        ctx.out("restore with /revert <n>")


def _cmd_branch_repl(ctx, arg):
    keep = int(arg) if arg.isdigit() else None
    sid = ctx.branch_sid()
    new_sid = ctx.store.branch(sid, keep) if sid else None
    ctx.out(
        f"[branched -> {new_sid}] resume it with: saturday chat --resume {new_sid}" if new_sid
        else "[no active session id to branch]",
        "cyan",
    )


def _cmd_branch_web(ctx, arg):
    keep = int(arg) if arg.isdigit() else None
    new_sid = ctx.store.branch(ctx.rt.sid, keep)
    ctx.out(f"[branched -> {new_sid}] continue in that chat from the sidebar" if new_sid
            else "[branch failed: nothing to fork]")


def _cmd_jobs(ctx, arg):
    tool = ctx.tool_for("job_list")
    if tool is None:
        ctx.out("(job tools not loaded)")
    else:
        ok, s = tool.run({})
        ctx.out(s if ok else f"[jobs error] {s}", None if ok else "red")


def _cmd_goals(ctx, arg):
    tool = ctx.tool_for("get_goal")
    ctx.out(tool.run({})[1] if tool is not None else "(goal tools not loaded)")


def _cmd_skills(ctx, arg):
    tool = ctx.tool_for("skills_index")
    ctx.out(tool.run({})[1] if tool is not None else "(no skills saved yet)")


def _cmd_yolo(ctx, arg):
    from saturday.safety import AUTONOMOUS_MODE, ApprovalPolicy, is_autonomous

    # per-agent override (NOT cfg.safety_mode): web sessions share one base
    # cfg, so writing cfg here would silently flip every concurrent session
    if is_autonomous(ctx.agent.safety_mode):
        ctx.agent.safety_mode = "ask"
        ctx.agent.approval_policy = ApprovalPolicy.from_mode("ask")
        ctx.file_gate_auto_approve(False)
        ctx.out("[yolo OFF - approvals restored (ask mode)]", "cyan")
    else:
        ctx.agent.safety_mode = AUTONOMOUS_MODE
        ctx.agent.approval_policy = ApprovalPolicy.from_mode(AUTONOMOUS_MODE)
        ctx.file_gate_auto_approve(True)
        ctx.out(
            "[yolo ON - fully autonomous: no approval prompts. Hardline blocks and deny rules still apply.]",
            "yellow",
        )


def _yolo_event(ctx):
    # badge/state must reflect the flip without a manual refresh
    return {
        "t": "config",
        "provider": ctx.agent.cfg.provider,
        "model": ctx.agent.cfg.model,
        "safety_mode": ctx.agent.safety_mode,
    }


def _cmd_rewind(ctx, arg):
    # Cursor-style: roll FILES back to a checkpoint state. Without an
    # arg, target = the latest conversation checkpoint's journal
    # position; /rewind <i> undoes journal entries 0..i (newest-first,
    # same indexing as /revert). Conversation history is untouched.
    from saturday.tools import journal

    root = getattr(ctx.agent.cfg, "workspace_root", None) or "."
    total = journal.journal_length(root)
    if arg.isdigit():
        target = max(0, total - int(arg) - 1)
    else:
        meta = ctx.checkpoint_store.load_checkpoint_meta(ctx.sid()) or {}
        if meta.get("journal_len") is None:
            ctx.out("(no checkpoint metadata yet — run something first, or use /rewind <n>)")
            return
        target = int(meta["journal_len"])
    ok, msg = journal.restore_to_length(root, target)
    ctx.out(("[rewind] " if ok else "[rewind failed] ") + msg, "green" if ok else "red")


def _cmd_context(ctx, arg):
    from saturday.context import render_text

    history = ctx.load_checkpoint_history()
    try:
        bd = ctx.agent.context_breakdown(history)
    except Exception as exc:
        ctx.out(f"[context error] {type(exc).__name__}: {exc}", "red")
    else:
        ctx.out(render_text(bd), "dim")


def _cmd_metrics(ctx, arg):
    from saturday.usage import render_metrics_text

    ctx.out(render_metrics_text(), "dim")


_COMMANDS = [
    SlashCommand("/help", "show available commands", _cmd_help),
    SlashCommand("/tools", "list registered tools", _cmd_tools),
    SlashCommand("/sessions", "list saved sessions", _cmd_sessions),
    SlashCommand("/model", "show or switch model", _cmd_model_repl, run_web=_cmd_model_web, web_event=_model_event),
    SlashCommand("/compact", "compact older context", _cmd_compact_repl, run_web=_cmd_compact_web),
    SlashCommand("/todo", "show current plan", _cmd_todo),
    SlashCommand("/memory", "show pinned working memory", _cmd_memory),
    SlashCommand("/reset", "clear working memory + context", _cmd_reset_repl, run_web=_cmd_reset_web),
    SlashCommand("/attach", "queue an image path", _cmd_attach),
    SlashCommand("/images", "list queued images", _cmd_images),
    SlashCommand("/context", "show context-window breakdown", _cmd_context),
    SlashCommand("/plan", "toggle plan mode (read-only)", _cmd_plan),
    SlashCommand("/revert", "undo a journaled file edit", _cmd_revert),
    SlashCommand("/rewind", "roll files back to checkpoint state", _cmd_rewind),
    SlashCommand("/toggle", "enable/disable tools for this session", _cmd_toggle),
    SlashCommand("/metrics", "usage metrics (turns, tokens, outcomes)", _cmd_metrics),
    SlashCommand("/branch", "fork this conversation", _cmd_branch_repl, run_web=_cmd_branch_web),
    SlashCommand("/yolo", "toggle fully-autonomous mode", _cmd_yolo, web_event=_yolo_event),
    SlashCommand("/jobs", "list background jobs (status/output)", _cmd_jobs),
    SlashCommand("/goals", "show the active session goal", _cmd_goals),
    SlashCommand("/skills", "list learned, reusable skills", _cmd_skills),
]

COMMANDS: dict[str, SlashCommand] = {sc.name: sc for sc in _COMMANDS}

assert len(COMMANDS) == len(SLASH_COMMAND_LIST) == len({n for n, _ in SLASH_COMMAND_LIST})
for _sc in COMMANDS.values():
    assert [_sc.name, _sc.desc] in SLASH_COMMAND_LIST
