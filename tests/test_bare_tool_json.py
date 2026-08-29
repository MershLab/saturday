"""Bare-JSON tool-call absorption for half-compliant local models.

qwen2.5-coder via ollama emits tool calls as a bare JSON document (no
<tool_call> wrapper). The Hermes parser used to ignore it, the loop read the
JSON blob as a final answer, and the run ended without acting. The parser now
accepts a whole-message tool-call-shaped JSON object (or list) when no
<tool_call> tags are present.
"""
from __future__ import annotations

from saturday.llm.client import parse_hermes_tool_calls


def test_bare_json_object_becomes_tool_call() -> None:
    text = '{\n  "name": "write_file",\n  "arguments": {"path": "launch.txt", "content": "Saturday online"}\n}'
    calls = parse_hermes_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "write_file"
    assert calls[0].arguments["content"] == "Saturday online"


def test_bare_json_list_becomes_tool_calls() -> None:
    text = '[{"name": "a", "arguments": {}}, {"name": "b", "parameters": {"x": 1}}]'
    calls = parse_hermes_tool_calls(text)
    assert [c.name for c in calls] == ["a", "b"]
    assert calls[1].arguments == {"x": 1}


def test_wrapped_calls_still_win() -> None:
    text = '<scratch_pad>plan</scratch_pad>\n<tool_call>{"name": "t", "arguments": {"k": "v"}}</tool_call>'
    calls = parse_hermes_tool_calls(text)
    assert len(calls) == 1 and calls[0].name == "t"


def test_plain_prose_not_a_tool_call() -> None:
    assert parse_hermes_tool_calls("The answer is 42.") == []


def test_non_tool_json_not_a_tool_call() -> None:
    assert parse_hermes_tool_calls('{"result": 5, "status": "ok"}') == []


def test_string_arguments_decoded() -> None:
    text = '{"name": "shell", "arguments": "{\\"cmd\\": \\"ls\\"}"}'
    calls = parse_hermes_tool_calls(text)
    assert calls[0].arguments == {"cmd": "ls"}


def test_response_tag_wrapped_call_absorbed() -> None:
    # qwen2.5-coder imitates the protocol's response tag for its own call
    text = (
        "The directory contents are listed. Next, I will check for the file.\n\n"
        '<tool_response>\n{"name": "glob", "arguments": {"pattern": "launch.txt"}}\n</tool_response>'
    )
    calls = parse_hermes_tool_calls(text)
    assert len(calls) == 1 and calls[0].name == "glob"
    assert calls[0].arguments == {"pattern": "launch.txt"}


def test_echoed_result_shape_not_absorbed() -> None:
    # our rendered tool results use name+content (no arguments) — must NOT
    # become phantom calls when the model echoes them
    text = (
        '<tool_response>\n{"name": "shell", "content": "file1\\nfile2"}\n</tool_response>\n'
        "Done."
    )
    assert parse_hermes_tool_calls(text) == []

def test_scratch_pad_then_bare_call_absorbed() -> None:
    # the observed qwen2.5-coder pattern: reasoning block, then the call as a
    # trailing bare JSON document
    text = (
        "<scratch_pad>\nCurrent goal: read the csv\n</scratch_pad>\n\n"
        '{"name": "read_file", "arguments": {"path": "data/sales_q1.csv"}}'
    )
    calls = parse_hermes_tool_calls(text)
    assert len(calls) == 1 and calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "data/sales_q1.csv"}


def test_prose_after_json_disqualifies() -> None:
    # a model DISCUSSING JSON (example embedded in an answer) must not fire a call
    text = 'Here is the JSON you asked for: {"name": "read_file", "arguments": {"path": "x"}} - modify as needed.'
    assert parse_hermes_tool_calls(text) == []


def test_non_call_json_at_end_stays_prose() -> None:
    assert parse_hermes_tool_calls('The config is: {"result": 5}') == []
