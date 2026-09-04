"""Saturday's MCP *server* half: protocol framing, dispatch, stdio loop."""
from __future__ import annotations

import io
import json
import sys

from saturday.mcp_client import PROTOCOL_VERSION
from saturday.mcp_server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    ExposedTool,
    McpServer,
    serve_stdio,
)


def _echo_tool() -> ExposedTool:
    return ExposedTool(
        name="echo",
        description="echo back",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        handler=lambda args: (True, str(args.get("text", ""))),
    )


def _req(rid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def test_initialize_reports_the_same_protocol_version_as_the_client():
    # client and server halves drifting apart is the failure this pins
    resp = McpServer().handle(_req(1, "initialize", {}))
    assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert resp["result"]["serverInfo"]["name"] == "saturday"
    assert "tools" in resp["result"]["capabilities"]


def test_tools_list_exposes_schema_under_input_schema_key():
    resp = McpServer([_echo_tool()]).handle(_req(2, "tools/list"))
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "echo"
    assert tools[0]["inputSchema"]["properties"]["text"]["type"] == "string"


def test_tools_call_returns_text_content():
    resp = McpServer([_echo_tool()]).handle(
        _req(3, "tools/call", {"name": "echo", "arguments": {"text": "hi"}})
    )
    assert resp["result"]["content"] == [{"type": "text", "text": "hi"}]
    assert resp["result"]["isError"] is False


def test_a_failing_tool_is_a_result_with_is_error_not_a_protocol_error():
    failing = ExposedTool(name="boom", description="", handler=lambda args: (False, "nope"))
    resp = McpServer([failing]).handle(_req(4, "tools/call", {"name": "boom"}))
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    assert resp["result"]["content"][0]["text"] == "nope"


def test_a_raising_tool_is_caught_and_reported_as_a_tool_error():
    def raiser(args):
        raise RuntimeError("kaboom")

    exploding = ExposedTool(name="boom", description="", handler=raiser)
    resp = McpServer([exploding]).handle(_req(5, "tools/call", {"name": "boom"}))
    assert resp["result"]["isError"] is True
    assert "kaboom" in resp["result"]["content"][0]["text"]


def test_unknown_tool_and_missing_name_are_invalid_params():
    srv = McpServer([_echo_tool()])
    assert srv.handle(_req(6, "tools/call", {"name": "nope"}))["error"]["code"] == INVALID_PARAMS
    assert srv.handle(_req(7, "tools/call", {}))["error"]["code"] == INVALID_PARAMS
    bad_args = srv.handle(_req(8, "tools/call", {"name": "echo", "arguments": []}))
    assert bad_args["error"]["code"] == INVALID_PARAMS


def test_unknown_method_is_method_not_found_and_does_not_kill_the_stream():
    # a client probing for resources/prompts must get a clean no
    resp = McpServer().handle(_req(9, "resources/list"))
    assert resp["error"]["code"] == METHOD_NOT_FOUND


def test_notifications_never_get_a_response():
    srv = McpServer()
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert srv.initialized is True
    # even an unknown notification stays silent rather than erroring back
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None


def test_malformed_request_shapes_are_rejected():
    srv = McpServer()
    assert srv.handle({"jsonrpc": "2.0", "id": 1})["error"]["code"] == INVALID_REQUEST
    assert srv.handle([])["error"]["code"] == INVALID_REQUEST


def test_serve_stdio_frames_one_json_object_per_line():
    inp = io.StringIO(
        json.dumps(_req(1, "initialize", {})) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + "\n"  # blank lines are skipped, not parse errors
        + json.dumps(_req(2, "tools/call", {"name": "echo", "arguments": {"text": "yo"}})) + "\n"
    )
    out = io.StringIO()
    assert serve_stdio(McpServer([_echo_tool()]), stdin=inp, stdout=out) == 0
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    # the notification produced no frame, so two responses for three messages
    assert [m["id"] for m in lines] == [1, 2]
    assert lines[1]["result"]["content"][0]["text"] == "yo"


def test_serve_stdio_reports_bad_json_and_keeps_reading():
    inp = io.StringIO("{not json\n" + json.dumps(_req(1, "ping")) + "\n")
    out = io.StringIO()
    serve_stdio(McpServer(), stdin=inp, stdout=out)
    lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
    assert lines[0]["error"]["code"] == PARSE_ERROR
    assert lines[1]["result"] == {}


def test_serve_stdio_rejects_batches_explicitly():
    inp = io.StringIO(json.dumps([_req(1, "ping")]) + "\n")
    out = io.StringIO()
    serve_stdio(McpServer(), stdin=inp, stdout=out)
    resp = json.loads(out.getvalue().strip())
    assert resp["error"]["code"] == INVALID_REQUEST
    assert "batch" in resp["error"]["message"]


def test_a_printing_tool_cannot_corrupt_the_protocol_stream(capsys):
    # the whole point of the stdout guard: tools assume a CLI and print freely
    def chatty(args):
        print("this would corrupt the stream")
        return True, "done"

    inp = io.StringIO(json.dumps(_req(1, "tools/call", {"name": "chatty"})) + "\n")
    out = io.StringIO()
    real_stdout = sys.stdout
    serve_stdio(McpServer([ExposedTool(name="chatty", description="", handler=chatty)]), stdin=inp, stdout=out)
    assert sys.stdout is real_stdout  # restored on the way out
    lines = [x for x in out.getvalue().splitlines() if x.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["content"][0]["text"] == "done"
    assert "corrupt" in capsys.readouterr().err


# -- exposure layer ---------------------------------------------------------

from saturday.mcp_server import build_server, registry_tools  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402


class _StubTraj:
    def __init__(self, answer="the answer", stop_reason="done"):
        self.final_answer = answer
        self.stop_reason = stop_reason
        self.steps = [1, 2, 3]


def _stub_agent(monkeypatch, traj=None, capture=None):
    """Replace Agent + AgentConfig.load so a run needs no provider or network."""
    import saturday.agent.core as core
    import saturday.config as cfgmod

    class StubAgent:
        def __init__(self, cfg=None):
            self.cfg = cfg

        def run(self, task, session_id=None, on_session_id=None, **kw):
            if capture is not None:
                capture["task"] = task
                capture["session_id"] = session_id
            if on_session_id:
                on_session_id("sess-123")
            return traj or _StubTraj()

    monkeypatch.setattr(core, "Agent", StubAgent)
    monkeypatch.setattr(
        cfgmod.AgentConfig,
        "load",
        classmethod(lambda cls, overrides=None: (capture.update(overrides=overrides or {}) if capture is not None else None) or object()),
    )


def test_agent_mode_is_the_default_and_does_not_expose_raw_shell():
    names = [t["name"] for t in build_server().handle(_req(1, "tools/list"))["result"]["tools"]]
    assert names == ["saturday_run", "saturday_sessions"]
    assert "shell" not in names  # the whole point of the default


def test_tools_mode_exposes_the_registry_and_all_mode_exposes_both():
    tool_names = [t["name"] for t in build_server("tools").handle(_req(1, "tools/list"))["result"]["tools"]]
    assert "shell" in tool_names and "read_file" in tool_names
    assert "saturday_run" not in tool_names
    all_names = [t["name"] for t in build_server("all").handle(_req(1, "tools/list"))["result"]["tools"]]
    assert "saturday_run" in all_names and "shell" in all_names


def test_read_only_passthrough_uses_the_existing_plan_mode_allowlist():
    names = {t.name for t in registry_tools(read_only=True)}
    assert names <= ToolRegistry.READ_ONLY_TOOLS
    assert "read_file" in names
    for mutating in ("shell", "write_file", "edit_file", "python"):
        assert mutating not in names


def test_registry_passthrough_actually_runs_a_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("from disk", encoding="utf-8")
    srv = build_server("tools")
    resp = srv.handle(_req(1, "tools/call", {"name": "read_file", "arguments": {"path": "hello.txt"}}))
    assert resp["result"]["isError"] is False
    assert "from disk" in resp["result"]["content"][0]["text"]


def test_unknown_expose_mode_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        build_server("everything")


def test_saturday_run_returns_the_answer_with_a_status_footer(monkeypatch):
    capture: dict = {}
    _stub_agent(monkeypatch, capture=capture)
    resp = build_server().handle(_req(1, "tools/call", {"name": "saturday_run", "arguments": {"task": "do it"}}))
    text = resp["result"]["content"][0]["text"]
    assert resp["result"]["isError"] is False
    assert text.startswith("the answer")
    assert "session sess-123" in text and "stop=done" in text
    assert capture["task"] == "do it"


def test_saturday_run_reports_a_run_that_produced_no_answer_as_an_error():
    class _Empty(_StubTraj):
        def __init__(self):
            super().__init__(answer="", stop_reason="max_steps")

    import pytest

    monkeypatch = pytest.MonkeyPatch()
    try:
        _stub_agent(monkeypatch, traj=_Empty())
        resp = build_server().handle(_req(1, "tools/call", {"name": "saturday_run", "arguments": {"task": "x"}}))
        assert resp["result"]["isError"] is True
        assert "stop=max_steps" in resp["result"]["content"][0]["text"]
    finally:
        monkeypatch.undo()


def test_saturday_run_requires_a_non_empty_task():
    resp = build_server().handle(_req(1, "tools/call", {"name": "saturday_run", "arguments": {"task": "   "}}))
    assert resp["result"]["isError"] is True
    assert "task" in resp["result"]["content"][0]["text"]


def test_read_only_puts_the_delegated_run_in_plan_mode(monkeypatch):
    capture: dict = {}
    _stub_agent(monkeypatch, capture=capture)
    srv = build_server(read_only=True)
    srv.handle(_req(1, "tools/call", {"name": "saturday_run", "arguments": {"task": "look around"}}))
    assert capture["overrides"]["plan_mode"] is True


def test_max_steps_argument_reaches_the_config(monkeypatch):
    capture: dict = {}
    _stub_agent(monkeypatch, capture=capture)
    build_server().handle(_req(1, "tools/call", {"name": "saturday_run", "arguments": {"task": "x", "max_steps": 4}}))
    assert capture["overrides"]["max_steps"] == 4


def test_saturday_sessions_reports_an_empty_store_without_failing():
    resp = build_server().handle(_req(1, "tools/call", {"name": "saturday_sessions", "arguments": {}}))
    assert resp["result"]["isError"] is False
    assert "no sessions yet" in resp["result"]["content"][0]["text"]


def test_the_client_and_server_halves_actually_talk_to_each_other(tmp_path, monkeypatch):
    """End to end over a real subprocess, using Saturday's own MCP client.

    Everything above tests the halves in isolation; only this proves the
    framing, handshake and stdout guard survive a real pipe."""
    import sys

    from saturday.mcp_client import McpStdioClient

    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("round trip", encoding="utf-8")
    client = McpStdioClient(
        command=[sys.executable, "-m", "saturday", "mcp-serve", "--expose", "all", "--read-only"],
        call_timeout=60,
    )
    try:
        info = client.start()
        assert info.get("name") == "saturday"
        names = {t.name for t in client.list_tools()}
        assert "saturday_run" in names and "read_file" in names
        assert "shell" not in names and "ask_user" not in names
        ok, text = client.call_tool("read_file", {"path": "note.txt"})
        assert ok and "round trip" in text
    finally:
        client.close()
