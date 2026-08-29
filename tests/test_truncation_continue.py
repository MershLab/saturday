"""Truncated thinking must not terminate the run as a bogus "done".

Thinking models (qwen3, deepseek-r1) can burn the entire max_tokens budget
mid-thought and return truncated plan text with no tool call. The loop used to
accept that text as the final answer (stop_reason "done", task silently
failed); it must instead keep the partial turn in history, nudge, and continue.
"""
from __future__ import annotations

from pathlib import Path

from fakes import make_scripted_model
from saturday.agent.loop import AgentLoop
from saturday.tools.files import ReadFile, WriteFile
from saturday.tools.base import ToolRegistry


def _registry(root: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ReadFile(root=str(root)))
    reg.register(WriteFile(root=str(root)))
    return reg


def test_truncated_plan_continues_and_completes(tmp_path: Path) -> None:
    model = make_scripted_model(
        [
            # thinking burned the budget: truncated plan, no tool call
            {"content": "Plan:\n1. create launch.txt\n2. verify contents", "finish_reason": "length"},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "launch.txt", "content": "Saturday online"}}]},
            {"content": "created and verified"},
        ]
    )
    loop = AgentLoop(model, _registry(tmp_path), max_steps=4)
    traj = loop.run("sys", "create launch.txt containing: Saturday online")

    assert traj.stop_reason == "done"
    assert traj.final_answer == "created and verified"
    # the truncated plan was kept in history, followed by the continuation nudge
    second_call = model.calls[1]["messages"]
    assistant_turns = [m for m in second_call if m.get("role") == "assistant"]
    assert any("Plan:" in str(m.get("content")) for m in assistant_turns)
    assert any("[response truncated at the token limit]" in str(m.get("content")) for m in second_call)


def test_empty_response_nudge_unchanged() -> None:
    model = make_scripted_model(
        [
            {"content": ""},
            {"content": "ok"},
        ]
    )
    loop = AgentLoop(model, ToolRegistry(), max_steps=3)
    traj = loop.run("sys", "goal")
    assert traj.stop_reason == "done"
    second_call = model.calls[1]["messages"]
    assert any("[empty response] Continue pursuing the goal." in str(m.get("content")) for m in second_call)


def test_truncated_turn_does_not_end_run() -> None:
    # a truncated answer is never terminal: the loop nudges and the next
    # (complete) response becomes the final answer
    model = make_scripted_model(
        [
            {"content": "42", "finish_reason": "length"},
            {"content": "The answer is 42"},
        ]
    )
    loop = AgentLoop(model, ToolRegistry(), max_steps=4)
    traj = loop.run("sys", "what is 6*7?")
    assert traj.stop_reason == "done"
    assert traj.final_answer == "The answer is 42"


def test_echoed_tool_response_is_not_a_final_answer(tmp_path: Path) -> None:
    # small local models sometimes echo the tool-result block verbatim as
    # their whole reply; that is noise, so the loop nudges instead of ending
    model = make_scripted_model(
        [
            {"tool_calls": [{"name": "read_file", "arguments": {"path": "notes.txt"}}]},
            {"content": '<tool_response>\n{"name": "read_file", "content": "hi"}\n</tool_response>'},
            {"content": "the file says hi"},
        ]
    )
    loop = AgentLoop(model, _registry(tmp_path), max_steps=4)
    traj = loop.run("sys", "read notes.txt and tell me what it says")
    assert traj.stop_reason == "done"
    assert traj.final_answer == "the file says hi"
    # the echo turn stayed in history with a nudge after it
    third_call = model.calls[2]["messages"]
    assert any("[empty response]" in str(m.get("content")) for m in third_call)


def test_real_answer_containing_quoted_response_still_wins(tmp_path: Path) -> None:
    model = make_scripted_model(
        [
            {"content": 'The tool said <tool_response>{"name": "x", "content": "ok"}</tool_response> so all good.'},
        ]
    )
    loop = AgentLoop(model, _registry(tmp_path), max_steps=2)
    traj = loop.run("sys", "check")
    assert traj.stop_reason == "done"
    assert "all good" in traj.final_answer
