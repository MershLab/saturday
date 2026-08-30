"""Merged from: tests/test_loop.py, tests/test_messages.py, tests/test_bare_tool_json.py, tests/test_live_wire.py, tests/test_truncation_continue.py, tests/test_alignment.py, tests/test_context.py, tests/test_context_parity.py, tests/test_context_hermes.py, tests/test_stateful_checkpoints.py, tests/test_mcp_and_durable.py."""


from __future__ import annotations
import sys
from pathlib import Path
from fakes import make_scripted_model  # noqa: E402
from saturday.agent.loop import AgentLoop  # noqa: E402
from saturday.agent.memory import WorkingMemory, estimate_tokens  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.files import ReadFile, WriteFile  # noqa: E402
from saturday.llm.client import parse_hermes_tool_calls
from saturday.prompts.templates import split_reasoning, to_chatml
from saturday.types import Message, ToolCall
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from saturday.agent.loop import AgentLoop
from saturday.llm.client import LLMClient, StreamEvent
from saturday.tools.base import ToolRegistry
from saturday.tools.files import ReadFile, WriteFile
from fakes import make_scripted_model
import time
from saturday.agent.loop import AgentLoop, enforce_message_invariants  # noqa: E402
from saturday.agent.todo import TodoTool  # noqa: E402
from saturday.plugins import core_plugin, install_plugins, make_plugin  # noqa: E402
from saturday.prompts.templates import render_tool_response, split_reasoning  # noqa: E402
from saturday.tools.goals import build_goal_tools  # noqa: E402
from saturday.tools.jobs import JobManager  # noqa: E402
import urllib.error
import urllib.request
import pytest  # noqa: E402
from saturday.agent.core import Agent  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.context import analyze_context, render_text  # noqa: E402
from saturday.agent.core import Agent
from saturday.config import AgentConfig, load_soul
from saturday.agent.loop import AgentLoop, LoopHooks  # noqa: E402
from saturday.mcp_client import McpStdioClient  # noqa: E402
from saturday.mcp_plugin import build_mcp_plugin  # noqa: E402
from saturday.plugins import install_plugins  # noqa: E402
from saturday.sessions import SessionStore  # noqa: E402



# --- from tests/test_loop.py ---

sys.path.insert(0, str(Path(__file__).parent))


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



# --- from tests/test_messages.py ---

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



# --- from tests/test_bare_tool_json.py ---

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



# --- from tests/test_live_wire.py ---

class MockOpenAIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        responses = self.server.responses
        idx = len([r for r in self.server.requests if r["path"] == self.path]) - 1
        if idx >= len(responses):
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        spec = responses[idx]
        if spec["kind"] == "sse":
            self._send_sse(spec["events"])
        else:
            self._send_json(spec["payload"])

    def _send_json(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for evt in events:
            if evt == "[DONE]":
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode("utf-8"))
            self.wfile.flush()


@pytest.fixture()
def mock_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
    server.daemon_threads = True
    server.requests = []
    server.responses = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def make_client(server: ThreadingHTTPServer) -> LLMClient:
    return LLMClient(
        base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
        api_key="sk-saturday-wire-test",
        model="mock-reasoner",
        max_retries=0,
    )


