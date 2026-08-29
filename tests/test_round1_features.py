"""Round-1 feature regressions: provenance marking, post-edit verify hook,
persistent desktop-tool approval prefixes, metrics v2, saturday init,
export compression, and the webui config exposure of the new knobs."""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# F1: provenance marking


def test_stamp_record_adds_provenance_and_commits_content():
    from saturday.provenance import stamp_record

    rec = {"task": "t", "messages": [{"role": "user", "content": "hi"}], "final_answer": "ans"}
    stamped = stamp_record(rec, provider="deepseek", model="r1", session_id="s1")
    assert rec.get("provenance") is None  # input never mutated
    prov = stamped["provenance"]
    assert prov["ai_generated"] is True
    assert prov["generated_with"] == "Saturday"
    assert prov["provider"] == "deepseek"
    assert prov["model"] == "r1"
    assert prov["session_id"] == "s1"
    assert len(prov["content_sha256"]) == 64

    tampered = stamp_record({**rec, "final_answer": "edited"}, provider="deepseek", model="r1")
    assert tampered["provenance"]["content_sha256"] != prov["content_sha256"]


def test_visible_footer_only_in_visible_mode_and_idempotent():
    from saturday.provenance import apply_visible_footer

    assert apply_visible_footer("answer", "metadata") == "answer"
    assert apply_visible_footer("answer", None) == "answer"
    marked = apply_visible_footer("answer", "visible")
    assert marked.startswith("answer") and "AI-assisted" in marked
    assert apply_visible_footer(marked, "visible") == marked  # never doubled
    assert apply_visible_footer("", "visible") == ""


def test_config_provenance_marking_validation_and_env(monkeypatch):
    from saturday.config import AgentConfig

    cfg = AgentConfig.load(overrides={"provenance_marking": "bogus"})
    assert cfg.provenance_marking == "metadata"
    monkeypatch.setenv("SATURDAY_PROVENANCE", "VISIBLE")
    cfg2 = AgentConfig.load(overrides={"provider": "deepseek"})
    assert cfg2.provenance_marking == "visible"


def test_eval_runner_stamps_saved_trajectory(tmp_path):
    from saturday.eval.runner import EvalCase, EvalRunner, contains_any
    from saturday.types import Trajectory

    class FakeAgent:
        def __init__(self):
            from saturday.config import AgentConfig

            self.cfg = AgentConfig(provider="deepseek", model="deepseek-reasoner", provenance_marking="metadata")

        def run(self, task):
            t = Trajectory(task=task, system_prompt="sys", final_answer="forty two", stop_reason="done")
            return t

    runner = EvalRunner(lambda: FakeAgent(), out_dir=tmp_path)
    results = runner.run([EvalCase(id="p1", task="do", verifier=contains_any("forty"))])
    assert results[0].reward == 1.0
    saved = json.loads((tmp_path / "p1.json").read_text(encoding="utf-8"))
    assert saved["provenance"]["provider"] == "deepseek"
    assert saved["provenance"]["model"] == "deepseek-reasoner"

    # marking off -> plain record
    class OffAgent(FakeAgent):
        def __init__(self):
            super().__init__()
            self.cfg.provenance_marking = "off"

    runner2 = EvalRunner(lambda: OffAgent(), out_dir=tmp_path)
    runner2.run([EvalCase(id="p2", task="do", verifier=contains_any("forty"))])
    assert "provenance" not in json.loads((tmp_path / "p2.json").read_text(encoding="utf-8"))


def test_cli_run_json_out_stamps_and_visible_footer(capsys, tmp_path, monkeypatch):
    from saturday.cli import cmd_run
    from saturday.types import Trajectory

    captured: dict = {}

    def fake_agent_init(self, cfg=None, **kw):
        from saturday.config import AgentConfig

        self.cfg = cfg or AgentConfig.load()
        captured["cfg"] = self.cfg

    def fake_agent_run(self, task, **kw):
        return Trajectory(task=task, system_prompt="s", final_answer="done answer", stop_reason="done")

    monkeypatch.setattr("saturday.agent.core.Agent.__init__", fake_agent_init)
    monkeypatch.setattr("saturday.agent.core.Agent.run", fake_agent_run)

    out = tmp_path / "traj.json"
    args = Namespace(
        task="say hi",
        quiet=True,
        session=None,
        json_out=str(out),
        ci=False,
        detach=False,
        background=False,
        images=None,
        env=None,
        provider=None,
        model=None,
        temperature=None,
        max_steps=None,
        assistant=False,
        plan=False,
        max_run_tokens=None,
        disabled_tools=None,
    )
    rc = cmd_run(args)
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["provenance"]["generated_with"] == "Saturday"
    assert data["provenance"]["provider"] == captured["cfg"].provider


