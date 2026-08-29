from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.loop import AgentLoop  # noqa: E402
from saturday.agent.memory import WorkingMemory, estimate_tokens  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.files import ReadFile, WriteFile  # noqa: E402


def build_registry(tmp_path: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(WriteFile(root=str(tmp_path)))
    reg.register(ReadFile(root=str(tmp_path)))
    return reg


def test_loop_full_cycle_with_tools(tmp_path: Path):
    model = make_scripted_model(
        [
            {
                "reasoning": "need to create the file first",
                "tool_calls": [{"name": "write_file", "arguments": {"path": "out.txt", "content": "Saturday online"}}],
            },
            {
                "reasoning": "verify by reading back",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "out.txt"}}],
            },
            {"content": "Created and verified out.txt containing 'Saturday online'."},
        ]
    )
    loop = AgentLoop(model, build_registry(tmp_path), max_steps=5)
    traj = loop.run("system-prompt", "create out.txt with Saturday online")

    assert traj.stop_reason == "done"
    assert traj.final_answer and "verified" in traj.final_answer.lower()
    assert len(traj.steps) == 3
    assert (tmp_path / "out.txt").read_text() == "Saturday online"
    assert model.calls[0]["messages"][0]["role"] == "system"
    assert any(m.get("role") == "tool" for m in model.calls[1]["messages"])


def test_loop_max_steps_stop(tmp_path: Path):
    # distinct calls each step: the stall detector must not fire, max_steps does
    turns = [
        {"reasoning": "looping", "tool_calls": [{"name": "read_file", "arguments": {"path": f"missing-{i}.txt"}}]}
        for i in range(4)
    ] + [{"content": ""}]
    model = make_scripted_model(turns)
    loop = AgentLoop(model, build_registry(tmp_path), max_steps=4)
    traj = loop.run("sys", "do the thing")
    assert traj.stop_reason == "max_steps"


def test_compaction_preserves_tail_and_pins_summary(tmp_path: Path):
    model = make_scripted_model([{"content": "filler"}])
    loop = AgentLoop(
        model,
        build_registry(tmp_path),
        max_steps=1,
        compact_above_tokens=10,
    )
    history = [{"role": "user", "content": f"turn {i} " + "y" * 200} for i in range(12)]
    before_tail = history[-6:]
    loop._compact(history)
    assert len(history) == 7
    assert history[0]["role"] == "user" and "compacted" in history[0]["content"]
    assert history[1:] == before_tail
    assert len(loop.memory) == 1
    assert loop.memory.items[0].kind == "compaction-summary"
    assert "turn 5" in loop.memory.items[0].text


def test_memory_render_truncation():
    mem = WorkingMemory(max_chars=50)
    for i in range(20):
        mem.add("fact", f"fact number {i} with some padding text")
    rendered = mem.render()
    assert len(rendered) <= 50 + 40
    assert rendered.startswith("[") or "\n[" in rendered


def test_estimate_tokens():
    assert estimate_tokens("") == 1
    assert estimate_tokens("a" * 400) == 100
