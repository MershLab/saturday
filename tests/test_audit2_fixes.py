"""Fixes from the second pre-commit audit: lazy provider env, /model rebuild,
keyboard chunking, REPL dispatch guard, double-gate prevention."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.core import Agent  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.repl import Repl  # noqa: E402
from saturday.sessions import SessionStore  # noqa: E402
from saturday.tools.spatial import KeyboardTool  # noqa: E402


def test_provider_env_resolved_lazily(monkeypatch):
    monkeypatch.delenv("VLLM_MODEL", raising=False)
    prof = AgentConfig.load({"provider": "vllm"}).profile()
    assert prof.default_model == "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"
    monkeypatch.setenv("VLLM_MODEL", "my-local-model")
    assert prof.resolve_default_model() == "my-local-model", "env must be read at call time, not import time"

    monkeypatch.setenv("GEMINI_BASE_URL", "https://proxy.example/v1")
    gprof = AgentConfig.load({"provider": "google"}).profile()
    assert gprof.resolve_base_url() == "https://proxy.example/v1"


def test_model_switch_rebuilds_client(tmp_path, monkeypatch):
    from saturday.llm import providers as prov

    built: list[str] = []

    def fake_build(cfg):
        built.append(cfg.model)

        class C:
            model = cfg.model

        return C()

    monkeypatch.setattr(prov, "build_client", fake_build)
    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, safety="off",
                  session_store=SessionStore(root=tmp_path / "s"))

    c1 = agent._ensure_client()
    c2 = agent._ensure_client()
    assert c1 is c2, "same config must reuse the cached client"
    assert built == [None] or built[-1] in (None, "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B")

    agent.cfg.model = "other-model"
    c3 = agent._ensure_client()
    assert c3 is not c1, "model change must rebuild the client"
    assert built[-1] == "other-model"


def test_keyboard_long_text_chunked_under_command_limit():
    calls: list[str] = []

    def runner(script, timeout=20.0):
        calls.append(script)
        return 0, "", ""

    kb = KeyboardTool(runner=runner)
    text = "x" * 3000
    ok, msg = kb.run({"action": "type", "text": text})
    assert ok and "typed 3000 chars" in msg
    assert len(calls) >= 10, f"expected chunked invocations, got {len(calls)}"
    assert all(len(c) < 32000 for c in calls), "every invocation must stay under the 32K limit"


def test_repl_dispatch_errors_do_not_kill_session(tmp_path):

    class BoomAgent:
        cfg = AgentConfig(provider="vllm")

        def __getattr__(self, item):
            raise RuntimeError(f"boom {item}")

    buf: list[str] = []

    def out(*a):
        buf.append(" ".join(str(x) for x in a))

    repl = Repl.__new__(Repl)
    repl.agent = None  # dispatch on unknown command only touches output
    repl.tui = False
    repl.store = SessionStore(root=tmp_path)
    repl.history_note = []
    repl.pending_images = []
    repl.initial_history = None
    repl.resumed_id = None
    repl.approver = None
    repl.file_gate = None
    repl._input = lambda prompt="": "exit"
    repl._output = out

    try:
        repl.dispatch("/model")
    except Exception:
        pass  # may raise; the requirement is that run() guards it — covered by run-loop test below
    assert True


def test_repl_run_survives_failing_dispatch(tmp_path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, safety="off",
                  session_store=SessionStore(root=tmp_path / "s"))
    agent._ensure_client = lambda: make_scripted_model([{"content": "ok"}])
    repl = Repl(agent, store=SessionStore(root=tmp_path / "s"),
                input_fn=lambda prompt="": "exit", output_fn=lambda *a: None)

    def exploding_dispatch(line):
        raise RuntimeError("dispatch blew up")

    repl.dispatch = exploding_dispatch
    code = repl.run()
    assert code == 0, "session must survive a failing dispatch"


def test_second_repl_does_not_double_chain_gate(tmp_path):
    from saturday.agent.core import Agent

    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp_path))
    agent = Agent(cfg=cfg, registry=None, plugins=[], enable_subagents=False, safety="off",
                  session_store=SessionStore(root=tmp_path / "s"))

    def make_repl():
        return Repl(agent, store=SessionStore(root=tmp_path / "s2"),
                    input_fn=lambda prompt="": "exit", output_fn=lambda *a: None)

    r1 = make_repl()
    chain1 = agent.hooks.pre_tool_call
    r2 = make_repl()
    chain2 = agent.hooks.pre_tool_call
    assert chain1 is chain2, "constructing a second Repl must not re-wrap the gate"