# ---------------------------------------------------------------------------
# F2: post-edit external verify hook


def _write_helper(tmp_path: Path) -> Path:
    helper = tmp_path / "verify_helper.py"
    helper.write_text(
        "import sys\n"
        "path = sys.argv[1]\n"
        "print('checked ' + path)\n"
        "sys.exit(int(sys.argv[2]))\n",
        encoding="utf-8",
    )
    return helper


def test_write_file_external_verify_success_and_failure(tmp_path):
    from saturday.tools.files import EditFile, WriteFile

    root = tmp_path
    helper = _write_helper(tmp_path)
    ok_cmd = f'"{sys.executable}" "{helper}" {{path}} 0'
    bad_cmd = f'"{sys.executable}" "{helper}" {{path}} 3'

    w = WriteFile(root=str(root), verify_command=ok_cmd)
    ok, msg = w.run({"path": "a.py", "content": "x = 1\n"})
    assert ok and "[verify ok]" in msg and "checked" in msg

    bad_writer = WriteFile(root=str(root), verify_command=bad_cmd)
    ok, msg = bad_writer.run({"path": "b.py", "content": "y = 2\n"})
    assert ok and "exit=3" in msg  # failing hook still non-blocking

    target = root / "c.txt"
    target.write_text("hello\n", encoding="utf-8")
    e = EditFile(root=str(root), verify_command=bad_cmd)
    ok, msg = e.run({"path": "c.txt", "old_string": "hello", "new_string": "world"})
    assert ok and "exit=3" in msg and "world" in target.read_text(encoding="utf-8")


def test_syntax_error_skips_external_verify(tmp_path):
    from saturday.tools.files import WriteFile

    helper = _write_helper(tmp_path)
    calls = []

    real = sys.executable

    def counting_cmd():
        return f'"{real}" "{helper}" {{path}} 0'

    w = WriteFile(root=str(tmp_path), verify_command=counting_cmd())
    ok, msg = w.run({"path": "broken.py", "content": "def oops(:\n"})
    assert ok
    assert "[verify] WARNING" in msg and "syntax error" in msg
    assert "exit=0" not in msg  # external hook skipped when ast already failed


def test_verify_command_wired_from_cfg(tmp_path):
    from saturday.config import AgentConfig
    from saturday.plugins import _core_tools

    cfg = AgentConfig(workspace_root=str(tmp_path), verify_command=f'"{sys.executable}" -c "print(1)"')
    tools = _core_tools(cfg)
    writers = [t for t in tools if getattr(t, "name", "") in ("write_file", "edit_file")]
    assert len(writers) == 2 and all(t.verify_command for t in writers)


# ---------------------------------------------------------------------------
# F3: persistent approval prefixes for gated desktop tools


def test_allow_rule_prefix_matches_pointer_signature():
    from saturday.safety import ApprovalPolicy, check_command

    policy = ApprovalPolicy.from_mode("ask", allow_rules=["click*"])
    args = {"action": "click", "target": "btnOK"}
    assert check_command(policy, "pointer", args) is None  # rule matched -> no ask

    policy2 = ApprovalPolicy.from_mode("ask", allow_rules=["click exact"])
    asked = {"n": 0}

    def approver(sig, reason):
        asked["n"] += 1
        return True

    policy2.approver = approver
    assert check_command(policy2, "pointer", {"action": "click", "x": 1, "y": 2}) is None
    assert asked["n"] == 1  # not matched by the exact rule -> asked exactly once


