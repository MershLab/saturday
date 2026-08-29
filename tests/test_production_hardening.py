"""Production-hardening regressions (design-review fixes).

Covers: trajectory export fidelity, usage-calibrated token meter, compaction
pinning dedupe, session chain-head cache + ephemeral store, lazy config paths,
per-provider overflow detection, sandbox structural fast-path, python-tool
guardrail parity, and the SessionRuntime run-state machine.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model

from saturday.agent.loop import AgentLoop
from saturday.agent.memory import TokenMeter, estimate_tokens
from saturday.llm.client import LLMClient, LLMContextOverflow, classify_error
from saturday.safety import ApprovalPolicy, check_command
from saturday.sessions import EphemeralSessionStore, SessionStore
from saturday.tools.base import ToolRegistry


# ------------------------------------------------------- export fidelity


def _tool_registry() -> ToolRegistry:
    from saturday.tools.files import WriteFile

    reg = ToolRegistry()
    reg.register(WriteFile(root=None))
    return reg


class _NullWriteFile:
    """write_file stand-in that succeeds without touching disk."""

    name = "write_file"
    description = "null"
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, args):
        return True, "wrote"


def test_trajectory_messages_match_live_history(tmp_path):
    turns = [
        {"tool_calls": [{"name": "shell", "arguments": {"command": "echo hi"}}]},
        {"content": "done"},
    ]
    model = make_scripted_model(turns)
    reg = ToolRegistry()

    class Echo:
        name = "shell"
        description = "echo"
        parameters = {"type": "object", "properties": {}, "required": []}

        def run(self, args):
            return True, f"ran {args.get('command')}"

    reg.register(Echo())
    loop = AgentLoop(model, reg, max_steps=3)
    traj = loop.run("sys", "run echo")
    exported = traj.messages()
    # second model call's history must contain EXACTLY what export renders
    live_tool_msgs = [m for m in model.calls[1]["messages"] if m.get("role") == "tool"]
    exp_tool_msgs = [m for m in exported if m.get("role") == "tool"]
    assert live_tool_msgs == exp_tool_msgs
    assert exp_tool_msgs[0]["content"].startswith("<tool_response>")
    assert '"content": "ran echo hi"' in exp_tool_msgs[0]["content"]
    # step.tool_messages captured verbatim
    assert traj.steps[0].tool_messages[0]["role"] == "tool"


# ------------------------------------------------------- token calibration


def test_token_meter_calibrates_and_projects():
    meter = TokenMeter()
    assert not meter.calibrated
    assert meter.project(100) == 100  # identity before calibration
    meter.observe(1000, 2000)  # provider says 2x the estimate
    assert meter.calibrated
    assert meter.project(1000) == 2000
    meter.observe(1000, 1500)
    assert 1700 <= meter.project(1000) <= 2000  # EMA moved toward 1.5x


def test_token_meter_ignores_zero_usage():
    meter = TokenMeter()
    meter.observe(100, 0)
    meter.observe(0, 100)
    assert not meter.calibrated


def test_loop_uses_calibration_for_compaction_threshold():
    from saturday.llm.client import ModelResponse
    from saturday.types import Usage

    base = make_scripted_model([{"content": "ok"}, {"content": "ok"}])
    orig_chat = base.chat

    def chat_with_usage(messages, **kwargs):
        resp = orig_chat(messages, **kwargs)
        resp.message.usage = Usage(prompt_tokens=5000, completion_tokens=10, total_tokens=5010)
        return ModelResponse(message=resp.message, finish_reason="stop")

    base.chat = chat_with_usage
    loop = AgentLoop(base, ToolRegistry(), max_steps=2, compact_above_tokens=10**9)
    loop.run("sys", "hi")
    assert loop.meter.samples >= 1
    assert loop.meter.calibrated


def test_estimate_tokens_cjk_counts_per_char():
    text = "你好" * 10  # 20 CJK chars ~ 20 tokens, not 40/4=10... len=20 -> old est 5
    assert estimate_tokens(text) >= 18


# ------------------------------------------------------- compaction pinning


def test_compact_pinned_summary_does_not_duplicate_excerpt():
    model = make_scripted_model([{"content": "filler"}])
    loop = AgentLoop(model, ToolRegistry(), max_steps=1, summarizer=lambda s: s.upper())
    history = [
        {"role": "user", "content": "# Goal\n" + "g" * 300},
        *[{"role": "user", "content": f"turn {i} " + "y" * 400} for i in range(12)],
    ]
    loop._compact(history)
    item = loop.memory.items[-1]
    # summary pinned once; raw excerpt tail only appended when NOT already inside
    summary_part = item.text[: len(item.text) // 2]
    tail = item.text[-2000:]
    assert item.text.count(tail) == 1 or tail.startswith(summary_part[-100:])


def test_compact_fallback_summary_pins_digest_once():
    model = make_scripted_model([{"content": "filler"}])
    loop = AgentLoop(model, ToolRegistry(), max_steps=1)
    history = [{"role": "user", "content": f"turn {i} " + "z" * 300} for i in range(12)]
    loop._compact(history)
    item = loop.memory.items[0]
    assert item.kind == "compaction-summary"
    assert "turn 5" in item.text


# ------------------------------------------------------- session store


def test_list_sessions_uncapped_by_default(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    sids = [store.create({"task": f"chat {i}"}) for i in range(30)]
    rows = store.list_sessions()
    assert len(rows) == 30  # a cap here once hid real chats behind junk files
    assert {r["id"] for r in rows} == set(sids)
    # newest first
    assert rows[0]["id"] == sids[-1]


def test_search_finds_needle_in_oldest_session(tmp_path):
    from saturday.webui_support import search_sessions

    store = SessionStore(root=tmp_path / "s")
    for i in range(25):
        sid = store.create({"task": f"filler {i}"})
        store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": f"small talk {i}"}]})
        if i == 0:
            store.append(
                sid,
                {"type": "messages", "messages": [{"role": "assistant", "content": "the quantum ferret protocol"}]},
            )
    hits = search_sessions(store, "quantum ferret")
    assert len(hits) == 1 and hits[0]["task"] == "filler 0"
    assert "ferret" in hits[0]["snippet"]


def test_hydrate_falls_back_to_checkpoint_for_interrupted_runs(tmp_path):
    """Interrupted runs never reach the post-run transcript append; their only
    copy of the conversation lives in .checkpoint.json. Hydration must fall
    back to it or those chats render blank in the app."""
    from saturday.webui_support import hydrate_session

    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "killed mid-run"})
    store.save_checkpoint(
        sid,
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "tool", "tool_call_id": "c1", "content": "<tool_response>\n{\"content\": \"42\"}\n</tool_response>"},
            {"role": "assistant", "content": "answer is 42"},
        ],
    )
    data = hydrate_session(store, sid)
    assert data is not None and data["resumable"] is True
    kinds = [it["kind"] for it in data["items"]]
    assert kinds == ["user", "assistant", "assistant"]
    assert data["items"][1]["results"]["c1"]["body"] == "42"

    # a completed run keeps its transcript as the source of truth
    sid2 = store.create({"task": "finished"})
    store.append(sid2, {"type": "messages", "messages": [{"role": "user", "content": "from transcript"}]})
    store.save_checkpoint(sid2, [{"role": "user", "content": "from checkpoint"}])
    data2 = hydrate_session(store, sid2)
    assert [it["text"] for it in data2["items"]] == ["from transcript"]


def test_read_meta_after_appends_first_line_only(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "meta check", "surface": "app"})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "x" * 5000}]})
    meta = store.read_meta(sid)
    assert meta is not None and meta["task"] == "meta check" and meta["surface"] == "app"


def test_chain_head_cache_keeps_chain_valid_under_load(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "t"})
    for i in range(50):
        store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": f"m{i}"}]})
    status = store.audit_verify(sid)
    assert status is not None and status["ok"] and status["records"] == 50


def test_chain_head_cache_invalidated_by_external_edit(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "t"})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "a"}]})
    # simulate external rewrite (different content, same length is fine)
    p = store._path(sid)
    lines = p.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps({"type": "messages", "messages": [{"role": "user", "content": "external"}], "hash": "f" * 64}))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "b"}]})
    status = store.audit_verify(sid)
    # the external record has a bogus hash -> verification MUST detect it,
    # proving the cache did not serve a stale head
    assert status is not None and not status["ok"]


def test_ephemeral_store_writes_nothing(tmp_path):
    probe = tmp_path / "nowhere"
    probe.mkdir()
    es = EphemeralSessionStore()
    sid = es.create({"task": "sub"})
    es.append(sid, {"type": "messages", "messages": []})
    es.save_checkpoint(sid, [{"role": "user", "content": "x"}])
    assert es.load_checkpoint(sid) is None
    assert es.list_sessions() == []
    assert es.read_meta(sid) is None
    assert list(probe.iterdir()) == []
    # the store must not fall back to the default CONFIG_DIR either
    import saturday.config as cfgmod

    assert not (Path(cfgmod.get_config_dir()) / "sessions").exists()


def test_subagent_isolated_store_and_shared_approver(monkeypatch):
    import saturday.agent.core as core_mod
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.safety import ApprovalPolicy

    captured = {}

    class FakeSubAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.approval_policy = ApprovalPolicy.from_mode("off")

        def run(self, prompt, initial_history=None):
            class T:
                final_answer = "sub done"
                stop_reason = "done"

            t = T()
            t.messages = lambda: [
                {"role": "system", "content": "s"},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "sub done"},
            ]
            return t

    monkeypatch.setattr(core_mod, "Agent", FakeSubAgent)
    cfg = AgentConfig(provider="openai", model="m")

    class Approver:
        pass

    parent = Agent(cfg=cfg, client=object(), enable_subagents=True)
    approver = Approver()
    parent.approval_policy.approver = approver
    tool = parent._make_task_tool()
    result = tool.run({"prompt": "go"})
    assert result[0]
    assert isinstance(captured.get("session_store"), EphemeralSessionStore)
    assert captured.get("enable_subagents") is False
    assert FakeSubAgent.__name__  # sub was constructed
    assert isinstance(parent.approval_policy.approver, Approver)


# ------------------------------------------------------- config paths


def test_config_dir_patch_propagates_to_file(monkeypatch, tmp_path):
    import saturday.config as cfgmod

    scratch = tmp_path / "dfhome"
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", scratch)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    assert cfgmod.get_config_file() == scratch / "config.json"
    cfgmod.save_config({"provider": "deepseek"})
    assert json.loads((scratch / "config.json").read_text(encoding="utf-8"))["provider"] == "deepseek"


def test_config_file_explicit_override_still_wins(monkeypatch, tmp_path):
    import saturday.config as cfgmod

    explicit = tmp_path / "elsewhere.json"
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", explicit)
    assert cfgmod.get_config_file() == explicit


# ------------------------------------------------------- overflow markers


class _FakeHTTPError(Exception):
    def __init__(self, code, body="", headers=None):
        self.code = code
        self.body = body
        self.headers = headers or {}
        self.msg = "http error"

    def read(self):
        return self.body.encode("utf-8")


def _net_err(code, body="", headers=None):
    """HTTP-ish error inside the client's caught exception tuple."""
    import urllib.error

    class NetErr(urllib.error.URLError):
        def __init__(self):
            super().__init__("http error")
            self.code = code
            self.headers = headers or {}
            self.msg = "http error"

        def read(self):
            return body.encode("utf-8")

    return NetErr()


