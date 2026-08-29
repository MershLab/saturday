"""Interactive console app for Saturday: streaming REPL with inline approvals,
diff previews, input history and a real slash-command surface. Stdlib-only."""
from __future__ import annotations

import sys
from typing import Callable

from saturday.ui import paint
from saturday.safety import is_autonomous
from saturday.editing import (  # noqa: F401  (re-exported for compat)
    FILE_EDIT_TOOLS,
    _norm,
    render_file_diff,
)


class ConsoleApprover:
    """policy.approver implementation: y/n/a prompts with session-scoped memory.

    'a' (always) remembers the exact normalized command for the rest of the session.
    """

    def __init__(self, input_fn: Callable[[str], str] = input, output_fn: Callable[..., None] = print) -> None:
        self.allowed_commands: set[str] = set()
        self.denied_commands: set[str] = set()
        self.allowed_paths: set[str] = set()
        self._input = input_fn
        self._output = output_fn

    def __call__(self, command: str, reason: str) -> bool:
        key = _norm(command)
        if key in self.allowed_commands:
            return True
        if key in self.denied_commands:
            self._output(paint(f"[auto-denied (session rule)] {reason}", "dim"))
            return False
        self._output("")
        self._output(paint(f"[approval needed] {reason}", "yellow"))
        self._output(paint(f"  $ {_norm(command)[:500]}", "bold"))
        for _ in range(3):
            ans = self._input("Allow? [y]es / [n]o / [a]lways this command > ").strip().lower()
            if ans in ("y", "yes"):
                return True
            if ans in ("a", "always"):
                self.allowed_commands.add(key)
                self._output(paint("[allowed for this session]", "dim"))
                return True
            if ans in ("n", "no", ""):
                break
        self.denied_commands.add(key)
        return False


class FileEditGate:
    """pre_tool_call hook: shows a unified diff for write/edit and asks inline."""

    def __init__(
        self,
        approver: ConsoleApprover,
        allowed_paths: set[str] | None = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[..., None] = print,
        root: str | None = None,
        auto_approve: bool = False,
    ) -> None:
        self.approver = approver
        self.allowed_paths = allowed_paths if allowed_paths is not None else approver.allowed_paths
        self._input = input_fn
        self._output = output_fn
        # workspace root for resolving relative edit paths in previews
        self.root = root
        # fully-autonomous mode: apply every file edit without asking
        self.auto_approve = auto_approve

    def __call__(self, tool_name: str, args: dict) -> str | None:
        if tool_name not in FILE_EDIT_TOOLS:
            return None
        if self.auto_approve:
            return None
        path = _norm(str(args.get("path") or ""))
        if not path:
            return None
        if path in self.allowed_paths:
            return None
        preview = render_file_diff(tool_name, args, root=self.root)
        self._output("")
        self._output(paint(f"[file change] {tool_name} -> {path}", "yellow"))
        if preview:
            for line in preview.splitlines():
                color = "green" if line.startswith("+") and not line.startswith("+++") else ("red" if line.startswith("-") and not line.startswith("---") else "dim")
                self._output(paint(line, color))
        else:
            body = args.get("content") or args.get("new_string") or ""
            self._output(paint(f"  ({len(str(body))} chars)", "dim"))
        for _ in range(3):
            ans = self._input("Apply? [y]es / [n]o / [a]lways this file > ").strip().lower()
            if ans in ("y", "yes"):
                return None
            if ans in ("a", "always"):
                self.allowed_paths.add(path)
                self._output(paint("[allowed for this session]", "dim"))
                return None
            if ans in ("n", "no", ""):
                break
        return f"user declined {tool_name} on {path}"


# Shared /help text lives in the slash registry (single source of truth for
# both surfaces); re-exported here so existing imports keep working.
from saturday.slash import HELP_TEXT  # noqa: F401,E402


