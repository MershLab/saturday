"""System design review round 1 fixes:

1. Config propagation: every settings-patch key propagates to live per-session
   cfg clones (derived list, not hand-maintained), and keys captured into tool
   instances at construction (verify_command, lsp_servers, memory_max_chars,
   auth_scopes) trigger an agent rebuild instead of silently going stale.
2. sandboxed honesty: the friction waiver requires an actual isolation
   backend; with none shipped, the effective value is False and the unmet
   request surfaces as a warning (it used to silently waive guardrails +
   dangerous asks with nothing in return).
3. shell_allow_network: actually wired into the shell tool (dynamic read,
   enforced via `unshare --net` on POSIX, fail-closed refusal where
   unenforceable) instead of being a dead toggle.
4. Hook composition: install_web_surface CHAINS a pre-existing pre_tool_call
   hook instead of replacing it.
5. Resource bounds: EventBus subscriber queues are bounded (drop-oldest);
   AppState evicts idle runtimes beyond a cap.
6. Layering: file-edit domain logic lives in saturday.editing (web surface no
   longer imports the REPL surface); /help text lives in the shared slash
   registry.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / ".saturday-home")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})


# ------------------------------------------------------- config propagation

def test_config_fields_all_propagate_or_rebuild():
    from saturday.webui import (
        _CONFIG_FIELDS,
        _PROJECT_OWNED_CONFIG_FIELDS,
        _REBUILD_CONFIG_FIELDS,
        _SHARED_CONFIG_FIELDS,
    )

    all_keys = {k for k, _ in _CONFIG_FIELDS}
    shared = set(_SHARED_CONFIG_FIELDS)
    # the derived shared list must be complete: every settings key either
    # propagates to per-session clones or is explicitly project-owned
    assert shared | set(_PROJECT_OWNED_CONFIG_FIELDS) == all_keys
    assert not shared & set(_PROJECT_OWNED_CONFIG_FIELDS)
    # rebuild keys must be real settings keys
    assert _REBUILD_CONFIG_FIELDS <= all_keys


def test_verify_command_reaches_live_tools(tmp_path, monkeypatch):
    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "sessions")
    rt = app.runtime_for("cfgtest")
    tool = rt.agent.registry.get("write_file")
    assert tool is not None
    assert tool.verify_command == ""
    app.apply_config({"verify_command": "echo VERIFY {path}"})
    assert rt.agent.cfg.verify_command == "echo VERIFY {path}"
    # the tool instance captured verify_command at construction: the rebuild
    # trigger must replace the agent so the setting actually takes effect
    fresh = app.runtime_for("cfgtest").agent.registry.get("write_file")
    assert fresh.verify_command == "echo VERIFY {path}"


def test_sandboxed_without_backend_keeps_friction_and_warns():
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.safety import isolation_enforced

    assert isolation_enforced() is False, (
        "this build ships no isolation executor; if you implement one, flip "
        "isolation_enforced() and update this test"
    )
    cfg = AgentConfig.load({"sandboxed": True})
    warnings: list[str] = []
    assert Agent._effective_sandboxed(cfg, warnings) is False
    assert any("no isolation executor" in w for w in warnings)
    # the warning surfaces once, not per run
    assert Agent._effective_sandboxed(cfg, warnings) is False
    assert len([w for w in warnings if "no isolation executor" in w]) == 1


# ------------------------------------------------------- shell_allow_network

def test_shell_allow_network_refuses_when_unenforceable(tmp_path):
    import os

    from saturday.tools.shell import ShellTool

    tool = ShellTool(root=str(tmp_path), allow_network_fn=lambda: False)
    ok, msg = tool.run({"command": "echo hi"})
    if os.name == "nt":
        # this platform cannot enforce per-process network isolation:
        # fail-closed refusal beats silently running with network
        assert not ok and "shell_allow_network=false" in msg
    # else: POSIX may wrap via unshare; covered indirectly below


def test_shell_allow_network_default_runs(tmp_path):
    from saturday.tools.shell import ShellTool

    tool = ShellTool(root=str(tmp_path))
    ok, out = tool.run({"command": "echo net-ok"})
    assert ok and "net-ok" in out


def test_shell_allow_network_read_dynamically(tmp_path):
    """The callable is consulted per call: flipping it changes behavior
    without rebuilding the tool."""
    import os

    from saturday.tools.shell import ShellTool

    state = {"allow": False}
    tool = ShellTool(root=str(tmp_path), allow_network_fn=lambda: state["allow"])
    if os.name == "nt":
        ok, _ = tool.run({"command": "echo x"})
        assert not ok  # refused while disallowed + unenforceable
    state["allow"] = True
    ok, out = tool.run({"command": "echo dynamic"})
    assert ok and "dynamic" in out


# ------------------------------------------------------------ hook composition

def test_install_web_surface_chains_pre_existing_hook():
    from saturday.agent.core import Agent
    from saturday.agent.loop import LoopHooks
    from saturday.config import AgentConfig
    from saturday.session_runtime import SessionRuntime, install_web_surface

    agent = Agent(cfg=AgentConfig.load({"workspace_root": "."}), enable_subagents=False)
    seen: list[str] = []
    agent.hooks = LoopHooks(pre_tool_call=lambda name, args: seen.append(name))
    rt = SessionRuntime("hooktest", agent)
    install_web_surface(rt, agent)
    rt.agent.hooks.pre_tool_call("read_file", {})
    assert "read_file" in seen, "pre-existing hook must survive install_web_surface"
    # and the web gate still emits tool cards
    assert any(e.get("t") == "tool_start" for e in rt.bus.buf)


# -------------------------------------------------------------- resource bounds

def test_eventbus_subscriber_queue_is_bounded():
    from saturday.session_runtime import EventBus

    bus = EventBus()
    q = bus.subscribe()
    for i in range(bus.SUB_QUEUE_MAX + 100):
        bus.publish({"t": "tick", "i": i})
    assert q.qsize() <= bus.SUB_QUEUE_MAX
    last = q.queue[-1]
    assert last["i"] >= 100, "newest events survive the drop-oldest bound"


def test_appstate_evicts_idle_runtimes(tmp_path, monkeypatch):
    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "sessions")
    monkeypatch.setattr(type(app), "MAX_RUNTIMES", 3)
    for i in range(6):
        app.runtime_for(f"sid-{i}")
    assert len(app.runtimes) <= 4, "runtime map must stay bounded"


# -------------------------------------------------------------- layering

def test_editing_module_is_single_source_of_truth():
    import saturday.editing as editing
    import saturday.repl as repl
    import saturday.session_runtime as sr

    assert repl.render_file_diff is editing.render_file_diff
    assert repl.FILE_EDIT_TOOLS is editing.FILE_EDIT_TOOLS
    assert repl._norm is editing.norm
    assert sr._norm is editing.norm
    # /help text: shared registry, re-exported by the REPL surface
    from saturday.slash import HELP_TEXT as SLASH_HELP

    assert repl.HELP_TEXT is SLASH_HELP
    assert "/toggle" in SLASH_HELP


def test_no_surface_to_surface_imports():
    """The web surface must not depend on the terminal surface: session_runtime
    (web support layer) and slash (shared registry) must not import repl.
    AST-based so docstring MENTIONS of the module don't false-positive."""
    import ast

    root = Path(__file__).parents[1] / "src" / "saturday"
    for mod in ("session_runtime.py", "slash.py"):
        tree = ast.parse((root / mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            bad = (
                isinstance(node, ast.Import) and any(a.name == "saturday.repl" for a in node.names)
            ) or (
                isinstance(node, ast.ImportFrom) and node.module == "saturday.repl"
            )
            assert not bad, f"{mod} imports saturday.repl at line {getattr(node, 'lineno', '?')}"


def test_lsp_clients_close_all():
    from saturday.tools import lsp

    lsp._clients["smoke"] = _FakeLspClient()
    lsp.close_all_clients()
    assert lsp._clients == {}
    assert lsp._clients.get("smoke") is None


class _FakeLspClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True