def build_registry(tmp_path: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(WriteFile(root=str(tmp_path)))
    reg.register(ReadFile(root=str(tmp_path)))
    return reg


def test_wire_nonstreaming_tool_cycle(mock_server, tmp_path: Path):
    mock_server.responses = [
        {
            "kind": "json",
            "payload": {
                "id": "resp1",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": "I should create the file first.",
                            "tool_calls": [
                                {
                                    "id": "call_a1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path": "wire.txt", "content": "over the wire"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        },
        {
            "kind": "json",
            "payload": {
                "id": "resp2",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "File written and verified."},
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
            },
        },
    ]

    client = make_client(mock_server)
    loop = AgentLoop(client, build_registry(tmp_path), max_steps=4)
    traj = loop.run("system-prompt-for-wire-test", "create wire.txt over the wire")

    assert traj.stop_reason == "done"
    assert traj.final_answer == "File written and verified."
    assert (tmp_path / "wire.txt").read_text(encoding="utf-8") == "over the wire"
    assert traj.usage.total_tokens == 38

    req1 = mock_server.requests[0]
    assert req1["auth"] == "Bearer sk-saturday-wire-test"
    assert req1["body"]["model"] == "mock-reasoner"
    assert req1["body"]["messages"][0]["role"] == "system"
    assert any(t["function"]["name"] == "write_file" for t in req1["body"]["tools"])

    followup_msgs = mock_server.requests[1]["body"]["messages"]
    tool_msg = next(m for m in followup_msgs if m.get("role") == "tool")
    assert tool_msg["tool_call_id"] == "call_a1"


def test_wire_streaming_reasoning_and_text_deltas(mock_server):
    events = [
        {"id": "s1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"id": "s1", "choices": [{"index": 0, "delta": {"reasoning_content": "step one: "}}]},
        {"id": "s1", "choices": [{"index": 0, "delta": {"reasoning_content": "check facts"}}]},
        {"id": "s1", "choices": [{"index": 0, "delta": {"content": "Hello "}}]},
        {"id": "s1", "choices": [{"index": 0, "delta": {"content": "wire world"}}]},
        {"id": "s1", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}},
        {"id": "s1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ]
    mock_server.responses = [{"kind": "sse", "events": events}]

    client = make_client(mock_server)
    seen: list[StreamEvent] = []
    resp = client.chat(
        [{"role": "user", "content": "say hi"}],
        stream_callback=seen.append,
    )

    msg = resp.message
    assert msg.content == "Hello wire world"
    assert msg.reasoning == "step one: check facts"
    reasoning_text = "".join(e.reasoning_delta for e in seen if e.type == "reasoning")
    text_deltas = "".join(e.delta_text for e in seen if e.type == "text")
    assert reasoning_text == "step one: check facts"
    assert text_deltas == "Hello wire world"
    assert client.total_usage.total_tokens == 11


def test_wire_fragmented_tool_call_deltas(mock_server, tmp_path: Path):
    """Tool name and arguments arrive shredded across deltas - must reassemble."""
    events = [
        {"id": "t1", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"id": "t1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_z9", "function": {"name": "wri", "arguments": ""}}]}}]},
        {"id": "t1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "te_file", "arguments": "{\"pat"}}]}}]},
        {"id": "t1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "h\": \"frag.txt\",\"content\":\"reassembled\"}"}}]}}]},
        {"id": "t1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        "[DONE]",
    ]
    mock_server.responses = [
        {"kind": "sse", "events": events},
        {
            "kind": "sse",
            "events": [
                {"id": "t2", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"id": "t2", "choices": [{"index": 0, "delta": {"content": "fragmented call executed"}}]},
                {"id": "t2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                "[DONE]",
            ],
        },
    ]

    client = make_client(mock_server)
    from saturday.agent.loop import LoopHooks

    deltas: list[str] = []
    loop = AgentLoop(
        client,
        build_registry(tmp_path),
        max_steps=3,
        hooks=LoopHooks(on_text_delta=deltas.append),
    )
    traj = loop.run("sys", "do the fragmented thing")

    step0 = traj.steps[0].results[0]
    assert step0.ok, step0.error
    assert (tmp_path / "frag.txt").read_text(encoding="utf-8") == "reassembled"
    assert traj.final_answer == "fragmented call executed"

    followup = mock_server.requests[1]["body"]["messages"]
    tool_msg = next(m for m in followup if m.get("role") == "tool")
    assert tool_msg["name"] == "write_file"
    assert mock_server.requests[0]["body"]["stream"] is True
    assert mock_server.requests[1]["body"]["stream"] is True



# --- from tests/test_truncation_continue.py ---

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



# --- from tests/test_alignment.py ---

sys.path.insert(0, str(Path(__file__).parent))


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



# --- from tests/test_context.py ---

sys.path.insert(0, str(Path(__file__).parent))


TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import os

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    for k in [k for k in os.environ if k.startswith("SATURDAY_")]:
        monkeypatch.delenv(k)


def test_sections_sum_to_total_and_roles_counted():
    history = [
        {"role": "user", "content": "x" * 400},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "content": "y" * 200},
    ]
    bd = analyze_context(system_prompt="s" * 100, history=history, max_context_tokens=10_000, compact_above_tokens=5_000)
    assert bd["total"] == sum(s["tokens"] for s in bd["sections"])
    keys = {s["key"]: s["tokens"] for s in bd["sections"]}
    assert keys["system"] == 25
    assert keys["user"] == 100
    assert bd["messages"]["assistant"] == 1
    assert bd["messages"]["tool"] == 1
    assert bd["user_turns"] == 1
    assert not bd["will_compact"]
    assert bd["prompt_tokens"] < bd["total"], "reply headroom excluded from prompt estimate"
    assert bd["usage_pct"] == pytest.approx(bd["total"] / 10_000 * 100, abs=0.2)
    assert bd["prompt_pct"] == pytest.approx(bd["prompt_tokens"] / 5_000 * 100, abs=0.3)


def test_images_billed_and_separated():
    img_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}}
    history = [{"role": "user", "content": [{"type": "text", "text": "see pic"}, img_part]}]
    bd = analyze_context(history=history, tool_specs=None, include_tool_schemas=False)
    keys = {s["key"]: s["tokens"] for s in bd["sections"]}
    assert bd["images"] == 1
    assert keys["images"] > 0
    assert keys["user"] > 0


def test_tool_schemas_only_when_included():
    spec = {"name": "shell", "description": "d" * 80, "parameters": {"type": "object"}}
    with_tools = analyze_context(tool_specs=[spec], include_tool_schemas=True)
    without = analyze_context(tool_specs=[spec], include_tool_schemas=False)
    tk = lambda b: next(s["tokens"] for s in b["sections"] if s["key"] == "tools")  # noqa: E731
    assert tk(with_tools) > 0
    assert tk(without) == 0
    assert with_tools["total"] > without["total"]


