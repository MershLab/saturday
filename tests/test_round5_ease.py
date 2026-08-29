"""Round-5 ease-of-use regressions: /metrics slash (repl + webui), friendly
did-you-mean provider errors, doctor local-JSON validation."""
from __future__ import annotations

import json
from argparse import Namespace


def test_render_metrics_text_empty_and_full(tmp_path):
    from saturday import usage as U

    # hermetic home -> empty
    text = U.render_metrics_text()
    assert "no usage recorded" in text

    p = U._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    import time as _t

    now = _t.time()
    now_day = _t.strftime("%Y-%m-%d")
    rows = [
        {"ts": now - 100, "day": now_day, "provider": "deepseek", "model": "r1", "session": "a",
         "steps": 1, "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
         "stop_reason": "done"},
        {"ts": now - 50, "day": now_day, "provider": "deepseek", "model": "r1", "session": "b",
         "steps": 2, "prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60,
         "stop_reason": "max_steps"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = U.render_metrics_text()
    assert "2 turns" in out and "180 tokens" in out and "50% completed" in out
    assert "outcomes: done 1, max_steps 1" in out
    assert "deepseek/r1" in out
    assert "nothing leaves this machine" in out


def test_repl_slash_metrics_dispatches():
    from saturday.repl import HELP_TEXT, Repl

    assert "/metrics" in HELP_TEXT

    repl = Repl.__new__(Repl)
    outputs: list[str] = []
    repl._output = lambda s="": outputs.append(str(s))
    ok = repl.dispatch("/metrics")
    assert ok and outputs and ("no usage recorded" in outputs[0] or "turns" in outputs[0])


def test_webui_slash_metrics_notice():
    from saturday.config import AgentConfig
    from saturday.session_runtime import SessionRuntime
    from saturday.webui import handle_slash

    class A:
        cfg = AgentConfig(workspace_root=".")

        def effective_registry(self):
            return type("R", (), {"names": staticmethod(lambda: [])})()

        disabled_tools = set()

        def toggle_tool(self, *a, **k):
            return True, "", False

        plan_mode = False

    rt = SessionRuntime("sid-x", A())
    events = handle_slash(rt, "/metrics")
    assert events and events[0]["t"] == "notice"
    assert "metrics (14d)" in events[0]["s"] or "no usage" in events[0]["s"]


def test_unknown_provider_did_you_mean():
    from saturday.config import AgentConfig

    with __import__("pytest").raises(ValueError) as ei:
        AgentConfig(provider="deepsek").profile()
    msg = str(ei.value)
    assert "did you mean 'deepseek'" in msg
    with __import__("pytest").raises(ValueError) as ei2:
        AgentConfig(provider="zzzzzz").profile()
    assert "did you mean" not in str(ei2.value)


def test_doctor_reports_invalid_local_json(tmp_path, monkeypatch, capsys):
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    home.mkdir()
    (home / "hooks.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)

    args = Namespace(
        provider=None, model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False,
    )
    rc = cli.cmd_doctor(args)
    assert rc == 1
    captured = capsys.readouterr().out
    assert "hooks.json" in captured and "INVALID JSON" in captured


def test_doctor_uses_provider_specific_probe(monkeypatch, tmp_path, capsys):
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://resource.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-test-key")
    monkeypatch.setenv("AZURE_OPENAI_MODEL", "deployment")

    seen = {}

    def fake_probe(profile, api_key, timeout):
        seen.update(name=profile.name, api_key=api_key, timeout=timeout)
        return True, "reachable — 1 models found", ["deployment"]

    monkeypatch.setattr("saturday.llm.probe.probe_connection", fake_probe)
    args = Namespace(
        provider="azure-openai", model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False,
    )
    assert cli.cmd_doctor(args) == 0
    assert seen == {"name": "azure-openai", "api_key": "azure-test-key", "timeout": 8}
    assert "reachable — 1 models found" in capsys.readouterr().out


def test_doctor_offline_skips_probe_and_never_fails_on_endpoint(tmp_path, monkeypatch, capsys):
    """--offline (CI mode): no probe at all, so an absent local provider cannot
    fail the harness check — the probe function must not even be imported."""
    import saturday.cli as cli
    import saturday.config as cfgmod

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    monkeypatch.chdir(workspace)

    def exploding_probe(*a, **k):
        raise AssertionError("probe_connection must not run with --offline")

    monkeypatch.setattr("saturday.llm.probe.probe_connection", exploding_probe)
    args = Namespace(
        provider="ollama", model=None, temperature=None, max_steps=None,
        assistant=False, plan=False, env=None, privacy=False, offline=True,
    )
    assert cli.cmd_doctor(args) == 0
    assert "skipped (--offline)" in capsys.readouterr().out


def test_init_mentions_chat_and_doctor(capsys, tmp_path, monkeypatch):
    from saturday.cli import cmd_init

    monkeypatch.chdir(tmp_path)
    cmd_init(Namespace(force=False))
    captured = capsys.readouterr().out
    assert "doctor" in captured and "app" in captured and "run" in captured
