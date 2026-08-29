"""Round-2 bug-fix regressions. Each test pins a defect found in the sweep:
- job_list crashed with AttributeError once a background subagent existed
  (JobManager.reap assumed every job has .proc)
- streamed responses whose server ignored stream:true (JSON fallback path)
  skipped Hermes XML tool-call extraction, silently dropping tool calls for
  non-native function-callers
- Message.from_openai mis-sliced DeepSeek <｜Assistant｜> payloads whose
  </think> closer appeared before the opener
- REPL /model changed the in-memory model but never persisted it
- CLI run and REPL turns never recorded usage rows (metrics undercounted;
  only the webui recorded)
- plan mode hid observation tools repo_search / lsp_diagnostics / lsp_definition
- compaction token estimates ignored assistant tool_call argument sizes
"""
from __future__ import annotations

import json
import time


def test_job_list_survives_background_subagent_jobs():
    from saturday.tools.jobs import AgentJob, JobManager, make_job_tools

    mgr = JobManager()
    mgr.register(AgentJob("ag-sub-9", "task sub-9", {"lines": ["x"], "done": True}))
    (job_list, job_output, _kill) = make_job_tools(mgr)
    ok, out = job_list.run({})
    assert ok and "ag-sub-9" in out


def test_reap_keeps_live_agent_jobs_and_old_process_jobs_semantics():
    from saturday.tools.jobs import AgentJob, Job, JobManager
    import subprocess
    import sys

    mgr = JobManager()
    fresh = AgentJob("ag-fresh", "task", {"lines": [], "done": False})
    old = AgentJob("ag-old", "task", {"lines": [], "done": True})
    old.created = time.time() - 7200
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    live_proc_job = Job("pj1", "sleep", proc)
    mgr.register(fresh)
    mgr.register(old)
    with mgr._lock:
        mgr._jobs["pj1"] = live_proc_job
    mgr.reap()
    assert mgr.get("ag-fresh") is not None
    assert mgr.get("ag-old") is None
    assert mgr.get("pj1") is not None
    live_proc_job.kill()


def _fake_stream_response(monkeypatch, body: dict, content_type: str = "application/json"):
    """Make LLMClient._chat_stream receive a non-SSE JSON body."""
    import io

    class FakeResp(io.BytesIO):
        def geturl(self):
            return "http://test/chat/completions"

    class FakeHeaders(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), super().get(k, d))

    resp = FakeResp(json.dumps(body).encode("utf-8"))
    resp.headers = {k.lower(): v for k, v in {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }.items()}
    resp.headers = FakeHeaders(resp.headers)

    def fake_urlopen(req, timeout=None):
        return resp

    # the client routes through its redirect-safe opener, not raw urlopen
    class _FakeOpener:
        def open(self, req, timeout=None):
            return fake_urlopen(req, timeout=timeout)

    import saturday.llm.client as client_mod

    monkeypatch.setattr(client_mod, "_OPENER", _FakeOpener())


def test_stream_json_fallback_still_parses_hermes_tool_calls(monkeypatch):
    from saturday.llm.client import LLMClient

    content = 'Let me check.\n<tool_call>\n{"name": "shell", "arguments": {"command": "echo hi"}}\n</tool_call>'
    body = {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    _fake_stream_response(monkeypatch, body)

    client = LLMClient(base_url="http://test", api_key="k", model="m")
    events: list = []
    resp = client.chat([{"role": "user", "content": "hi"}], stream_callback=events.append)
    assert len(resp.message.tool_calls) == 1
    assert resp.message.tool_calls[0].name == "shell"
    assert resp.message.content == "Let me check."
    assert any(e.type == "tool_call" for e in events)


def test_deepseek_marker_with_misplaced_closer_left_untouched():
    from saturday.types import Message

    raw = {"content": "</think>oops<｜Assistant｜>real answer"}
    msg = Message.from_openai(raw)
    assert msg.content == raw["content"]
    assert msg.reasoning is None


def test_repl_model_persists_to_config(tmp_path, monkeypatch):
    import saturday.config as cfgmod
    from saturday.config import AgentConfig
    from saturday.repl import Repl

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)

    cfg = AgentConfig(provider="deepseek")
    outputs: list[str] = []

    class Store:
        def list_sessions(self):
            return []

    repl = Repl.__new__(Repl)
    repl.agent = type("A", (), {})()
    repl.agent.cfg = cfg
    repl.agent.effective_registry = lambda: type("R", (), {"names": lambda self: []})()
    repl.agent.disabled_tools = set()
    repl.agent.toggle_tool = lambda *a, **k: (True, "", False)
    repl.agent.plan_mode = False
    repl.store = Store()
    repl.history_note = []
    repl.pending_images = []
    repl._output = lambda s="": outputs.append(str(s))

    repl.dispatch("/model my-model-x")
    saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert saved["model"] == "my-model-x"