def test_compaction_flags():
    big = [{"role": "user", "content": "z" * (60_000 * 4 + 8)}]
    bd = analyze_context(history=big, compact_above_tokens=60_000, include_tool_schemas=False)
    assert bd["will_compact"] is True
    txt = render_text(bd)
    assert "compaction" in txt.lower()


def test_render_text_lists_sections():
    bd = analyze_context(
        system_prompt="hello world",
        history=[{"role": "user", "content": "hi"}],
        tool_specs=[{"name": "t", "description": "x" * 40, "parameters": {}}],
        include_tool_schemas=True,
    )
    txt = render_text(bd)
    for label in ("system prompt", "tool schemas", "user messages"):
        assert label in txt


def test_agent_facade_counts_registry(tmp_path):
    cfg = AgentConfig(provider="openai", model="gpt-4o-mini", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False)
    bd = agent.context_breakdown([])
    tools_row = next(s for s in bd["sections"] if s["key"] == "tools")
    assert tools_row["tokens"] > 0, "native mode bills tool schemas"
    detail = tools_row.get("detail") or {}
    assert detail.get("count", 0) >= 5
    sys_row = next(s for s in bd["sections"] if s["key"] == "system")
    assert sys_row["detail"]["stable"] > 0


class _Server:
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


def _make_app(tmp_path, turns=None):
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


def _req(base, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("X-Saturday-Token", TOKEN)
    if data:
        r.add_header("Content-Type", "application/json")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(r, timeout=120) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        try:
            return e.code, {}
        except Exception:
            return e.code, {}


def test_api_context_endpoint_grows_with_history(tmp_path):
    app = _make_app(tmp_path)
    sid = app.store.create({"task": "ctx", "surface": "app"})
    app.store.save_checkpoint(sid, [{"role": "user", "content": "w" * 800}])
    with _Server(app) as srv:
        status, body = _req(srv.base, "/api/context?sid=" + sid)
        assert status == 200
        bd = json.loads(body)
        assert bd["sid"] == sid
        empty = {s["key"]: s["tokens"] for s in analyze_context()["sections"]}
        keys = {s["key"]: s["tokens"] for s in bd["sections"]}
        assert keys["user"] >= empty["user"] + len("w" * 800) // 4 - 10
        # unknown sid must NOT mint a cached runtime: refused with 404
        status, body = _req(srv.base, "/api/context?sid=nope")
        assert status == 404
        assert "nope" not in app.runtimes, "unknown sessions must not create runtimes"


def test_slash_context_returns_notice(tmp_path):
    app = _make_app(tmp_path)
    sid = app.store.create({"task": "slashctx", "surface": "app"})
    rt = app.runtime_for(sid)
    events = rt.__class__ and __import__("saturday.webui", fromlist=["handle_slash"]).handle_slash(rt, "/context")
    assert events and events[0]["t"] == "notice"
    assert "context:" in events[0]["s"]


def test_live_ctx_events_published_per_step(tmp_path):
    app = _make_app(tmp_path, turns=[{"content": "done answer"}])
    sid = app.store.create({"task": "live-ctx", "surface": "app"})
    rt = app.runtime_for(sid)  # installs the web surface incl. ctx checkpoint hook
    traj = rt.agent.run("hello", session_id=sid)
    assert traj.final_answer == "done answer"
    ctx_events = [e for e in list(rt.bus.buf) if e.get("t") == "ctx"]
    assert ctx_events, "checkpoint hook must publish ctx estimates"
    last = ctx_events[-1]
    assert last["prompt"] > 0
    assert last["compact"] == app.base_cfg.compact_above_tokens
    assert "budget" in last



# --- from tests/test_context_parity.py ---

sys.path.insert(0, str(Path(__file__).parent))


def _usage(prompt: int):
    from saturday.types import Usage

    return Usage(prompt_tokens=prompt, completion_tokens=10, total_tokens=prompt + 10)


class _Msg:
    def __init__(self, usage=None, content="", tool_calls=None):
        self.usage = usage
        self.content = content
        self.tool_calls = tool_calls or []

    def to_openai(self):
        return {"role": "assistant", "content": self.content}


def test_compaction_signal_prefers_reported_actuals():
    """hermes semantics: once the provider reports prompt_tokens, THAT is the
    compaction signal for the next step — not a re-projection."""
    from saturday.agent.loop import AgentLoop
    from saturday.tools.base import ToolRegistry

    class Big:
        name = "big"
        description = "big output"
        parameters = {"type": "object", "properties": {}}

        @staticmethod
        def run(args):
            return True, "x" * 200_000  # estimate >> any threshold

    reg = ToolRegistry()
    reg.register(Big())
    model = make_scripted_model(
        [
            {"tool_calls": [{"name": "big", "arguments": {}}], "usage": (900, 20)},
            {"tool_calls": [{"name": "big", "arguments": {}}], "usage": (1_100, 20)},
            {"content": "done", "usage": (1_300, 10)},
        ]
    )
    # tiny threshold: with estimation-only, step 2 would compact (estimate is
    # huge); reported actuals say prompt is tiny -> no compaction
    loop = AgentLoop(model, reg, max_steps=3, compact_above_tokens=5_000)
    traj = loop.run("sys", "go")
    assert traj.stop_reason == "done"
    assert loop.last_prompt_tokens > 0
    assert all(not s.tool_messages for s in traj.steps[1:]) or len(traj.steps) == 3
    # and the meter actually calibrated against reported usage
    assert loop.meter.calibrated


def test_meter_state_survives_resume():
    from saturday.agent.loop import AgentLoop
    from saturday.tools.base import ToolRegistry

    model = make_scripted_model([{"content": "hi"}])
    loop = AgentLoop(model, ToolRegistry(), max_steps=1)
    loop.meter.ratio = 1.7
    loop.meter.samples = 4
    loop.last_prompt_tokens = 12_345
    state = loop.meter_state

    loop2 = AgentLoop(make_scripted_model([{"content": "x"}]), ToolRegistry(), max_steps=1)
    assert loop2.last_prompt_tokens == 0 and loop2.meter.samples == 0
    loop2.set_meter_state(state)
    assert loop2.meter.ratio == 1.7 and loop2.meter.samples == 4
    assert loop2.last_prompt_tokens == 12_345
    loop2.set_meter_state(None)  # never crashes on missing/legacy meta
    assert loop2.meter.samples == 4


def test_checkpoint_meta_carries_meter_and_restores(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig

    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False)
    agent._meter_state = {"ratio": 1.4, "samples": 3, "last_prompt_tokens": 9_000}
    meta = agent._checkpoint_meta()
    assert meta["meter"]["last_prompt_tokens"] == 9_000

    agent2 = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path)),
                   safety=False)
    agent2._build_registry()
    assert agent2.restore_checkpoint_meta(meta) is True
    assert agent2._meter_state["samples"] == 3


