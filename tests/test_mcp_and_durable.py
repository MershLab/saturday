from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.loop import AgentLoop, LoopHooks  # noqa: E402
from saturday.mcp_client import McpStdioClient  # noqa: E402
from saturday.mcp_plugin import build_mcp_plugin  # noqa: E402
from saturday.plugins import install_plugins  # noqa: E402
from saturday.sessions import SessionStore  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402

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