def test_cli_run_records_usage(tmp_path, monkeypatch):
    from argparse import Namespace

    import saturday.cli as cli
    from saturday.types import Trajectory, Usage

    rows: list[dict] = []
    monkeypatch.setattr("saturday.usage.record_usage", lambda **kw: rows.append(kw))

    def fake_init(self, cfg=None, **kw):
        from saturday.config import AgentConfig

        self.cfg = cfg or AgentConfig.load()

    def fake_run(self, task, **kw):
        t = Trajectory(task=task, system_prompt="s", final_answer="ok", stop_reason="done")
        t.usage = Usage(prompt_tokens=11, completion_tokens=7, total_tokens=18)
        return t

    monkeypatch.setattr("saturday.agent.core.Agent.__init__", fake_init)
    monkeypatch.setattr("saturday.agent.core.Agent.run", fake_run)

    args = Namespace(
        task="t", quiet=True, session="sess-1", json_out=None, ci=False, detach=False,
        background=False, images=None, env=None, provider=None, model=None,
        temperature=None, max_steps=None, assistant=False, plan=False,
        max_run_tokens=None, disabled_tools=None,
    )
    assert cli.cmd_run(args) == 0
    assert rows and rows[0]["session"] == "sess-1"
    assert rows[0]["total_tokens"] == 18


def test_repl_turn_records_usage(tmp_path, monkeypatch):
    import saturday.usage as U
    from saturday.types import Trajectory, Usage

    scripted = Trajectory(task="q", system_prompt="s", final_answer="a", stop_reason="done")
    scripted.usage = Usage(3, 4, 7)

    class FakeAgent:
        def __init__(self):
            from saturday.config import AgentConfig

            self.cfg = AgentConfig(provider="deepseek", workspace_root=str(tmp_path))
            self.session_store = _FakeStore()

        def run(self, task, **kw):
            return scripted

        @property
        def approval_policy(self):
            from saturday.safety import ApprovalPolicy

            return ApprovalPolicy.from_mode("ask")

        hooks = None

    class _FakeStore:
        def create(self, meta):
            return "sid1"

        def load_checkpoint(self, sid):
            return None

    inputs = iter(["hello", "exit"])
    outputs: list[str] = []
    agent = FakeAgent()

    # minimal Repl wiring without __init__ (input/output injected)
    from saturday.repl import Repl

    repl = Repl.__new__(Repl)
    repl.agent = agent
    repl.tui = False
    repl.store = agent.session_store
    repl.initial_history = None
    repl.resumed_id = None
    repl.history_note = []
    repl.pending_images = []
    repl.line_buffer = []
    repl._input = lambda prompt="": next(inputs)
    repl._output = lambda s="": outputs.append(str(s))
    repl.approver = type("AP", (), {"allowed_commands": set(), "denied_commands": set(), "allowed_paths": set(), "__call__": lambda s, c, r: True})()
    repl.file_gate = type("FG", (), {"__call__": staticmethod(lambda name, args: None)})

    rc = repl.run()
    assert rc == 0
    entries = U.load_entries(limit_days=1)
    assert any(e["session"] == "sid1" and e["total_tokens"] == 7 for e in entries)


def test_plan_mode_exposes_new_observation_tools():
    from saturday.tools.base import ToolRegistry

    for name in ("repo_search", "lsp_diagnostics", "lsp_definition"):
        assert name in ToolRegistry.READ_ONLY_TOOLS


def test_estimate_message_tokens_counts_tool_call_arguments():
    from saturday.agent.memory import estimate_message_tokens

    small = {"role": "assistant", "content": None}
    big = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"function": {"name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "z" * 4000})}}
        ],
    }
    assert estimate_message_tokens(big) > estimate_message_tokens(small) + 500


def test_compaction_triggers_sooner_with_heavy_tool_calls():
    """Regression for underestimated prompts: a history of big tool_call
    assistant messages must project above the compact threshold."""
    from saturday.agent.memory import estimate_message_tokens

    big_call = {
        "function": {"name": "shell", "arguments": json.dumps({"command": "echo " + "y" * 3000})}
    }
    msgs = [{"role": "assistant", "tool_calls": [big_call]} for _ in range(6)]
    total = sum(estimate_message_tokens(m) for m in msgs)
    assert total > 6 * 3000 // 4 - 1000  # comfortably more than pre-fix undercount