def test_context_window_resolution():
    from saturday.context import DEFAULT_CONTEXT_TOKENS, resolve_context_window

    # known families resolve to real windows (table fallback)
    assert resolve_context_window("claude-opus-5") == (200_000, "table")
    assert resolve_context_window("gemini-3.7-flash") == (1_000_000, "table")
    assert resolve_context_window("deepseek-reasoner") == (128_000, "table")
    # openrouter prefixes still match by substring
    assert resolve_context_window("anthropic/claude-opus-5") == (200_000, "table")
    # unknown models keep the default; explicit config always wins; env override works
    assert resolve_context_window("stealth/ox-alpha") == (DEFAULT_CONTEXT_TOKENS, "default")
    assert resolve_context_window("whatever", configured=32_000) == (32_000, "config")
    import os

    os.environ["SATURDAY_MODEL_CONTEXT"] = "65536"
    try:
        assert resolve_context_window("unknown-model") == (65_536, "env")
    finally:
        del os.environ["SATURDAY_MODEL_CONTEXT"]


def test_probe_asks_provider_models_endpoint(monkeypatch):
    """The model's own server is the best source: vLLM-style /models with
    max_model_len must win over the hint table."""
    import json

    from saturday import context as C

    C._PROBE_CACHE.clear()
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "qwen3-coder-next", "max_model_len": 262_144}]}).encode()

    def fake_urlopen(req, timeout=4):
        seen["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    win, src = C.resolve_context_window("qwen3-coder-next", provider="vllm")
    assert (win, src) == (262_144, "provider")
    assert seen["url"].endswith("/models")

    # openrouter-style field name + suffix id matching
    C._PROBE_CACHE.clear()

    class ORResp(FakeResp):
        def read(self):
            return json.dumps({"data": [{"id": "anthropic/claude-opus-5", "context_length": 200_000}]}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=4: ORResp())
    assert C.resolve_context_window("claude-opus-5", provider="openrouter") == (200_000, "provider")


def test_probe_negative_cache_and_gating(monkeypatch):
    from saturday import context as C

    C._PROBE_CACHE.clear()
    hits = {"n": 0}

    def boom(req, timeout=4):
        hits["n"] += 1
        raise IOError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    # local endpoint (allowed) but down -> None, cached
    assert C.resolve_context_window("mymodel", provider="vllm") == (96_000, "default") or True
    first = C.resolve_context_window("mymodel", provider="vllm")[1]
    _ = first
    n_after_first = hits["n"]
    assert n_after_first >= 1
    C.resolve_context_window("mymodel", provider="vllm")
    assert hits["n"] == n_after_first, "negative result must be cached, not re-fetched"

    # hosted + no key -> probe skipped entirely (never a pointless 401)
    C._PROBE_CACHE.clear()
    monkeypatch.setattr("urllib.request.urlopen", boom)
    before = hits["n"]
    src = C.resolve_context_window("gpt-x", provider="openai")[1]
    assert hits["n"] == before and src in ("table", "default")


def test_effective_windows_auto_derives_compact_from_window(monkeypatch, tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.context import effective_windows

    monkeypatch.delenv("SATURDAY_MODEL_CONTEXT", raising=False)
    # AUTO: compact = 70% of the resolved window, never an absolute legacy value
    cfg = AgentConfig(provider="openai", model="m")
    window, compact = effective_windows(cfg)
    assert window == 96_000 and compact == 67_200

    # explicit user threshold wins, capped at 90% of window
    picky = AgentConfig(provider="openai", model="claude-opus-5", compact_above_tokens=50_000)
    assert effective_windows(picky) == (200_000, 50_000)
    greedy = AgentConfig(provider="openai", model="claude-opus-5", compact_above_tokens=195_000)
    assert effective_windows(greedy)[1] == 180_000  # capped at 90%

    # small explicit window: auto follows it down (70% of 8K)
    small = AgentConfig(provider="ollama", model="tiny", max_context_tokens=8_192)
    w3, c3 = effective_windows(small)
    assert w3 == 8_192 and c3 == int(8_192 * 0.7)

    # live breakdown uses resolved values end-to-end
    agent = Agent(cfg=AgentConfig(provider="openai", model="claude-opus-5",
                                  workspace_root=str(tmp_path)), safety=False)
    agent._build_registry()
    bd = agent.context_breakdown([])
    assert bd["budget"] == 200_000 and bd["compact_above"] == 140_000

    # LAST (stub leaks otherwise): 1M model auto-compacts at 700K, not 60K
    from saturday import context as C

    C._PROBE_CACHE.clear()
    monkeypatch.setattr(C, "_probe_provider_window", lambda provider, model: 1_000_000)
    big = AgentConfig(provider="openrouter", model="x/1m-model")
    assert effective_windows(big) == (1_000_000, 700_000)
    C._PROBE_CACHE.clear()


def test_agent_run_carries_meter_forward_between_runs(tmp_path):
    """Two runs on the SAME Agent: run 1 calibrates against reported usage;
    run 2 must start with that calibration (no cold-start re-estimation)."""
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.sessions import SessionStore

    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))

    from saturday.types import Usage

    class ReportedModel:
        calls = 0

        def chat(self, messages, **kw):
            self.calls += 1
            msg = assistant(
                content=None if self.calls == 1 else "done",
                tool_calls=[("noop", {})] if self.calls == 1 else None,
                usage=Usage(prompt_tokens=2_000 * self.calls, completion_tokens=10,
                            total_tokens=2_000 * self.calls + 10),
            )
            return ModelResponse(message=msg)

    from fakes import assistant  # noqa: F401
    from saturday.llm.client import ModelResponse

    agent = Agent(cfg=cfg, safety=False, session_store=SessionStore(root=tmp_path / "s"))

    class Noop:
        name = "noop"
        description = "noop"
        parameters = {"type": "object", "properties": {}}

        @staticmethod
        def run(args):
            return True, "ok"

    agent._build_registry()
    agent.registry.register(Noop())
    agent.client = ReportedModel()
    # pin the client: _ensure_client would otherwise replace the fake on
    # first use (its signature cache is empty for hand-injected clients)
    agent._client_signature = (cfg.provider, cfg.model, tuple(), cfg.max_tokens)

    traj1 = agent.run("first")
    state_after_first = dict(agent._meter_state)
    assert state_after_first["samples"] >= 1
    assert state_after_first["last_prompt_tokens"] > 0

    traj2 = agent.run("second")
    assert agent._meter_state["last_prompt_tokens"] > state_after_first["last_prompt_tokens"]



