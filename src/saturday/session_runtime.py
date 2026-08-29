"""Per-session runtime machinery for the desktop app surface.

Extracted from webui.py (design review: transport, domain logic and session
lifecycle were one god-module). This module owns:

- ``RunStopped`` / ``WebApprover`` / ``WebFileGate`` — browser-side approval
  bridge (mirror of repl.ConsoleApprover)
- ``EventBus`` — bounded event buffer + fan-out with monotonic sequence numbers
- ``SessionRuntime`` — one live session: agent handle, event bus, and an
  EXPLICIT run-state machine.

## Run-state discipline (the production fix)

Concurrency bugs in this surface were historically caused by ad-hoc
``busy``/``stop_flag`` booleans mutated from worker threads and HTTP handlers.
The rules are now structural:

1. ``busy`` is derived from ``phase`` ("idle" | "running"); it is never written
   directly by callers.
2. A run starts only via ``try_begin_run()`` — an atomic idle->running
   transition that also bumps ``run_generation`` (monotonic counter, NEVER
   reset: stale unwinds from a previous generation can't affect newer turns,
   cf. hermes gateway/session_state.py).
3. A run ends only via ``finish_run()`` — and the worker MUST call it BEFORE
   publishing its terminal bus event. Stream pumps exit on done/error only when
   the runtime is already idle again, so publishing first would leave clients
   hanging on the stream (this exact race shipped once).
4. ``request_stop()`` only flips ``stop_requested``; the worker observes it via
   callbacks and performs the transition itself.

Stdlib-only, like the rest of the core.
"""
from __future__ import annotations

import copy
import itertools
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from queue import Queue

from saturday.editing import _norm, norm  # noqa: F401  (_norm re-exported for compat)
from saturday.safety import is_autonomous

APPROVAL_TTL_SOURCE = "SATURDAY_APPROVAL_TTL"


def _ttl_default() -> float:
    import os

    return float(os.environ.get(APPROVAL_TTL_SOURCE, "600"))


class RunStopped(Exception):
    """Raised inside agent callbacks when the user presses Stop."""


