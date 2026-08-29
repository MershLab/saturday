"""Regressions for the post-session code-review findings."""
from __future__ import annotations

import json
import time


def test_reap_never_drops_running_subagent_jobs():
    from saturday.tools.jobs import AgentJob, JobManager, make_job_tools

    mgr = JobManager()
    old_running = AgentJob("ag-old-running", "task", {"lines": [], "done": False})
    old_running.created = time.time() - 7200  # way past the hour
    old_done = AgentJob("ag-old-done", "task", {"lines": [], "done": True})
    old_done.created = time.time() - 7200
    mgr.register(old_running)
    mgr.register(old_done)
    job_list, _, _ = make_job_tools(mgr)
    ok, out = job_list.run({})  # run() reaps internally
    assert ok
    ids = [line.split(":")[0] for line in out.splitlines()]
    assert "ag-old-running" in ids, "running subagent must survive reaping"
    assert "ag-old-done" not in ids, "finished subagent past the hour is reaped"


def test_metrics_endpoint_days_parameter_extends_window(tmp_path):

    import urllib.request

    from saturday import usage as U
    from saturday.webui import AppServer, AppState
    import threading

    p = U._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    old_ts = time.time() - 20 * 86_400  # 20 days ago: outside 14d, inside 30d
    row = {
        "ts": old_ts, "day": time.strftime("%Y-%m-%d", time.localtime(old_ts)),
        "provider": "deepseek", "model": "r1", "session": "old",
        "steps": 1, "prompt_tokens": 10, "completion_tokens": 5,
        "total_tokens": 15, "stop_reason": "done",
    }
    p.write_text(json.dumps(row) + "\n", encoding="utf-8")

    app = AppState(store_root=tmp_path / "s")
    srv = AppServer(("127.0.0.1", 0), app, token="t")
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(qs):
            req = urllib.request.Request(base + f"/api/metrics{qs}", headers={"X-Saturday-Token": "t"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))

        d14 = get("?days=14")
        d30 = get("?days=30")
        assert d14["turns"] == 0, "20-day-old entry must be outside the 14d window"
        assert d30["turns"] == 1 and d30["window_days"] == 30, "?days must extend the actual window"
    finally:
        srv.shutdown()


def test_apply_config_registry_cache_no_failure_caching_and_mcp_invalidation(tmp_path, monkeypatch):
    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "s")
    calls = {"n": 0}

    class FlakyAgent:
        def _build_registry(self):
            class R:
                def names(self):
                    return ["shell"]

            return R()

    real_make = app.make_agent

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real_make()

    app.make_agent = flaky
    monkeypatch.setattr("saturday.config.save_config", lambda partial: None)

    app.apply_config({"temperature": 0.5})
    first_state = getattr(app, "_reg_names_cache", None)
    assert first_state is None, "failed probe must not be cached"
    app.apply_config({"temperature": 0.6})
    assert getattr(app, "_reg_names_cache", None), "a later successful probe must populate the cache"
    assert calls["n"] >= 2, "failed probe must NOT be cached"

    # mcp server set change invalidates so new tools become toggleable
    app._reg_names_cache = {"shell"}
    app._reg_names_mcp_key = ("old-server",)
    app.base_cfg.mcp_servers = {"newserver": {"command": "x"}}
    app.apply_config({"temperature": 0.7})
    assert app._reg_names_mcp_key == ("newserver",), "mcp change must refresh the probe key"
    assert "shell" in (app._reg_names_cache or set())


def test_export_compress_then_stamp_hash_matches_payload(tmp_path, capsys, monkeypatch):
    """The shipped record's provenance hash must verify against its own
    (compressed) messages — compress BEFORE stamping."""
    from argparse import Namespace

    import saturday.cli as cli
    from saturday.provenance import content_fingerprint

    src = tmp_path / "eval_runs"
    src.mkdir()
    big_tool = "<tool_response>\n" + "x" * 4000 + "\n</tool_response>"
    rec = {
        "task": "compress me",
        "system": "sys",
        "final_answer": "done",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "# Goal\ncompress me"},
            {"role": "tool", "tool_call_id": "c1", "name": "shell", "content": big_tool},
            {"role": "assistant", "content": "step"},
            {"role": "assistant", "content": "done"},
        ],
    }
    (src / "t1.json").write_text(json.dumps(rec), encoding="utf-8")

    out = tmp_path / "out.jsonl"
    args = Namespace(dir=str(src), out=str(out), keep_unknown=False, compress=800)
    monkeypatch.setattr(cli.AgentConfig, "load", classmethod(lambda cls, o=None: cli.AgentConfig(provider="deepseek")))
    rc = cli.cmd_export(args)
    assert rc == 0
    line = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert "provenance" in line and "compression" in line
    # hash must commit EXACTLY the shipped messages (compressed, pre-stamp)
    expected = content_fingerprint({k: line[k] for k in ("task", "system", "messages", "final_answer") if k in line})
    assert line["provenance"]["content_sha256"] == expected, \
        "compression after stamping would make every compressed export fail tamper checks"