class Repl:
    def __init__(
        self,
        agent,
        *,
        tui: bool = False,
        store=None,
        initial_history: list[dict] | None = None,
        resumed_id: str | None = None,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[..., None] = print,
    ) -> None:
        self.agent = agent
        self.tui = tui
        self.store = store or agent.session_store
        self.initial_history = initial_history
        self.resumed_id = resumed_id
        self.history_note: list[str] = []
        self.pending_images: list[str] = []
        self.line_buffer: list[str] = []
        self._input = input_fn
        self._output = output_fn
        self.approver = ConsoleApprover(input_fn=input_fn, output_fn=output_fn)
        self.file_gate = FileEditGate(
            self.approver, input_fn=input_fn, output_fn=output_fn,
            root=getattr(getattr(self.agent, "cfg", None), "workspace_root", None),
            auto_approve=is_autonomous(getattr(getattr(self.agent, "cfg", None), "safety_mode", "ask")),
        )

        from saturday.agent.loop import LoopHooks

        base_hooks = agent.hooks or LoopHooks()
        if not getattr(agent, "_repl_gate_installed", False):
            gate = self.file_gate
            user_pre = base_hooks.pre_tool_call

            def chained_gate(tool_name: str, tool_args: dict) -> str | None:
                block = user_pre(tool_name, tool_args) if user_pre else None
                if block is not None:
                    return block
                return gate(tool_name, tool_args)

            base_hooks.pre_tool_call = chained_gate
            agent._repl_gate_installed = True
        agent.hooks = base_hooks
        # attach in every mode: ask gates need it, and destructive-data
        # guardrails must be able to prompt even when safety is "off"
        if agent.approval_policy.approver is None:
            agent.approval_policy.approver = self.approver

    def _setup_input_history(self) -> None:
        import os

        if os.name == "posix" and sys.stdin.isatty():
            try:
                import readline  # noqa: F401
            except ImportError:
                pass

    def read_line(self, prompt_str: str) -> str:
        parts = [self._input(prompt_str)]
        while parts[-1].endswith("\\"):
            parts[-1] = parts[-1][:-1]
            parts.append(self._input(paint("   … ", "cyan")))
        return "\n".join(parts).strip()

    def dispatch(self, line: str) -> bool:
        if not line.startswith("/"):
            return False
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()
        # shared registry (saturday.slash) powers both this REPL and the web
        # chat surface; per-surface behavior lives on the command entries
        from saturday.slash import COMMANDS, SlashContext

        ctx = SlashContext.for_repl(self)
        sc = COMMANDS.get(cmd)
        if sc is None:
            ctx.out(f"unknown command {cmd}; try /help")
        else:
            sc.run_repl(ctx, arg)
        for s in ctx.lines:
            self._output(s)
        return True

    def run(self) -> int:
        self._setup_input_history()
        out = self._output
        prompt_str = paint(" ▸ ", "cyan")
        try:
            sid = self.resumed_id or self.store.create({"task": "interactive"})
        except OSError:
            sid = None
        self._sid = sid
        if sid:
            out(paint(f"[session {sid}]", "dim"))
        try:
            if self.tui:
                from saturday import tui as t

                t.enter_alt_screen()
                out(t.header())
            else:
                out("Saturday session. /help for commands.")
            while True:
                if self.tui:
                    from saturday import tui as t

                    out(t.status_line(self.agent, sid))
                try:
                    user = self.read_line(prompt_str)
                except (EOFError, KeyboardInterrupt):
                    return 0
                if user.lower() in ("exit", "quit"):
                    return 0
                if not user:
                    continue
                if user:
                    try:
                        if self.dispatch(user):
                            continue
                    except Exception as exc:
                        out("\n" + paint(f"[command error] {type(exc).__name__}: {exc}", "red"))
                        continue
                task = user
                if self.history_note:
                    task = user + "\n\n(Conversation so far:\n" + "\n".join(self.history_note[-6:]) + ")"
                traj = None
                try:
                    # continuity comes from the per-step checkpoint (same as the
                    # webui surface); the note is a human-readable summary only
                    initial_history = None
                    if sid:
                        try:
                            initial_history = self.agent.session_store.load_checkpoint(sid)
                            self.agent.restore_checkpoint_meta(
                                self.agent.session_store.load_checkpoint_meta(sid)
                            )
                        except Exception:
                            initial_history = None
                    if initial_history is None:
                        initial_history = self.initial_history
                    traj = self.agent.run(
                        task,
                        attachments=self.pending_images or None,
                        on_text_delta=lambda d: self._stream(d, False),
                        on_reasoning_delta=lambda d: self._stream(d, True),
                        on_tool_result=self._on_result,
                        initial_history=initial_history,
                        session_id=sid,
                    )
                except KeyboardInterrupt:
                    out("\n" + paint("[interrupted]", "yellow"))
                except Exception as exc:
                    out("\n" + paint(f"[error] {type(exc).__name__}: {exc}", "red"))
                if traj is None:
                    self.pending_images.clear()
                    continue
                # no session-hint fallback: when store.create failed above,
                # persistence simply degrades (usage rows carry session="")
                # instead of crashing on a now-undefined name lookup
                self.pending_images.clear()
                try:
                    from saturday.usage import record_usage

                    record_usage(
                        provider=self.agent.cfg.provider,
                        model=self.agent.cfg.model or "?",
                        session=sid or "",
                        steps=len(traj.steps),
                        prompt_tokens=traj.usage.prompt_tokens,
                        completion_tokens=traj.usage.completion_tokens,
                        total_tokens=traj.usage.total_tokens,
                        stop_reason=traj.stop_reason or "",
                    )
                except Exception:
                    pass
                self.history_note.append(f"user: {user}")
                self.history_note.append(f"agent: {(traj.final_answer or '')[:800]}")
                ctx_bit = ""
                try:
                    history = []
                    if sid:
                        history = self.agent.session_store.load_checkpoint(sid) or []
                    bd = self.agent.context_breakdown(history)
                    ctx_bit = f" · ctx ~{bd['prompt_tokens']:,}"
                except Exception:
                    pass
                footer = f" done · steps {len(traj.steps)} · tokens {traj.usage.total_tokens}{ctx_bit} · {traj.stop_reason}"
                if not traj.final_answer:
                    out("\n" + paint(f"[stopped: {traj.stop_reason}]", "yellow"))
                out("\n" + paint(footer, "dim"))
                if self.tui:
                    from saturday import tui as t

                    out(t.rule())
        finally:
            if self.tui:
                from saturday import tui as t

                t.exit_alt_screen()

    def _stream(self, delta: str, reasoning: bool) -> None:
        sys.stdout.write(paint(delta, "dim") if reasoning else delta)
        sys.stdout.flush()

    def _on_result(self, result) -> None:
        color = "green" if result.ok else "red"
        status = "ok" if result.ok else "ERROR"
        body = (result.output if result.ok else (result.error or ""))[:300]
        self._output("\n" + paint(f"[{result.name} {status}]", color) + " " + body)