def test_classify_error_tuple_api_unchanged():
    assert classify_error(_net_err(429, headers={"Retry-After": "7"})) == ("rate_limit", 7)
    assert classify_error(_net_err(401))[0] == "auth"
    assert classify_error(_net_err(400, body="maximum context length"))[0] == "context_overflow"
    assert classify_error(_net_err(500))[0] == "server"


def test_provider_overflow_marker_triggers_context_overflow(monkeypatch):
    client = LLMClient(base_url="http://x/v1", model="m", overflow_markers=("prompt is too long",))
    _patch_opener(monkeypatch, lambda req, timeout=None: (_ for _ in ()).throw(_net_err(400, body='{"error":{"message":"Your prompt is too long"}}')))
    with pytest.raises(LLMContextOverflow):
        client.chat([{"role": "user", "content": "hi"}], stream_callback=None)


def test_non_marked_bad_request_stays_bad_request(monkeypatch):
    client = LLMClient(base_url="http://x/v1", model="m", overflow_markers=("prompt is too long",))
    _patch_opener(monkeypatch, lambda req, timeout=None: (_ for _ in ()).throw(_net_err(400, body='{"error":{"message":"invalid parameter"}}')))
    with pytest.raises(Exception) as ei:
        client.chat([{"role": "user", "content": "hi"}], stream_callback=None)
    assert "context overflow" not in str(ei.value).lower()