def test_desktop_prefix_rules_cannot_bypass_deny_or_hardline_paths():
    from saturday.safety import ApprovalPolicy, check_command

    deny_policy = ApprovalPolicy.from_mode("deny", allow_rules=["click*"])
    reason = check_command(deny_policy, "pointer", {"action": "click", "target": "x"})
    assert reason and "DENIED" in reason

    bg_policy = ApprovalPolicy.from_mode("ask", allow_rules=["move*"], )
    blocked = check_command(bg_policy, "pointer", {"action": "move", "x": 5}, background_only=True)
    assert blocked and "BACKGROUND-ONLY" in blocked  # structural gate beats rules


def test_webapprover_persists_action_signature_for_desktop_tools(tmp_path):
    """An 'always' on a pointer ask writes the SIGNATURE so the next identical
    action skips the ask (per-(action,target) approval memory end-to-end)."""
    from saturday.approvals_store import load_rules
    from saturday.safety import ApprovalPolicy, check_command
    from saturday.session_runtime import WebApprover

    events: list[dict] = []
    approver = WebApprover(events.append, ttl=5, scope="t1")
    policy = ApprovalPolicy.from_mode("ask")
    policy.approver = approver

    def resolve_after_ask():
        aids = [e["id"] for e in events if e.get("t") == "approval"]
        assert approver.resolve(aids[-1], "always")

    import threading

    def ask_async():
        result: dict = {}

        def worker():
            result["r"] = check_command(policy, "pointer", {"action": "click", "target": "okBtn"})

        th = threading.Thread(target=worker)
        th.start()
        resolve_after_ask()
        th.join(5)

    ask_async()
    assert load_rules()["allow"], "signature should be persisted"
    saved = load_rules()["allow"][-1]
    assert saved.startswith("click")
    # second identical action must not ask anymore
    assert check_command(policy, "pointer", {"action": "click", "target": "okBtn"}) is None


# ---------------------------------------------------------------------------
# F4: usage metrics v2


def test_usage_summary_metrics_fields(tmp_path):
    from saturday import usage as U

    U._path.__wrapped__ if False else None  # noqa: B011 (keep linters calm)
    monkey_target = U

    entries = [
        {"ts": 1, "day": "2026-08-20", "provider": "deepseek", "model": "r1", "session": "a",
         "steps": 2, "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "stop_reason": "done"},
        {"ts": 2, "day": "2026-08-21", "provider": "openai", "model": "gpt-4o", "session": "b",
         "steps": 1, "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "stop_reason": "max_steps"},
    ]
    import time as _t

    now = _t.time()
    for i, e in enumerate(entries):
        e["ts"] = now - 3600 * (i + 1)
    p = monkey_target._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    s = monkey_target.usage_summary()
    assert s["turns"] == 2
    assert s["success_rate"] == 0.5
    assert s["avg_tokens_per_turn"] == int((150 + 15) / 2)
    assert s["stop_reasons"] == {"done": 1, "max_steps": 1}
    assert {"provider": "deepseek", "turns": 1} in s["providers"]
    assert s["est_cost_usd_14d"] is not None


def test_api_metrics_endpoint(ui_app_factory):
    client, _ = ui_app_factory()
    status, body = client.get_json("/api/metrics")
    assert status == 200
    assert "success_rate" in body and "window_days" in body


# ---------------------------------------------------------------------------
# F5: saturday init


def test_init_scaffolds_idempotently_with_force(tmp_path, capsys, monkeypatch):
    from saturday.cli import cmd_init

    monkeypatch.chdir(tmp_path)
    assert cmd_init(Namespace(force=False)) == 0
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / ".saturday" / "mcp.json.example").is_file()
    assert (tmp_path / ".saturday" / "hooks.json.example").is_file()

    (tmp_path / "AGENTS.md").write_text("CUSTOM", encoding="utf-8")
    assert cmd_init(Namespace(force=False)) == 0
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "CUSTOM"

    assert cmd_init(Namespace(force=True)) == 0
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") != "CUSTOM"


def test_init_registered_in_parser():
    from saturday.cli import build_parser

    ns = build_parser().parse_args(["init"])
    assert getattr(ns, "force", False) is False


# ---------------------------------------------------------------------------
# F6: export compression


def _big_record(n_tool_msgs: int = 12) -> dict:
    msgs = [{"role": "system", "content": "sys prompt"}]
    msgs.append({"role": "user", "content": "# Goal\ncompress me"})
    for i in range(n_tool_msgs):
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"c{i}",
                "name": "shell",
                "content": "<tool_response>\n" + ("x" * 4000) + f"\n</tool_response> #{i}",
            }
        )
        msgs.append({"role": "assistant", "content": f"step {i} done"})
    msgs.append({"role": "assistant", "content": "final answer here"})
    return {"task": "compress me", "system": "sys prompt", "messages": msgs, "final_answer": "final answer here"}


