"""Fully-autonomous mode ("yolo"): zero approval prompts, hardline floor kept."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# ------------------------------------------------------------- mode plumbing

def test_from_mode_normalizes_yolo_aliases():
    from saturday.safety import ApprovalPolicy, AUTONOMOUS_MODE

    for alias in ("yolo", "auto", "autonomous", "YOLO"):
        assert ApprovalPolicy.from_mode(alias).mode == AUTONOMOUS_MODE
    assert ApprovalPolicy.from_mode("ask").mode == "ask"


def test_config_load_normalizes_aliases(monkeypatch, tmp_path):
    from saturday import config as cfgmod

    (tmp_path / "config.json").write_text('{"safety_mode": "yolo"}', encoding="utf-8")
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("saturday.mcp_plugin.load_mcp_config", lambda *a, **k: {})
    cfg = cfgmod.AgentConfig.load()
    assert cfg.safety_mode == "autonomous"


# ------------------------------------------------------------ what still blocks

def test_hardline_still_blocks_in_autonomous():
    from saturday.safety import ApprovalPolicy, check_command

    policy = ApprovalPolicy.from_mode("yolo")
    reason = check_command(policy, "shell", {"command": "rm -rf /"})
    assert reason and "HARDLINE" in reason


def test_deny_rules_still_bind_in_autonomous():
    from saturday.safety import ApprovalPolicy, check_command

    policy = ApprovalPolicy.from_mode("yolo", deny_rules=["npm publish*"])
    reason = check_command(policy, "shell", {"command": "npm publish --access public"})
    assert reason and "DENIED" in reason


def test_background_only_structural_gating_survives_yolo():
    from saturday.safety import ApprovalPolicy, check_command

    policy = ApprovalPolicy.from_mode("yolo")
    reason = check_command(policy, "pointer", {"action": "click", "x": 5, "y": 5}, background_only=True)
    assert reason and "BACKGROUND-ONLY" in reason


# ------------------------------------------------------------------ what stops asking

def test_dangerous_patterns_no_longer_ask_in_autonomous():
    from saturday.safety import ApprovalPolicy, check_command

    asked = {"n": 0}

    def approver(cmd, why):
        asked["n"] += 1
        return False

    # ask-mode: sudo triggers an approval (fail-closed when no approver)
    ask_policy = ApprovalPolicy.from_mode("ask")
    assert check_command(ask_policy, "shell", {"command": "sudo apt install x"}) is not None
    # yolo: passes with NO approver at all, nothing asked
    yolo = ApprovalPolicy.from_mode("yolo")
    assert check_command(yolo, "shell", {"command": "sudo apt install x"}) is None


def test_guardrails_no_longer_block_or_ask_in_autonomous():
    from saturday.safety import ApprovalPolicy, check_command

    # previously: GUARDRAIL BLOCK (fail-closed with no approver)
    yolo = ApprovalPolicy.from_mode("yolo")
    assert check_command(yolo, "shell", {"command": "DROP TABLE users"}, guardrails=True) is None
    assert check_command(yolo, "shell", {"command": "git reset --hard HEAD~3"}, guardrails=True) is None


def test_desktop_tools_pass_without_approver_in_autonomous():
    from saturday.safety import ApprovalPolicy, check_command

    yolo = ApprovalPolicy.from_mode("yolo")
    assert check_command(yolo, "pointer", {"action": "click", "x": 1, "y": 2}) is None
    assert check_command(yolo, "app_open", {"target": "notepad"}) is None


def test_reserved_scope_passes_in_autonomous_without_approver():
    from saturday.safety import ApprovalPolicy, check_command

    yolo = ApprovalPolicy.from_mode("yolo")
    scopes = {"reserved": ["shell"]}
    assert check_command(yolo, "shell", {"command": "echo hi"}, scopes=scopes) is None
    # legacy off-mode keeps the reserved governance ask (fail-closed)
    off = ApprovalPolicy.from_mode("off")
    reason = check_command(off, "shell", {"command": "echo hi"}, scopes=scopes)
    assert reason and "APPROVAL" in reason


# ------------------------------------------------------------------------- gates

def test_file_gates_auto_approve_when_wired(tmp_path, monkeypatch):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.safety import is_autonomous
    from saturday.session_runtime import SessionRuntime
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    agent = Agent(cfg=AgentConfig(provider="openai", model="m",
                                  workspace_root=str(tmp_path), safety_mode="yolo"),
                  safety=True, session_store=store)
    assert is_autonomous(agent.cfg.safety_mode)
    repl = Repl(agent, store=store, output_fn=lambda *a, **k: None)
    assert repl.file_gate.auto_approve is True
    assert repl.file_gate("write_file", {"path": str(tmp_path / "x.txt"), "content": "hi"}) is None

    rt = SessionRuntime("sid", agent)
    assert rt.file_gate.auto_approve is True
    assert rt.file_gate("edit_file", {"path": "x.txt", "old_string": "a", "new_string": "b"}) is None


def test_repl_yolo_toggle_round_trip(tmp_path):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.repl import Repl
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    agent = Agent(cfg=AgentConfig(provider="openai", model="m", workspace_root=str(tmp_path)),
                  safety=False, session_store=store)
    repl = Repl(agent, store=store, output_fn=lambda *a, **k: None)
    collected: list[str] = []
    repl._output = lambda *a, **k: collected.append(" ".join(str(x) for x in a))

    assert repl.dispatch("/yolo") is True
    assert repl.file_gate.auto_approve is True
    assert repl.dispatch("/yolo") is True
    assert repl.file_gate.auto_approve is False
    assert any("yolo ON" in c for c in collected) and any("yolo OFF" in c for c in collected)


def test_cli_overrides_map_yolo_flag():
    import argparse

    from saturday.cli import _overrides

    out = _overrides(argparse.Namespace(provider=None, model=None, temperature=None,
                                        max_steps=None, assistant=False, plan=False,
                                        max_run_tokens=None, disabled_tools=None, yolo=True))
    assert out["safety_mode"] == "autonomous"
    out_off = _overrides(argparse.Namespace(provider=None, model=None, temperature=None,
                                            max_steps=None, assistant=False, plan=False,
                                            max_run_tokens=None, disabled_tools=None))
    assert out_off["safety_mode"] is None


def test_webui_config_accepts_autonomous(tmp_path):
    """The settings whitelist must accept the new mode end-to-end."""
    import threading

    import json
    import urllib.request

    from saturday.webui import AppServer, AppState

    app = AppState(cfg_overrides={"workspace_root": str(Path.cwd())})
    srv = AppServer(("127.0.0.1", 0), app, token="")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/config",
        data=json.dumps({"safety_mode": "yolo"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
        assert body["safety_mode"] == "autonomous"
    finally:
        srv.server_close()
