from __future__ import annotations

from typing import Callable

from saturday.agent.loop import AgentLoop, LoopHooks
from saturday.agent.memory import WorkingMemory
from saturday.config import AgentConfig
from saturday.llm.client import LLMClient, LLMContextOverflow, StreamEvent  # noqa: F401
from saturday.plugins import core_plugin, install_plugins, workflow_plugin
from saturday.safety import ApprovalPolicy, isolation_enforced, make_approval_hook, normalize_mode
from saturday.sessions import SessionStore
from saturday.tasks import SubagentTask
from saturday.tools.base import ToolRegistry
from saturday.types import ToolResult, Trajectory

_SANDBOXED_WARN_PREFIX = "sandboxed=true requested"
_SANDBOXED_WARN_MESSAGE = (
    "sandboxed=true requested, but this build ships no isolation executor — "
    "approval friction stays ON (dangerous/guardrail asks NOT waived)"
)


class Agent:
    """Top-level facade: config -> client -> plugins -> tools -> loop."""

    def __init__(
        self,
        cfg: AgentConfig | None = None,
        *,
        client: LLMClient | None = None,
        registry: ToolRegistry | None = None,
        memory: WorkingMemory | None = None,
        hooks: LoopHooks | None = None,
        plugins: list | None = None,
        persona_extra: str = "",
        enable_subagents: bool = True,
        subagent_depth: int = 1,
        safety: bool | str = True,
        session_store: SessionStore | None = None,
    ) -> None:
        self.cfg = cfg or AgentConfig.load()
        self.client = client
        # an injected client (tests, custom runtimes) must NOT be silently
        # replaced by build_client on first run: record its signature so
        # _ensure_client reuses it while the config is unchanged.
        self._client_signature = None
        if client is not None:
            try:
                self._client_signature = (
                    getattr(self.cfg, "provider", "") or "",
                    getattr(self.cfg, "model", None),
                    tuple(getattr(self.cfg, "fallback_models", ()) or ()),
                    getattr(self.cfg, "max_tokens", None),
                )
            except Exception:
                self._client_signature = None
        # persistent allow-rules survive restarts (approvals.json); a caller-
        # supplied policy keeps whatever rules it already carries
        if safety is True:
            safety_mode = getattr(self.cfg, "safety_mode", "ask") or "ask"
            self.approval_policy = ApprovalPolicy.from_mode(
                safety_mode, blocked_apps=list(getattr(self.cfg, "blocked_apps", []) or [])
            )
        elif safety is False:
            self.approval_policy = ApprovalPolicy.from_mode("off")
        else:
            self.approval_policy = ApprovalPolicy.from_mode(str(safety))
        if (not self.approval_policy.allow_rules or not self.approval_policy.deny_rules) and bool(
            getattr(self.cfg, "persist_approvals", True)
        ):
            from saturday.approvals_store import load_rules

            rules = load_rules()
            # deny rules are the "never, even with safety off" floor: they load
            # unconditionally, unlike allow rules which only fill an empty list
            self.approval_policy.deny_rules = list(rules.get("deny") or [])
            if not self.approval_policy.allow_rules:
                self.approval_policy.allow_rules = list(rules.get("allow") or [])
        self.registry = registry
        self._custom_registry = registry is not None
        self.memory = memory or WorkingMemory(max_chars=self.cfg.memory_max_chars)
        self.hooks = hooks
        self.persona_sections: list[str] = []
        # Hermes parity: SOUL.md rides into the system prompt via persona_extra
        # (project AGENTS.md/CLAUDE.md stays with _rules_block's precedence);
        # never let its absence break startup
        try:
            from saturday.config import load_soul

            merged = (persona_extra + "\n\n" + load_soul()).strip()
            self.persona_extra = merged or persona_extra
        except Exception:
            self.persona_extra = persona_extra
        if plugins is None:
            from saturday.plugins import learning_plugin

            plugins = [core_plugin(self.cfg), workflow_plugin(), learning_plugin()]
        self.plugins = plugins
        self._assembled = False
        self.enable_subagents = enable_subagents and subagent_depth > 0
        self.subagent_depth = subagent_depth
        self.session_store = session_store if session_store is not None else SessionStore()
        self.warnings: list[str] = []
        # Per-agent plan-mode override (None => follow cfg.plan_mode). The web
        # UI and REPL /plan slash toggle THIS, so sessions don't fight over a
        # shared cfg object.
        self._plan_mode_override: bool | None = None
        # Per-agent safety-mode override (None => follow cfg.safety_mode), same
        # reasoning as plan_mode: /yolo in one web session must not flip the
        # shared base cfg for every concurrent session.
        self._safety_mode_override: str | None = None
        # Per-agent tool-toggle set (session-scoped via /toggle); None =>
        # follow cfg.disabled_tools. Stored as concrete expanded names.
        self._disabled_tools_override: set[str] | None = None
        # Hidden contract: several surfaces (web UI, REPL, project switcher)
        # SET agent.memory_scope directly; default it here so the attribute
        # always exists instead of relying on getattr fallbacks everywhere.
        self.memory_scope = None
        # Dedupe flag so persistence failures surface once per session rather
        # than spamming a warning every step.
        self._persist_warned = False

    @property
    def plan_mode(self) -> bool:
        if self._plan_mode_override is not None:
            return self._plan_mode_override
        return bool(getattr(self.cfg, "plan_mode", False))

    @plan_mode.setter
    def plan_mode(self, value: bool) -> None:
        self._plan_mode_override = bool(value)

    @property
    def safety_mode(self) -> str:
        """Effective safety mode: per-agent override wins over shared cfg."""
        if self._safety_mode_override is not None:
            return self._safety_mode_override
        return normalize_mode(getattr(self.cfg, "safety_mode", "ask"))

    @safety_mode.setter
    def safety_mode(self, value: str) -> None:
        self._safety_mode_override = normalize_mode(value)

    @property
    def disabled_tools(self) -> set[str]:
        """Effective concrete disabled-tool names (override wins over cfg)."""
        if self._disabled_tools_override is not None:
            return set(self._disabled_tools_override)
        return ToolRegistry.expand_tool_names(getattr(self.cfg, "disabled_tools", []) or [])

    def toggle_tool(self, name_or_family: str) -> tuple[bool, str, bool]:
        """Session-scoped toggle. Returns (ok, message, now_disabled)."""
        key = str(name_or_family or "").strip()
        if not key:
            return False, "usage: /toggle <tool-or-family>", False
        current = self.disabled_tools
        members = ToolRegistry.TOOL_FAMILIES.get(key)
        target = {key} if members is None else set(members)
        # Validation must NOT open MCP connections (a dead server used to make
        # /toggle stall); consult the full registry only when MCP is already
        # up from a previous run, otherwise the base registry is enough.
        known = (
            self._ensure_mcp()
            if getattr(self, "_mcp_installed", False)
            else self._build_registry()
        )
        if members is None and key not in known.names():
            return (
                False,
                f"unknown tool or family '{key}' (families: {', '.join(sorted(ToolRegistry.TOOL_FAMILIES))})",
                False,
            )
        if members is not None and target <= current:
            current -= target
            action = "enabled"
        elif members is not None:
            current |= target
            action = "disabled"
        elif target <= current:
            current -= target
            action = "enabled"
        else:
            current |= target
            action = "disabled"
        self._disabled_tools_override = current
        scope = f"family '{key}'" if members is not None else f"'{key}'"
        return True, f"{scope} {action} for this session", key in current or (members is not None and target <= current)

    def effective_registry(self) -> ToolRegistry:
        """Registry as the model will see it this run (toggles + plan mode)."""
        registry = self._ensure_mcp()
        disabled = self.disabled_tools
        if disabled:
            registry = registry.excluding(disabled)
        if self.plan_mode:
            registry = registry.filtered(ToolRegistry.READ_ONLY_TOOLS)
        return registry

    def _ensure_client(self) -> LLMClient:
        if self.cfg.provider in (getattr(self.cfg, "blocked_providers", None) or ()):
            raise ValueError(f"provider '{self.cfg.provider}' is blocked by a data-policy guardrail (blocked_providers)")
        if self.cfg.model in (getattr(self.cfg, "blocked_models", None) or ()):
            raise ValueError(f"model '{self.cfg.model}' is blocked by a data-policy guardrail (blocked_models)")
        signature = (
            self.cfg.provider,
            self.cfg.model,
            tuple(getattr(self.cfg, "fallback_models", ()) or ()),
            getattr(self.cfg, "max_tokens", None),
        )
        if self.client is not None and getattr(self, "_client_signature", None) == signature:
            return self.client
        from saturday.llm.providers import build_client

        self.client = build_client(self.cfg)
        self._client_signature = signature
        return self.client

    def _build_registry(self) -> ToolRegistry:
        """Assemble the BASE tool registry exactly once (idempotent, cheap).

        Covers, in order: plugin tools (skipped for caller-supplied
        registries), the subagent tool, and per-project memory scoping.
        Deliberately excludes MCP: connecting to servers lives in
        _ensure_mcp() so cheap validation paths (/toggle) never open sockets.
        A caller-supplied registry keeps its tools but still receives
        task + memory wiring — silently skipping those made behavior depend
        on an easy-to-miss constructor detail."""
        if self._assembled:
            return self.registry
        self._assembled = True
        self.registry = self.registry or ToolRegistry()
        if not self._custom_registry:
            install_plugins(self.registry, list(self.plugins or []), self.persona_sections)
        if self.enable_subagents and self.registry.get("task") is None:
            self.registry.register(self._make_task_tool())
        # persona_mode never removes capability: assistant mode hides plumbing
        # in the UI + prompt (and defaults to background-first computer use),
        # but the model keeps every tool that acts on the world.
        scope = getattr(self, "memory_scope", None)
        mem_tool = self.registry.get("memory")
        if scope and mem_tool is not None:
            from pathlib import Path as _P

            mem_tool.scope_path = str(_P(scope) / ".saturday" / "MEMORY.md")
        return self.registry

    def _ensure_mcp(self) -> ToolRegistry:
        """Install configured MCP servers exactly once, on demand.

        Split from _build_registry so validation-only callers avoid network
        I/O; every real run reaches MCP through effective_registry(), so
        connection behavior for actual runs is unchanged."""
        if getattr(self, "_mcp_ready", False):
            return self.registry
        self._mcp_ready = True
        self._build_registry()
        for problem in getattr(self.cfg, "mcp_warnings", None) or []:
            self.warnings.append(f"mcp: {problem}")
        mcp_servers = getattr(self.cfg, "mcp_servers", None) or {}
        if not mcp_servers or getattr(self, "_mcp_installed", False):
            return self.registry
        self._mcp_installed = True
        from saturday.mcp_plugin import build_mcp_plugin

        def warn(msg: str) -> None:
            self.warnings.append(msg)

        mcp = build_mcp_plugin(mcp_servers, on_warning=warn)
        install_plugins(self.registry, [mcp], self.persona_sections)
        if not getattr(mcp, "warnings", None):
            self.warnings.append("mcp: all configured servers connected")
        return self.registry

    def _checkpoint_meta(self) -> dict:
        """Cursor-style checkpoint payload: everything needed to resume the
        agent's BRAIN (not just its transcript) — pinned working memory,
        todo plan, goal, and the file-journal position so /rewind can roll
        the workspace back to this exact point. Tools opt in via
        export_state(); anything without it simply isn't snapshotted."""
        from saturday.tools.journal import journal_length

        tool_states: dict[str, dict] = {}
        try:
            for name, tool in (getattr(self.registry, "_tools", None) or {}).items():
                export = getattr(tool, "export_state", None)
                if callable(export):
                    try:
                        tool_states[name] = export() or {}
                    except Exception:
                        pass
        except Exception:
            pass
        memory_items = [
            {"kind": it.kind, "text": it.text} for it in getattr(self.memory, "items", [])[-40:]
        ]
        return {
            "journal_len": journal_length(getattr(self.cfg, "workspace_root", None) or "."),
            "memory": memory_items,
            "tools": tool_states,
            # token-meter calibration survives restarts (hermes keeps
            # last_prompt_tokens in-process; we go one further)
            "meter": getattr(self, "_meter_state", None) or {},
        }

    def restore_checkpoint_meta(self, meta: dict | None) -> bool:
        """Rehydrate non-transcript state after a resume. Idempotent; file
        rollback is deliberately NOT automatic here — /rewind does that
        explicitly, so resuming never destroys work done outside the agent."""
        if not isinstance(meta, dict) or not meta:
            return False
        meter_state = meta.get("meter")
        if isinstance(meter_state, dict) and meter_state:
            self._meter_state = meter_state

        items = meta.get("memory") or []
        if isinstance(items, list) and items and not getattr(self.memory, "items", None):
            for it in items:
                if isinstance(it, dict) and it.get("text"):
                    self.memory.add(str(it.get("kind") or "note"), str(it["text"]))
        registry_tools = (getattr(self.registry, "_tools", None) or {})
        for name, tool in registry_tools.items():
            state = (meta.get("tools") or {}).get(name)
            imp = getattr(tool, "import_state", None)
            if state is not None and callable(imp):
                try:
                    imp(state)
                except Exception:
                    pass
        return True

    def _make_task_tool(self) -> SubagentTask:
        from saturday.sessions import EphemeralSessionStore

        def child_factory():
            sub = Agent(
                cfg=self.cfg,
                memory=WorkingMemory(),
                hooks=None,
                plugins=[core_plugin(self.cfg)],
                persona_extra="You are a focused sub-agent. Complete the task efficiently and report only the result.",
                enable_subagents=False,
                session_store=EphemeralSessionStore(),
            )
            # child approvals surface through the parent's approver (same human
            # gate) instead of fail-closed blocking with no one to ask
            sub.approval_policy.approver = self.approval_policy.approver
            return sub

        return SubagentTask(agent_factory=child_factory)

    @property
    def native_tool_calling(self) -> bool:
        profile = self.cfg.profile()
        return profile.name != "ollama" or "hermes" not in (self.cfg.model or "").lower()

    def system_prompt(self, registry: ToolRegistry) -> str:
        from saturday.prompts.system import build_system_prompt_parts
        from saturday.tools.memory import load_memory_block
        from saturday.tools.skills import SkillStore, skills_prompt_block

        plugin_persona = "\n\n".join(self.persona_sections)
        extra = (self.persona_extra + "\n\n" + plugin_persona).strip()
        memory_block = "\n\n".join(
            b
            for b in [
                load_memory_block(scope=getattr(self, "memory_scope", None)),
                skills_prompt_block(SkillStore()),
            ]
            if b
        )
        parts = build_system_prompt_parts(
            registry,
            native_tool_calling=self.native_tool_calling,
            enable_reasoning=True,
            workspace_root=self.cfg.workspace_root,
            persona_extra=extra,
            max_steps=self.cfg.max_steps,
            memory_block=memory_block,
            background_only=bool(getattr(self.cfg, "desktop_background_only", False)),
            persona_mode=getattr(self.cfg, "persona_mode", "agent") or "agent",
            assistant_name=getattr(self.cfg, "assistant_name", "") or "",
            assistant_user_title=getattr(self.cfg, "assistant_user_title", "") or "",
            plan_mode=self.plan_mode,
            rules_block=self._rules_block(),
        )
        self._prompt_tiers = parts
        return "\n\n".join([parts["stable"], parts["context"], parts["volatile"]])

    def _rules_block(self) -> str:
        """Workspace AGENTS.md convention (Claude Code CLAUDE.md parity): repo
        instructions autoloaded into the context tier. The project workspace
        (memory_scope) takes precedence over the global root; capped at 8 KB.
        Cached per file mtime — this runs on every system-prompt build."""
        from pathlib import Path

        roots = []
        scope = getattr(self, "memory_scope", None)
        if scope:
            roots.append(Path(scope))
        root = getattr(self.cfg, "workspace_root", None)
        if root:
            roots.append(Path(root))
        cache = getattr(self, "_rules_cache", None)
        if cache is None:
            cache = self._rules_cache = {}
        for base in roots:
            for name in ("AGENTS.md", "CLAUDE.md"):
                p = base / name
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                    key = (str(p), st.st_mtime_ns, st.st_size)
                    hit = cache.get(str(p))
                    if hit is not None and hit[0] == key:
                        return hit[1]
                    text = p.read_text(encoding="utf-8", errors="replace")[:8_000]
                    block = f"# Project instructions ({p.name})\n{text}"
                    cache[str(p)] = (key, block)
                    return block
                except OSError:
                    continue
        return ""

    def context_breakdown(self, history: list[dict] | None = None) -> dict:
        """Token accounting of what a run would send: system prompt tiers,
        tool schemas (native mode only), per-role history and images."""
        from saturday.context import analyze_context, resolve_context_info

        # Context analysis mirrors what a real run would send, so it needs the
        # full registry (MCP schemas included) — same path as a run.
        registry = self._ensure_mcp()
        sysprompt = self.system_prompt(registry)
        tiers = getattr(self, "_prompt_tiers", None)
        native = self.native_tool_calling
        info = resolve_context_info(self.cfg)
        bd = analyze_context(
            system_prompt=sysprompt,
            system_tiers=tiers if isinstance(tiers, dict) else None,
            history=history or [],
            tool_specs=registry.specs() or None,
            include_tool_schemas=native,
            max_context_tokens=info["window"],
            compact_above_tokens=info["compact"],
            max_reply_tokens=self.cfg.max_tokens,
        )
        bd["window_source"] = info["source"]
        return bd

    @staticmethod
    def _effective_sandboxed(cfg, warnings: list[str]) -> bool:
        """cfg.sandboxed AND an actual isolation backend.

        The friction-waiver in safety.check_command is only legitimate when a
        real executor isolates the workload; with none present the flag must
        not strip the only control that exists, so the effective value stays
        False and a one-time warning surfaces the unenforceable request."""
        sandboxed_cfg = bool(getattr(cfg, "sandboxed", False))
        sandboxed = sandboxed_cfg and isolation_enforced()
        warned = any(w.startswith(_SANDBOXED_WARN_PREFIX) for w in warnings)
        if sandboxed_cfg and not sandboxed and not warned:
            warnings.append(_SANDBOXED_WARN_MESSAGE)
        return sandboxed

    def run(
        self,
        task: str,
        *,
        attachments: list[str] | None = None,
        on_text_delta=None,
        on_reasoning_delta=None,
        on_tool_result: Callable[[ToolResult], None] | None = None,
        on_step_start: Callable[[int], None] | None = None,
        initial_history: list[dict] | None = None,
        session_id: str | None = None,
        on_session_id: Callable[[str], None] | None = None,
    ) -> Trajectory:
        client = self._ensure_client()
        registry = self.effective_registry()
        base = self.hooks or LoopHooks()

        def compose(cb_a, cb_b):
            if cb_a is None:
                return cb_b
            if cb_b is None:
                return cb_a

            def both(*a, **k):
                cb_a(*a, **k)
                return cb_b(*a, **k)

            return both

        hooks = LoopHooks(
            # chain instead of override: passing a caller hook must not drop
            # the base one (same asymmetry the other callbacks had)
            on_step_start=compose(base.on_step_start, on_step_start),
            on_text_delta=compose(base.on_text_delta, on_text_delta),
            on_reasoning_delta=compose(base.on_reasoning_delta, on_reasoning_delta),
            on_tool_result=compose(base.on_tool_result, on_tool_result),
            pre_tool_call=base.pre_tool_call,
            post_tool_call=base.post_tool_call,
            on_compaction=base.on_compaction,
            on_checkpoint=base.on_checkpoint,
        )
        auth_scopes = getattr(self.cfg, "auth_scopes", None) or None
        guardrails = bool(getattr(self.cfg, "destructive_guardrails", True))
        sandboxed = self._effective_sandboxed(self.cfg, self.warnings)
        if (
            self.approval_policy.mode != "off"
            or getattr(self.cfg, "desktop_background_only", False)
            or auth_scopes
            or guardrails
            or getattr(self.approval_policy, "allow_rules", None)
        ):
            background_only = bool(getattr(self.cfg, "desktop_background_only", False))
            approval = make_approval_hook(
                self.approval_policy,
                background_only=background_only,
                scopes=auth_scopes,
                guardrails=guardrails,
                sandboxed=sandboxed,
            )
            user_pre = hooks.pre_tool_call

            def chained_pre(tool_name, tool_args):
                block = user_pre(tool_name, tool_args) if user_pre else None
                return block if block is not None else approval(tool_name, tool_args)

            hooks.pre_tool_call = chained_pre

        # user-scriptable lifecycle hooks (hooks.json): refine AFTER safety so
        # approval gating is never bypassed; hooks can only add blocks.
        try:
            from saturday.user_hooks import load_hooks, make_post_tool_hook, make_pre_tool_hook

            hook_cfg = load_hooks(getattr(self.cfg, "workspace_root", None))
            pre_cmds = hook_cfg.get("pre_tool_call") or []
            post_cmds = hook_cfg.get("post_tool_call") or []
            if pre_cmds:
                safety_pre = hooks.pre_tool_call

                def hooked_pre(tool_name, tool_args, _s=safety_pre, _h=make_pre_tool_hook(pre_cmds)):
                    block = _s(tool_name, tool_args) if _s else None
                    return block if block is not None else _h(tool_name, tool_args)

                hooks.pre_tool_call = hooked_pre
            if post_cmds:
                base_post = hooks.post_tool_call
                extra_post = make_post_tool_hook(post_cmds)
                if base_post is None:
                    hooks.post_tool_call = extra_post
                else:
                    def hooked_post(result, _b=base_post, _e=extra_post):
                        _b(result)
                        _e(result)

                    hooks.post_tool_call = hooked_post
        except Exception as exc:
            # Hooks are an enhancement, never a hard dependency — but failing
            # silently hid misconfiguration (bad hooks.json path, syntax
            # error); surface once per run so the user can fix it.
            self.warnings.append(f"user hooks disabled: {exc}")

        from saturday.context import effective_windows

        _window, _compact = effective_windows(self.cfg)
        loop = AgentLoop(
            client,
            registry,
            max_steps=self.cfg.max_steps,
            temperature=self.cfg.temperature,
            top_p=self.cfg.top_p,
            max_tokens=self.cfg.max_tokens,
            compact_above_tokens=_compact,
            memory=self.memory,
            hooks=hooks,
            keep_reasoning_in_history=getattr(self.cfg, "keep_reasoning_in_history", False),
            max_run_tokens=int(getattr(self.cfg, "max_run_tokens", 0) or 0),
            max_wall_seconds=int(getattr(self.cfg, "max_wall_seconds", 0) or 0),
            max_run_cost_usd=float(getattr(self.cfg, "max_run_cost_usd", 0.0) or 0.0),
            cost_provider=self.cfg.provider or "",
            cost_model=self.cfg.model or "",
            memory_nudge_interval=int(getattr(self.cfg, "memory_nudge_interval", 0) or 0),
            # optional per-tool-call watchdog (None => wait forever)
            tool_call_timeout=getattr(self.cfg, "tool_timeout", None),
            injection_guard=bool(getattr(self.cfg, "injection_guard", True)),
        )
        # resume calibration: prior runs' EMA + last reported prompt size
        loop.set_meter_state(getattr(self, "_meter_state", None))
        sysprompt = self.system_prompt(registry)
        sid = session_id or self.session_store.create({"task": task})
        if on_session_id is not None:
            on_session_id(sid)

        base_checkpoint = base.on_checkpoint

        def persist_checkpoint(messages):
            if base_checkpoint:
                base_checkpoint(messages)
            try:
                self.session_store.save_checkpoint(sid, messages, meta=self._checkpoint_meta())
            except OSError as exc:
                # Persistence loss must not kill the run, but staying silent
                # meant invisible resume gaps; surface once per session.
                if not self._persist_warned:
                    self._persist_warned = True
                    self.warnings.append(f"checkpoint persistence failed: {exc}")

        hooks.on_checkpoint = persist_checkpoint
        loop.hooks = hooks
        traj = loop.run(sysprompt, task, initial_history=initial_history, attachments=attachments)
        # carry calibration forward to the next run in this process
        self._meter_state = loop.meter_state

        try:
            self.session_store.append(sid, {"type": "messages", "messages": traj.messages()[1:]})
        except OSError as exc:
            # same dedupe flag as checkpoints: one persistence warning per
            # session is enough — the user needs to know, not be spammed
            if not self._persist_warned:
                self._persist_warned = True
                self.warnings.append(f"session persistence failed: {exc}")
        return traj