class WebApprover:
    """policy.approver implementation that asks the browser instead of a console.

    Session-scoped allow/deny memory like the console approver; decisions arrive
    asynchronously via POST /api/approve. Fail-closed: timeout/stop => deny.
    ``scope`` namespaces pending ids per session so two runtimes can never
    resolve each other's approvals."""

    def __init__(self, publish: Callable[[dict], None], ttl: float | None = None, scope: str = "") -> None:
        self.allowed_commands: set[str] = set()
        self.denied_commands: set[str] = set()
        self.allowed_paths: set[str] = set()
        self.ttl = float(ttl) if ttl is not None else _ttl_default()
        self._publish = publish
        self._scope = str(scope or "")
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self._denial_note = ""  # optional user note attached to the latest deny

    # -- policy.approver interface -------------------------------------------------
    def __call__(self, command: str, reason: str) -> bool:
        key = _norm(command)
        if key in self.allowed_commands:
            return True
        if key in self.denied_commands:
            self._publish({"t": "notice", "s": f"[auto-denied (session rule)] {reason}"})
            return False
        # publish the RAW text: the dialog must show the command the agent
        # actually runs (multiline included), not a folded one-line summary
        return self._ask(kind="command", title=reason, command=command, remember_key=key)

    # -- file gate -----------------------------------------------------------------
    def ask_file(self, tool_name: str, path: str, diff: str | None, body_chars: int) -> bool:
        key = _norm(path)
        detail = diff if diff else f"({body_chars} chars)"
        return self._ask(kind="file", title=f"{tool_name} -> {path}", command="", diff=diff, detail=detail, remember_key=key)

    # -- plumbing ------------------------------------------------------------------
    def _persist_rule(self, command: str) -> None:
        """Write an 'always allow' rule through to approvals.json (opt-out via
        cfg.persist_approvals=False). Compound commands are refused by the
        store's matcher anyway; keep them session-only."""
        persist = getattr(getattr(self, "agent", None), "cfg", None)
        if persist is not None and not bool(getattr(persist, "persist_approvals", True)):
            return
        if _norm(command) != command or any(op in command for op in ("&&", "||", ";", "|", "`", "$(", "\n", "\r")):
            return
        try:
            from saturday.approvals_store import add_rule

            add_rule("allow", command)
            self._publish({"t": "notice", "s": f"[saved approval rule] {command[:80]}"})
        except Exception:
            pass

    def _ask(self, *, kind: str, title: str, command: str = "", diff: str | None = None, detail: str = "", remember_key: str = "") -> bool:
        aid = f"a{self._scope}:{next(self._seq)}" if self._scope else f"a{next(self._seq)}"
        box = {"event": threading.Event(), "decision": None}
        with self._lock:
            self._pending[aid] = box
        self._publish(
            {
                "t": "approval",
                "id": aid,
                "kind": kind,
                "title": title,
                "detail": detail,
                "command": command,
                "diff": diff,
                "ttl": int(self.ttl),
            }
        )
        try:
            got = box["event"].wait(self.ttl)
        finally:
            with self._lock:
                self._pending.pop(aid, None)
        decision = box["decision"]
        allowed = bool(got and decision in ("allow", "always"))
        self._denial_note = "" if allowed else str(box.get("note") or "")[:500]
        if decision == "always" and remember_key:
            if kind == "file":
                self.allowed_paths.add(remember_key)
            else:
                self.allowed_commands.add(remember_key)
                self._persist_rule(remember_key)
        elif decision == "deny" and kind == "command" and remember_key:
            self.denied_commands.add(remember_key)
        if not got:
            if kind == "command" and remember_key:
                self.denied_commands.add(remember_key)
            self._publish({"t": "notice", "s": f"[approval timed out -> denied] {title}"})
        self._publish({"t": "approval_done", "id": aid, "allowed": allowed, "timeout": not got})
        return allowed

    def consume_denial_note(self) -> str:
        """The user's optional note attached to the most recent deny (Claude
        Code-style "deny with feedback"); consumed once by the denial-message
        builder in safety.py so the agent learns WHY it was refused."""
        with self._lock:
            note, self._denial_note = self._denial_note, ""
        return note

    def resolve(self, aid: str, decision: str, note: str = "") -> bool:
        with self._lock:
            # Decision guard + assignment must be atomic with the lookup:
            # two concurrent resolves (double-click / duplicate POST) used to
            # both pass the None check and overwrite each other's decision.
            box = self._pending.get(aid)
            if box is None or box["decision"] is not None:
                return False
            box["decision"] = decision
            box["note"] = str(note or "")[:500]
            box["event"].set()
        return True

    # -- interactive questions (ask_user tool) --------------------------------------
    def ask_question(self, question: str, options: list[str] | None = None, ttl: float | None = None) -> str:
        """Publish an interactive question card and block for the answer.

        Returns the user's answer, or "" on timeout (the tool then tells the
        model to proceed with best judgment). Shares the approval id-namespace
        but uses its own event kind so the UI renders a different card."""
        aid = f"q{self._scope}:{next(self._seq)}" if self._scope else f"q{next(self._seq)}"
        box = {"event": threading.Event(), "decision": None, "note": ""}
        with self._lock:
            self._pending[aid] = box
        wait = float(ttl) if ttl else min(900.0, max(120.0, self.ttl * 4))
        self._publish(
            {
                "t": "ask",
                "id": aid,
                "q": str(question or "")[:1000],
                "options": [str(o)[:200] for o in (options or [])][:8],
                "ttl": int(wait),
            }
        )
        try:
            got = box["event"].wait(wait)
        finally:
            with self._lock:
                self._pending.pop(aid, None)
        answer = str(box.get("note") or "") if got else ""
        self._publish({"t": "ask_done", "id": aid, "answer": answer[:2000], "timeout": not got})
        return answer

    def cancel_pending(self, why: str) -> None:
        with self._lock:
            boxes = list(self._pending.values())
        for box in boxes:
            if box["decision"] is None:
                box["decision"] = "deny"
                box["event"].set()
        if boxes:
            self._publish({"t": "notice", "s": f"[approvals cancelled: {why}]"})


class WebFileGate:
    """pre_tool_call hook mirroring repl.FileEditGate: unified-diff preview."""

    def __init__(self, approver: WebApprover, root: str | None = None, auto_approve: bool = False) -> None:
        self.approver = approver
        self.root = root
        self.auto_approve = auto_approve

    def __call__(self, tool_name: str, args: dict) -> str | None:
        from saturday.editing import FILE_EDIT_TOOLS, render_file_diff

        if tool_name not in FILE_EDIT_TOOLS:
            return None
        if self.auto_approve:
            return None
        path = _norm(str(args.get("path") or ""))
        if not path or path in self.approver.allowed_paths:
            return None
        preview = render_file_diff(tool_name, args, root=self.root)
        body = args.get("content") or args.get("new_string") or ""
        allowed = self.approver.ask_file(tool_name, path, preview, len(str(body)))
        if allowed:
            return None
        return f"user declined {tool_name} on {path}"


# ---------------------------------------------------------------------------
# Event bus