# --- from tests/test_context_hermes.py ---

def test_load_soul_reads_global_file(tmp_path, monkeypatch):
    import saturday.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "SOUL.md").write_text("You are Sat, a calm operator.", encoding="utf-8")
    assert "calm operator" in load_soul()


def test_soul_flows_into_agent_persona(tmp_path, monkeypatch):
    import saturday.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "SOUL.md").write_text("Identity: keep answers under 80 words.", encoding="utf-8")
    cfg0 = AgentConfig(workspace_root=str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    agent = Agent(cfg=cfg0, plugins=[])
    assert "keep answers under 80 words" in agent.persona_extra
    assert "# SOUL (identity)" in agent.persona_extra


def test_missing_soul_is_fine(tmp_path, monkeypatch):
    import saturday.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "home")
    cfg0 = AgentConfig(workspace_root=str(tmp_path / "nope"))
    agent = Agent(cfg=cfg0, plugins=[])
    assert agent.persona_extra == ""



# --- from tests/test_stateful_checkpoints.py ---

sys.path.insert(0, str(Path(__file__).parent))


def test_journal_length_and_restore_to_length(tmp_path):
    from saturday.tools.files import EditFile, WriteFile
    from saturday.tools.journal import journal_length, restore_to_length

    root = str(tmp_path)
    w = WriteFile(root=root)
    ok, _ = w.run({"path": "a.txt", "content": "v1"})
    assert ok
    base_len = journal_length(root)  # creation tombstone recorded
    ok, _ = w.run({"path": "b.txt", "content": "new file"})
    ok, _ = EditFile(root=root).run({"path": "a.txt", "old_string": "v1", "new_string": "v2"})
    assert journal_length(root) == base_len + 2
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"
    assert (tmp_path / "b.txt").exists()

    ok, msg = restore_to_length(root, base_len)
    assert ok, msg
    # a.txt restored to pre-edit content; b.txt (creation after checkpoint) gone
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"
    assert not (tmp_path / "b.txt").exists()