def test_compress_record_hits_budget_and_preserves_bookends():
    from saturday.eval.compress import compress_record
    from saturday.agent.memory import estimate_tokens

    rec = _big_record()
    before = sum(estimate_tokens(str(m.get("content") or "")) for m in rec["messages"])
    tiny_budget = 1200
    out = compress_record(rec, token_budget=tiny_budget)
    after = sum(estimate_tokens(str(m.get("content") or "")) for m in out["messages"])
    assert after < before
    texts = [str(m.get("content") or "") for m in out["messages"]]
    assert any("omitted during export compression" in t for t in texts)
    # bookends preserved verbatim
    assert out["messages"][1]["content"] == "# Goal\ncompress me"
    assert out["messages"][-1]["content"] == "final answer here"
    assert out["compression"]["before_tokens"] >= out["compression"]["after_tokens"]


def test_compress_noop_when_under_budget_or_tiny():
    from saturday.eval.compress import compress_record

    rec = _big_record(2)
    assert compress_record(rec, token_budget=10**9)["messages"] == rec["messages"]
    assert compress_record(rec, token_budget=0) is rec


def test_export_compress_flag_wired():
    from saturday.cli import build_parser

    ns = build_parser().parse_args(["export", "--compress", "8000"])
    assert ns.compress == 8000


# ---------------------------------------------------------------------------
# webui config exposure for the new knobs


def test_state_and_apply_config_roundtrip_new_knobs(ui_app_factory):
    client, app = ui_app_factory()
    status, body = client.get_json("/api/state")
    assert status == 200
    assert body["provenance_marking"] == "metadata"
    assert body["verify_command"] == ""

    status, resp = client.post_json("/api/config", {"provenance_marking": "visible", "verify_command": "pytest -q"})
    assert status == 200
    assert "provenance_marking" in resp["applied"] and "verify_command" in resp["applied"]

    status, body2 = client.get_json("/api/state")
    assert body2["provenance_marking"] == "visible"
    assert body2["verify_command"] == "pytest -q"

    status, err = client.post_json("/api/config", {"provenance_marking": "loud"})
    assert status == 400


# ---------------------------------------------------------------------------
# shared fixtures


@pytest.fixture
def ui_app_factory():
    """Hermetic AppState + JSON client against a bound ephemeral server."""
    import threading

    from saturday.webui import AppServer, AppState

    made: list = []

    def make():
        app = AppState(cfg_overrides={"workspace_root": str(Path.cwd())})
        srv = AppServer(("127.0.0.1", 0), app, token="")
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        made.append(srv)

        class Client:
            base = f"http://127.0.0.1:{port}"

            def get_json(self, path):
                import urllib.request

                try:
                    with urllib.request.urlopen(self.base + path, timeout=10) as r:
                        return r.status, json.loads(r.read().decode("utf-8"))
                except Exception as exc:
                    code = getattr(exc, "code", 500)
                    try:
                        body = json.loads(exc.read().decode("utf-8"))
                    except Exception:
                        body = {"error": str(exc)}
                    return code, body

            def post_json(self, path, payload):
                import urllib.request

                req = urllib.request.Request(
                    self.base + path,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as r:
                        return r.status, json.loads(r.read().decode("utf-8"))
                except Exception as exc:
                    code = getattr(exc, "code", 500)
                    try:
                        body = json.loads(exc.read().decode("utf-8"))
                    except Exception:
                        body = {"error": str(exc)}
                    return code, body

        return Client(), app

    yield make
    for srv in made:
        srv.shutdown()