class EventBus:
    """Bounded event buffer + fan-out to live subscribers.

    Events carry a monotonic ``_seq`` so replay can be positional-safe even
    after the ring buffer wraps (long-lived sessions). Per-subscriber queues
    are bounded too: a client that stops reading (dead socket the pump has
    not noticed yet) must not grow the process without limit — on overflow
    the OLDEST queued event is dropped, mirroring the buffer's own
    newest-wins policy (clients resync via hydrate/replay)."""

    SUB_QUEUE_MAX = 2000

    def __init__(self, maxlen: int = 4000) -> None:
        self.buf: deque[dict] = deque(maxlen=maxlen)
        self.subs: list[Queue] = []
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def publish(self, evt: dict) -> None:
        with self._lock:
            # Stamp the caller's dict (kept: callers can read back their own
            # _seq), but fan out snapshots — buffer and subscribers must not
            # share one mutable object a later handler could mutate.
            evt.setdefault("_seq", next(self._seq))
            snapshot = copy.copy(evt)
            self.buf.append(snapshot)
            subs = list(self.subs)
        for q in subs:
            item = copy.copy(snapshot)
            try:
                q.put_nowait(item)
            except Exception:
                try:
                    q.get_nowait()  # drop the oldest, keep the queue bounded
                    q.put_nowait(item)
                except Exception:
                    pass

    def subscribe(self) -> Queue:
        q: Queue = Queue(maxsize=self.SUB_QUEUE_MAX)
        with self._lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            if q in self.subs:
                self.subs.remove(q)

    @property
    def last_seq(self) -> int:
        with self._lock:
            return getattr(self.buf[-1], "get", lambda k: 0)("_seq") if self.buf else 0

    def replay(self, after: int) -> list[dict]:
        with self._lock:
            return [e for e in self.buf if e.get("_seq", 0) > after]


_Bus = EventBus  # legacy name


# ---------------------------------------------------------------------------
# Per-session runtime with explicit run-state machine


class SessionRuntime:
    PHASE_IDLE = "idle"
    PHASE_RUNNING = "running"

    def __init__(self, sid: str, agent, bus: EventBus | None = None, project_id: str | None = None) -> None:
        self.sid = sid
        self.agent = agent
        self.bus = bus if bus is not None else EventBus()
        self.project_id = project_id
        # -- run-state machine (see module docstring) --
        self._run_lock = threading.RLock()
        self._phase = self.PHASE_IDLE
        self._stop_requested = False
        self.run_generation = 0  # monotonic; NEVER reset
        self.run_started_at = 0.0  # time.time() while a run is live (runs panel)
        # -- web surface wiring --
        self.approver = WebApprover(self.bus.publish, scope=sid)
        self.file_gate = WebFileGate(
            self.approver,
            root=getattr(getattr(agent, "cfg", None), "workspace_root", None),
            auto_approve=is_autonomous(getattr(getattr(agent, "cfg", None), "safety_mode", "ask")),
        )
        self.pending_images: list[str] = []
        self.pending_calls: list[tuple[str, str, dict]] = []
        self._card_seq = itertools.count(1)
        self._ctx_base: dict | None = None
        self.app = None  # AppState backref, injected by runtime_for()
        self.last_used = time.monotonic()  # drives AppState idle eviction

    # -- state machine -------------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._run_lock:
            return self._phase == self.PHASE_RUNNING

    @property
    def is_idle(self) -> bool:
        with self._run_lock:
            return self._phase == self.PHASE_IDLE

    @busy.setter
    def busy(self, value: bool) -> None:
        # legacy write-path kept for compatibility; new code uses the methods
        if value:
            self.try_begin_run()
        else:
            self.finish_run()

    def try_begin_run(self) -> bool:
        """Atomically idle->running. False means a run is already active."""
        with self._run_lock:
            if self._phase != self.PHASE_IDLE:
                return False
            self._phase = self.PHASE_RUNNING
            self._stop_requested = False
            self.run_started_at = time.time()
            self.run_generation += 1
            return True

    def finish_run(self) -> None:
        """The ONLY way a run ends. Callers MUST invoke this BEFORE publishing
        their terminal bus event so pumps see idle-when-done."""
        with self._run_lock:
            self._phase = self.PHASE_IDLE
            self._stop_requested = False
            self.run_started_at = 0.0

    def request_stop(self) -> None:
        with self._run_lock:
            self._stop_requested = True

    def should_stop(self) -> bool:
        with self._run_lock:
            return self._stop_requested

    # legacy alias
    @property
    def stop_flag(self) -> bool:
        return self.should_stop()

    @stop_flag.setter
    def stop_flag(self, value: bool) -> None:
        if value:
            self.request_stop()

    # -- context meter ------------------------------------------------------------
    def emit_ctx(self, agent, messages: list[dict]) -> None:
        """Publish a live context estimate (called on every step checkpoint)."""
        try:
            if self._ctx_base is None:
                bd = agent.context_breakdown([])
                sys_t = next((s["tokens"] for s in bd["sections"] if s["key"] == "system"), 0)
                tools_t = next((s["tokens"] for s in bd["sections"] if s["key"] == "tools"), 0)
                self._ctx_base = {"sys": sys_t, "tools": tools_t}
            from saturday.agent.memory import estimate_message_tokens

            hist = sum(estimate_message_tokens(m) for m in messages)
            self.bus.publish(
                {
                    "t": "ctx",
                    "prompt": self._ctx_base["sys"] + self._ctx_base["tools"] + hist,
                    "compact": agent.cfg.compact_above_tokens,
                    "budget": getattr(agent.cfg, "max_context_tokens", 96_000),
                }
            )
        except Exception:
            pass

    def take_pending_call(self, tool_name: str) -> tuple[str, dict]:
        """FIFO-match a result to the oldest started call with the same name.

        The loop executes calls in index order (parallel execution completes
        before results are iterated), so name-matched FIFO is deterministic."""
        for i, (card, name, args) in enumerate(self.pending_calls):
            if name == tool_name:
                del self.pending_calls[i]
                return card, args
        return f"r{uuid.uuid4().hex[:8]}", {}

    @property
    def store(self):
        return self.agent.session_store