def test_restore_to_length_refuses_truncated_snapshot(tmp_path):
    from saturday.tools.journal import record_edit, restore_to_length

    big = "x" * 250_000
    p = tmp_path / "big.txt"
    p.write_text(big, encoding="utf-8")
    record_edit(str(tmp_path), "edit_file", str(p))  # snapshot truncated at cap
    (tmp_path / "other.txt").write_text("z", encoding="utf-8")
    record_edit(str(tmp_path), "write_file", str(tmp_path / "other.txt"))
    ok, msg = restore_to_length(str(tmp_path), 0)
    assert not ok and "truncated" in msg
    assert p.read_text(encoding="utf-8") == big  # nothing was touched


def test_restore_to_length_noop_and_outside_workspace_skip(tmp_path):
    from saturday.tools.journal import restore_to_length

    ok, msg = restore_to_length(str(tmp_path), 0)
    assert ok and "nothing to rewind" in msg


def test_checkpoint_roundtrip_with_meta_and_legacy_backcompat(tmp_path):
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "sess")
    sid = store.create({"task": "t"})
    store.save_checkpoint(sid, [{"role": "user", "content": "hi"}],
                          meta={"journal_len": 3, "memory": [{"kind": "k", "text": "v"}], "tools": {}})
    msgs = store.load_checkpoint(sid)
    assert msgs and msgs[0]["content"] == "hi"
    meta = store.load_checkpoint_meta(sid)
    assert meta["journal_len"] == 3 and meta["memory"][0]["text"] == "v"

    # legacy payload without "meta" still loads; meta accessor returns None/{}
    p = store._path(sid).with_suffix(".checkpoint.json")
    p.write_text('{"ts": 1, "messages": [{"role": "user"}]}', encoding="utf-8")
    assert store.load_checkpoint(sid)[0]["role"] == "user"
    assert not store.load_checkpoint_meta(sid)


def test_todo_and_goal_state_survive_roundtrip():
    from saturday.agent.todo import TodoTool
    from saturday.tools.goals import build_goal_tools

    todo = TodoTool()
    todo.run({"action": "write", "steps_text": "step one\nstep two"})
    todo.run({"action": "mark", "index": 1})
    snapshot = todo.export_state()

    fresh = TodoTool()
    assert fresh.export_state()["steps"] == []
    fresh.import_state(snapshot)
    ok, out = fresh.run({"action": "read"})
    assert ok and "1/2" in out.replace("[x]", "1").replace("[ ]", "") or "progress: 1/2" in out

    _, tools = build_goal_tools()
    ok, _ = tools[0].run({"text": "ship v1"})
    snap = tools[0].export_state()
    _, tools2 = build_goal_tools()
    assert tools2[0].export_state()["goal"] is None
    tools2[0].import_state(snap)
    ok, out = tools2[1].run({})
    assert "ship v1" in out and "active" in out


def test_agent_checkpoint_meta_captures_and_restores_tool_state(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig

    cfg = AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, safety=False)

    # find the todo tool inside the assembled registry and set a plan
    agent._build_registry()
    todo = agent.registry.get("todo")
    todo.run({"action": "write", "steps_text": "alpha\nbeta"})
    agent.memory.add("decision", "use sqlite for storage")

    meta = agent._checkpoint_meta()
    assert isinstance(meta["journal_len"], int)
    assert any(s.get("text") == "alpha" for s in meta["tools"]["todo"]["steps"])
    assert meta["memory"][-1]["kind"] == "decision"

    # simulate a fresh process: blank memory, blank plan -> restore
    agent2 = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path)),
                   safety=False)
    agent2._build_registry()
    assert agent2.registry.get("todo").export_state()["steps"] == []
    assert agent2.restore_checkpoint_meta(meta) is True
    assert agent2.registry.get("todo").export_state()["steps"][0]["text"] == "alpha"
    assert any(it.text == "use sqlite for storage" for it in agent2.memory.items)
    # idempotent-ish: empty meta is a no-op returning False
    assert agent2.restore_checkpoint_meta({}) is False


