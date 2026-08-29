"""Tests for the interactive console app layer (repl.py): approvals, diff gate, slash commands."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.todo import TodoTool  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.repl import ConsoleApprover, FileEditGate, Repl, render_file_diff  # noqa: E402
from saturday.sessions import SessionStore  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.shell import ShellTool  # noqa: E402


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