_SessionRuntime = SessionRuntime  # legacy name


def install_web_surface(rt: SessionRuntime, agent) -> None:
    """Wire tool-card emission + file gate + approver (mirrors Repl.__init__)."""
    from saturday.agent.loop import LoopHooks

    base = agent.hooks or LoopHooks()
    if not getattr(agent, "_web_gate_installed", False):
        gate = rt.file_gate
        user_pre = base.pre_tool_call

        def chained_gate(tool_name: str, tool_args: dict) -> str | None:
            # CHAIN, never replace: a pre-existing pre_tool_call hook (another
            # surface's gate, future wiring) must keep running — replacing it
            # here silently dropped that hook (design-review finding V4).
            block = user_pre(tool_name, tool_args) if user_pre else None
            if block is not None:
                return block
            card = f"c{next(rt._card_seq)}"
            rt.pending_calls.append((card, tool_name, tool_args))
            rt.bus.publish({"t": "tool_start", "card": card, "name": tool_name, "args": tool_args})
            return gate(tool_name, tool_args)

        base.pre_tool_call = chained_gate
        agent._web_gate_installed = True
    if not getattr(agent, "_web_ctx_installed", False):
        prev_ckpt = base.on_checkpoint

        def ctx_checkpoint(messages, _prev=prev_ckpt, _rt=rt, _agent=agent):
            _rt.emit_ctx(_agent, messages)
            if _prev:
                _prev(messages)

        base.on_checkpoint = ctx_checkpoint
        agent._web_ctx_installed = True
    agent.hooks = base
    # approver is attached in every mode: ask uses it for gated tools, and the
    # destructive-data guardrails need it even when safety is "off"
    agent.approval_policy.approver = rt.approver
    # Backref so WebApprover._persist_rule can read the real cfg.persist_approvals.
    # Without it getattr(approver, "agent") was ALWAYS None, so the opt-out was
    # dead code and "always allow" silently persisted even when disabled.
    rt.approver.agent = agent
    # ask_user: route the tool's clarifying questions to this session's question box
    try:
        ask_tool = agent.registry.get("ask_user")
    except Exception:
        ask_tool = None
    if ask_tool is not None:
        ask_tool.ask_fn = lambda q, options, ttl, _rt=rt: _rt.approver.ask_question(q, options, ttl)
    # subagent progress: forward child activity to the bus, attributed to the
    # parent `task` card (Claude Code / Warp subagent-rows parity)
    try:
        task_tool = agent.registry.get("task")
    except Exception:
        task_tool = None
    if task_tool is not None and getattr(task_tool, "_event_fn", None) is None:

        def _sub_event(cid, kind, kw, _rt=rt):
            parent = None
            for c, name, _a in reversed(_rt.pending_calls):
                if name == "task":
                    parent = c
                    break
            evt = {"t": "subagent", "child": cid, "kind": kind, "parent": parent}
            evt.update(kw)
            _rt.bus.publish(evt)

        task_tool._event_fn = _sub_event


_install_web_surface = install_web_surface  # legacy name