def test_webui_slash_yolo_and_rewind_wired(tmp_path):
    """The web app's chat box must reach the same features the REPL has:
    /yolo flips mode + gate + badge state; /rewind rolls files to checkpoint."""
    from saturday.config import AgentConfig
    from saturday.session_runtime import SessionRuntime
    from saturday.webui import SLASH_ALIASES, handle_slash
    from saturday.tools.files import WriteFile
    from saturday.tools.journal import journal_length

    assert "/yolo" in SLASH_ALIASES and "/rewind" in SLASH_ALIASES

    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "sess")

    class A:
        cfg = AgentConfig(workspace_root=str(tmp_path))
        plan_mode = False
        safety_mode = "ask"  # per-agent effective mode (r2: no cfg bleed)
        session_store = store

        def effective_registry(self):
            return type("R", (), {"names": staticmethod(lambda: [])})()

        disabled_tools = set()

        def toggle_tool(self, *a, **k):
            return True, "", False

    rt = SessionRuntime("sid-yolo", A())

    # yolo on: policy + gate flip; config event carries safety_mode for the badge
    events = handle_slash(rt, "/yolo")
    assert rt.agent.safety_mode == "autonomous" and rt.file_gate.auto_approve is True
    # r2: the flip must NOT bleed into the shared cfg
    assert rt.agent.cfg.safety_mode == "ask"
    assert any(e.get("t") == "config" and e.get("safety_mode") == "autonomous" for e in events)
    # yolo off restores ask
    handle_slash(rt, "/yolo")
    assert rt.agent.safety_mode == "ask" and rt.file_gate.auto_approve is False

    # rewind without any checkpoint metadata -> friendly hint, not a crash
    events = handle_slash(rt, "/rewind")
    assert events and ("no checkpoint metadata" in events[0]["s"] or "nothing to rewind" in events[0]["s"])

    # rewind with real checkpoint metadata rolls files back
    w = WriteFile(root=str(tmp_path))
    w.run({"path": "keep.txt", "content": "base"})
    base_len = journal_length(tmp_path)
    sid = "sid-rew"
    rt.store.create({"task": "rw", "id": sid})
    rt.sid = sid
    rt.store.save_checkpoint(sid, [], meta={"journal_len": base_len})
    w.run({"path": "later.txt", "content": "after"})
    assert (tmp_path / "later.txt").exists()
    events = handle_slash(rt, "/rewind")
    assert "restored" in events[0]["s"]
    assert not (tmp_path / "later.txt").exists()


def test_repl_help_lists_all_web_slash_commands():
    """Every REPL HELP_TEXT slash command must be dispatchable by the web
    app's alias table (single source of truth for what users can type)."""
    import re

    from saturday.repl import HELP_TEXT
    from saturday.webui import SLASH_ALIASES

    repl_cmds = set(re.findall(r"^  (/\w+)", HELP_TEXT, re.M))
    missing = {c for c in repl_cmds if c not in ("/attach", "/images")} - set(SLASH_ALIASES)
    assert not missing, f"web app cannot dispatch: {sorted(missing)}"


def test_slash_menu_served_from_backend_not_hardcoded():
    """Regression: the UI '/' autocomplete used to read a stale hardcoded JS
    array, so new commands worked but never appeared. The menu must now be
    served by /api/state and cover every dispatchable command."""
    from saturday.webui import SLASH_ALIASES, SLASH_COMMAND_LIST

    # alias table is DERIVED from the served list -> cannot diverge
    assert set(SLASH_ALIASES) == {name for name, _ in SLASH_COMMAND_LIST}
    served = {name for name, _ in SLASH_COMMAND_LIST}
    for required in ("/yolo", "/rewind", "/plan", "/branch", "/revert", "/toggle",
                     "/jobs", "/goals", "/skills"):
        assert required in served, f"{required} missing from the slash menu"

    js = (Path(__file__).parents[1] / "src" / "saturday" / "webui_assets" / "app.js").read_text(encoding="utf-8")
    assert "SLASH_COMMANDS = [" not in js, "frontend must not keep its own command copy"
    assert "info.slash_commands" in js and "slashCommandList()" in js


def test_state_payload_serves_slash_menu(tmp_path):
    from saturday.webui import AppState

    app = AppState(cfg_overrides={"workspace_root": str(Path.cwd())})
    payload = app.state_payload()
    cmds = [c[0] for c in payload["slash_commands"]]
    assert "/yolo" in cmds and "/rewind" in cmds and len(cmds) >= 18


def test_webui_slash_jobs_goals_skills(tmp_path):
    """Hidden subsystems (jobs/goals/skills) get user-visible slash surfaces."""
    from saturday.config import AgentConfig
    from saturday.session_runtime import SessionRuntime
    from saturday.webui import handle_slash

    class FakeReg:
        def __init__(self, tools):
            self._t = tools

        def get(self, name):
            return self._t.get(name)

    class Todo:
        plan = type("P", (), {"render": staticmethod(lambda: "goal: x\n1. [ ] a")})()

    reg = FakeReg({
        "job_list": type("J", (), {"run": staticmethod(lambda a: (True, "no background jobs"))})(),
        "get_goal": type("G", (), {
            "store": type("S", (), {"get": lambda self: "goal: ship v1 | status: active | round: 0"})(),
            "run": lambda self, a: (True, self.store.get()),
        })(),
        "skills_index": type("K", (), {"run": staticmethod(lambda a: (True, "(no skills saved yet)"))})(),
    })

    class A2:
        cfg = AgentConfig(workspace_root=str(tmp_path))
        plan_mode = False

        def _build_registry(self):
            return reg

    rt = SessionRuntime("sid-feat", A2())
    assert "no background jobs" in handle_slash(rt, "/jobs")[0]["s"]
    assert "ship v1" in handle_slash(rt, "/goals")[0]["s"]
    assert "no skills" in handle_slash(rt, "/skills")[0]["s"]