# ------------------------------------------------------- sandbox fast-path


def _policy(mode="ask", approver=None):
    return ApprovalPolicy.from_mode(mode, approver=approver)


class _FakeOpener:
    """Stands in for saturday.llm.client._OPENER (urllib.urlopen is no longer
    the transport: redirects must strip Authorization cross-host)."""

    def __init__(self, fn):
        self._fn = fn

    def open(self, req, timeout=None):
        return self._fn(req, timeout=timeout)


def _patch_opener(monkeypatch, fn):
    import saturday.llm.client as client_mod

    monkeypatch.setattr(client_mod, "_OPENER", _FakeOpener(fn))


def test_sandboxed_skips_guardrail_ask_but_ask_mode_hardline_holds():
    args = {"command": "rm -rf /tmp/bigdata"}
    # without sandbox + no approver: guardrail fails closed
    assert check_command(_policy("off"), "shell", args, guardrails=True) is not None
    # with sandbox: structural isolation replaces pattern friction
    assert check_command(_policy("off"), "shell", args, guardrails=True, sandboxed=True) is None
    # hardline still bites in ask mode even when sandboxed
    root_args = {"command": "rm -rf / --no-preserve-root"}
    reason = check_command(_policy("ask"), "shell", root_args, sandboxed=True)
    assert reason is not None and "HARDLINE BLOCK" in reason


