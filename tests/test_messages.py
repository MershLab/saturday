from __future__ import annotations

from saturday.llm.client import parse_hermes_tool_calls
from saturday.prompts.templates import split_reasoning, to_chatml
from saturday.types import Message, ToolCall


def test_toolcall_roundtrip():
    tc = ToolCall(id="abc", name="shell", arguments={"command": "echo hi"})
    raw = tc.to_openai()
    parsed = ToolCall.from_openai(raw)
    assert parsed.name == "shell"
    assert parsed.arguments == {"command": "echo hi"}


def test_message_parses_think_blocks():
    msg = Message.from_openai({"role": "assistant", "content": "<think>let me check</think>The answer is 4."})
    assert msg.reasoning == "let me check"
    assert msg.content == "The answer is 4."


def test_hermes_xml_parsing():
    text = 'I will use the tool.\n<tool_call>\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n</tool_call>'
    calls = parse_hermes_tool_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "a.txt"}


def test_split_reasoning_unclosed():
    reasoning, remainder = split_reasoning("<think>partial thought")
    assert reasoning == "partial thought"
    assert remainder == ""


def test_chatml_renders_tools_and_reasoning():
    out = to_chatml(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "reasoning": "plan it",
                "tool_calls": [{"function": {"name": "glob", "arguments": '{"pattern": "**/*.py"}'}}],
            },
            {"role": "tool", "name": "glob", "content": "x.py"},
        ]
    )
    assert "<|im_start|>system\nsys<|im_end|>" in out
    assert "<scratch_pad>plan it</scratch_pad>" in out
    assert '<tool_call>\n{"name": "glob"' in out
    assert "<tool_response>\nx.py\n</tool_response>" in out
    assert out.endswith("<|im_start|>assistant\n")