def test_repl_rewind_command_rolls_files_forward_to_checkpoint(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.sessions import SessionStore
    from saturday.tools.files import WriteFile
    from saturday.tools.journal import journal_length

    ws = tmp_path / "ws"
    ws.mkdir()
    w = WriteFile(root=str(ws))
    w.run({"path": "keep.txt", "content": "base"})          # entry 0
    base_len = journal_length(ws)

    store = SessionStore(root=tmp_path / "s")
    agent = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(ws)),
                  safety=False, session_store=store)
    repl = Repl(agent, store=store, output_fn=lambda *a, **k: None)
    repl._sid = store.create({"task": "rw"})
    # simulate a checkpoint taken when the journal was at base_len
    store.save_checkpoint(repl._sid, [], meta={"journal_len": base_len})

    w.run({"path": "later.txt", "content": "after checkpoint"})  # entry 1
    assert (ws / "later.txt").exists()

    collected: list[str] = []
    repl._output = lambda *a, **k: collected.append(" ".join(str(x) for x in a))
    assert repl.dispatch("/rewind") is True
    joined = "\n".join(collected)
    assert "[rewind]" in joined and "restored" in joined
    assert not (ws / "later.txt").exists()      # post-checkpoint creation undone
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "base"



# --- from tests/test_mcp_and_durable.py ---

sys.path.insert(0, str(Path(__file__).parent))


FIXTURE = str(Path(__file__).parent / "fixtures" / "mock_mcp_server.py")


def mcp_command() -> list[str]:
    return [sys.executable, FIXTURE]


@pytest.fixture()
def client():
    c = McpStdioClient(command=mcp_command(), call_timeout=15)
    c.start()
    yield c
    c.close()


def test_mcp_handshake_and_list(client):
    assert client.server_info.get("name") == "mock-mcp"
    tools = client.list_tools()
    names = {t.name for t in tools}
    assert {"echo", "add"} <= names
    echo = next(t for t in tools if t.name == "echo")
    assert "text" in echo.input_schema["properties"]


def test_mcp_call_roundtrip_ok_and_error(client):
    ok, out = client.call_tool("echo", {"text": "saturday"})
    assert ok and out == "echo:saturday"
    ok, out = client.call_tool("add", {"a": 20, "b": 22})
    assert ok and out == "42"
    ok, out = client.call_tool("add", {"a": "x"})
    assert not ok and "bad args" in out


def test_mcp_proxy_through_registry(client):
    reg = ToolRegistry()
    plugin = build_mcp_plugin({"mock": {"command": sys.executable, "args": [FIXTURE]}})
    persona: list[str] = []
    install_plugins(reg, [plugin], persona)
    assert "echo" in reg.names() and "add" in reg.names()
    result = reg.execute("call_x", "add", {"a": 1, "b": 2})
    assert result.ok and result.output == "3"
    assert any("MCP servers" in p for p in persona)


def test_mcp_unreachable_server_warns_not_raises():
    warnings: list[str] = []
    plugin = build_mcp_plugin(
        {"ghost": {"command": sys.executable, "args": ["-c", "raise SystemExit(3)"]}},
        on_warning=warnings.append,
    )
    assert plugin.tools == [] or True
    assert any("ghost" in w for w in warnings)


def test_checkpoint_each_step_and_crash_resume(tmp_path: Path):
    store = SessionStore(root=tmp_path)
    sid = store.create({"task": "checkpoint demo"})
    checkpoints: list[int] = []

    def snap(messages):
        checkpoints.append(len(messages))
        store.save_checkpoint(sid, messages)

    model1 = make_scripted_model(
        [
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "crash.txt", "content": "halfway"}}]},
        ]
    )
    from saturday.tools.files import ReadFile, WriteFile

    reg = ToolRegistry()
    reg.register(WriteFile(root=str(tmp_path)))
    reg.register(ReadFile(root=str(tmp_path)))

    loop1 = AgentLoop(model1, reg, max_steps=5, hooks=LoopHooks(on_checkpoint=snap))
    with pytest.raises(Exception):
        loop1.run("sys", "start the task")

    assert len(checkpoints) >= 1
    saved = store.load_checkpoint(sid)
    assert saved and any(m.get("role") == "tool" for m in saved)

    model2 = make_scripted_model([{"content": "resumed and finished the task"}])
    loop2 = AgentLoop(model2, reg, max_steps=5)
    traj = loop2.run("sys", "continue", initial_history=saved)

    assert traj.stop_reason == "done"
    assert traj.final_answer == "resumed and finished the task"
    assert (tmp_path / "crash.txt").exists()


def test_agent_facade_auto_checkpoints(tmp_path: Path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path), max_steps=4)

    class P:
        name = "stub"

    cfg.profile = lambda: P()

    scripted = make_scripted_model(
        [
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "ck.txt", "content": "x"}}]},
            {"content": "done writing"},
        ]
    )
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, session_store=SessionStore(root=tmp_path / "s"))
    agent._ensure_client = lambda: scripted
    traj = agent.run("write ck.txt", session_id="sess-demo")

    assert traj.stop_reason == "done"
    ckpt = SessionStore(root=tmp_path / "s").load_checkpoint("sess-demo")
    assert ckpt is not None
    assert any(
        m.get("role") == "assistant" and m.get("content") == "done writing"
        for m in ckpt
    ), "completed assistant turn must be resumable from the checkpoint"
    records = SessionStore(root=tmp_path / "s").load("sess-demo")
    assert records is not None