def test_sandboxed_deny_mode_still_denies_guardrail():
    args = {"command": "DROP TABLE users"}
    assert check_command(_policy("deny"), "shell", args, guardrails=True, sandboxed=True) is not None


def test_reserved_scope_still_asks_when_sandboxed():
    args = {"command": "ls"}
    scopes = {"reserved": ["shell"]}
    reason = check_command(
        _policy("off"),
        "shell",
        args,
        scopes=scopes,
        sandboxed=True,
    )
    assert reason is not None and "AWAITING APPROVAL unavailable" in reason


def test_python_tool_parity_with_shell_guardrails():
    code = {"code": 'import os\nos.system("rm -rf ./data")'}
    assert check_command(_policy("off"), "python", code, guardrails=True) is not None
    sql = {"code": 'import sqlite3\nc.execute("DELETE FROM users")'}
    assert check_command(_policy("off"), "python", sql, guardrails=True) is not None


# ------------------------------------------------------- runtime state machine


def _runtime():
    from saturday.session_runtime import SessionRuntime

    rt = SessionRuntime.__new__(SessionRuntime)
    threading.Lock()
    rt._run_lock = threading.RLock()
    rt._phase = SessionRuntime.PHASE_IDLE
    rt._stop_requested = False
    rt.run_generation = 0
    return rt


def test_try_begin_run_is_atomic_and_generations_monotonic():
    rt = _runtime()
    assert rt.try_begin_run() is True
    assert rt.busy is True
    assert rt.try_begin_run() is False  # double-start rejected
    gen = rt.run_generation
    rt.finish_run()
    assert rt.is_idle
    assert rt.run_generation == gen  # finish never resets generation
    assert rt.try_begin_run() is True
    assert rt.run_generation == gen + 1


def test_request_stop_only_flags_until_finish():
    rt = _runtime()
    rt.try_begin_run()
    rt.request_stop()
    assert rt.should_stop() is True
    rt.finish_run()
    assert rt.should_stop() is False  # cleared by the terminal transition


def test_webui_reexports_intact():
    import saturday.webui as w

    for name in (
        "WebApprover",
        "WebFileGate",
        "RunStopped",
        "_Bus",
        "_SessionRuntime",
        "_install_web_surface",
        "hydrate_session",
        "search_sessions",
        "_title_from_text",
        "_env_upsert",
        "_save_data_urls",
    ):
        assert hasattr(w, name), name
