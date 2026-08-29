from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.loop import AgentLoop, enforce_message_invariants  # noqa: E402
from saturday.agent.todo import TodoTool  # noqa: E402
from saturday.plugins import core_plugin, install_plugins, make_plugin  # noqa: E402
from saturday.prompts.templates import render_tool_response, split_reasoning  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.goals import build_goal_tools  # noqa: E402
from saturday.tools.jobs import JobManager  # noqa: E402


def test_scratch_pad_parsing():
    reasoning, rest = split_reasoning("<scratch_pad>plan first</scratch_pad>Answer: 7")
    assert reasoning == "plan first"
    assert rest == "Answer: 7"


def test_tool_response_rendering_and_retry_hint():
    ok = render_tool_response("glob", True, "a.py")
    assert "<tool_response>" in ok and '"content"' in ok and "error" not in ok
    bad = render_tool_response("shell", False, "boom")
    assert '"error"' in bad and "again with correct arguments" in bad


def test_message_invariants():
    history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"function": {"name": "x", "arguments": "{}"}}]},
        {"role": "assistant", "content": "a2", "tool_calls": [{"function": {"name": "y", "arguments": "{}"}}]},
        {"role": "user", "content": "u2"},
        {"role": "user", "content": "u3"},
    ]
    out = enforce_message_invariants(history)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "user"]
    assert len(out[1]["tool_calls"]) == 2
    assert "u2" in out[2]["content"] and "u3" in out[2]["content"]


class SlowTool:
    description = "sleeps"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self.delay = delay

    def run(self, args: dict) -> tuple[bool, str]:
        time.sleep(self.delay)
        return True, self.name


def test_concurrent_tool_execution(tmp_path: Path):
    reg = ToolRegistry()
    for i in range(4):
        reg.register(SlowTool(f"slow_{i}", 0.25))
    model = make_scripted_model(
        [
            {"tool_calls": [
                {"name": "slow_0", "arguments": {}},
                {"name": "slow_1", "arguments": {}},
                {"name": "slow_2", "arguments": {}},
                {"name": "slow_3", "arguments": {}},
            ]},
            {"content": "all four finished"},
        ]
    )
    model = make_scripted_model(
        [
            {"tool_calls": [
                {"name": "slow", "arguments": {}},
                {"name": "slow", "arguments": {}},
                {"name": "slow", "arguments": {}},
                {"name": "slow", "arguments": {}},
            ]},
            {"content": "all four finished"},
        ]
    )
    loop = AgentLoop(model, reg, max_steps=2)
    start = time.time()
    traj = loop.run("sys", "run four slow tools")
    elapsed = time.time() - start
    assert traj.stop_reason == "done"
    assert len(traj.steps[0].results) == 4
    assert elapsed < 0.9, f"tools appear sequential: {elapsed:.2f}s"


def test_pre_tool_call_block():
    reg = ToolRegistry()
    reg.register(SlowTool("slow", 0.0))
    model = make_scripted_model([{"tool_calls": [{"name": "slow", "arguments": {}}]}, {"content": "done"}])
    from saturday.agent.loop import LoopHooks

    loop = AgentLoop(model, reg, max_steps=2, hooks=LoopHooks(pre_tool_call=lambda n, a: "blocked by policy"))
    traj = loop.run("sys", "try the tool")
    assert traj.steps[0].results[0].ok is False
    assert "blocked by policy" in traj.steps[0].results[0].error


def test_todo_tool_flow():
    todo = TodoTodo = TodoTool()
    ok, _ = todo.run({"action": "write", "steps_text": "one\ntwo\nthree"})
    assert ok
    ok, out = todo.run({"action": "mark", "index": 2})
    assert ok and "progress 1/3" in out
    ok, out = todo.run({"action": "read"})
    assert ok and "[x] two" in out and "progress: 1/3" in out


def test_goal_tools_lifecycle():
    _, tools = build_goal_tools()
    create, get, update = tools
    ok, _ = create.run({"text": "ship Saturday"})
    assert ok
    ok, out = get.run({})
    assert ok and "active" in out
    ok, out = update.run({"action": "note", "note": "halfway"})
    assert ok and "halfway" in out
    ok, _ = update.run({"action": "complete"})
    assert ok and "done" in get.run({})[1]
    ok, out = create.run({"text": "second goal"})
    assert ok and "goal created" in out


def test_job_manager_background_shell(tmp_path: Path):
    mgr = JobManager()
    jid = mgr.start("python -c \"import time; print('boot'); time.sleep(0.6); print('done')\"", workdir=str(tmp_path))
    job = mgr.get(jid)
    deadline = time.time() + 5
    while time.time() < deadline and "done" not in job.tail():
        time.sleep(0.05)
    assert "boot" in job.tail() and "done" in job.tail()


def test_plugin_install_persona_and_conflict():
    persona: list[str] = []
    reg = ToolRegistry()
    p1 = core_plugin(None)
    p2 = make_plugin("extra", [], description="d", persona_sections=["# Extra"])
    install_plugins(reg, [p1, p2], persona_out=persona)
    assert "read_file" in reg.names() and "# Extra" in "\n".join(persona)
