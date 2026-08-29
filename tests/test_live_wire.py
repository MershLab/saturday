"""End-to-end wire tests: the REAL LLMClient over REAL localhost HTTP.

Covers what no fake can: Authorization headers on the wire, JSON encoding,
SSE frame parsing, fragmented tool-call delta accumulation, and usage events.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from saturday.agent.loop import AgentLoop
from saturday.llm.client import LLMClient, StreamEvent
from saturday.tools.base import ToolRegistry
from saturday.tools.files import ReadFile, WriteFile


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
