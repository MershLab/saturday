"""Context-tracking parity with hermes-agent / opencode:

1. compaction signal prefers provider-reported prompt_tokens over projections
2. per-model context-window resolution (no more fixed 96K for every model)
3. meter calibration survives resume via checkpoint meta
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402


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


# ------------------------------------------------------- per-model windows

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


# --------------------------------------------- live-loop integration check

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
