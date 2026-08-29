"""Three-tier authorization scopes (reserved / approval / autonomous):
enforcement matrix, project wiring through the webui, and agent integration."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from saturday.safety import (  # noqa: E402
    ApprovalPolicy,
    classify_scope as _cs,
    check_command,
    make_approval_hook,
)


class Approver:
    def __init__(self):
        self.calls: list[str] = []
        self.allow = True

    def __call__(self, sig, reason):
        self.calls.append(sig)
        return self.allow


# --------------------------------------------------------------- classify


def test_classify_scope():
    scopes = {"reserved": ["shell"], "approval": ["pointer"], "autonomous": ["read_file"]}
    assert _cs("shell", scopes) == "reserved"
    assert _cs("pointer", scopes) == "approval"
    assert _cs("read_file", scopes) == "autonomous"
    assert _cs("web_fetch", scopes) is None
    assert _cs("shell", None) is None
    assert _cs("shell", {}) is None


# ------------------------------------------------------------- reserved


def test_reserved_asks_even_with_safety_off():
    policy = ApprovalPolicy.from_mode("off")
    scopes = {"reserved": ["shell"]}
    reason = check_command(policy, "shell", {"command": "echo hi"}, scopes=scopes)
    assert reason and "AWAITING APPROVAL unavailable" in reason, "reserved must gate even in off mode"


def test_reserved_with_approver_allow_and_deny():
    scopes = {"reserved": ["shell"]}
    appr = Approver()
    policy = ApprovalPolicy.from_mode("off", approver=appr)
    assert check_command(policy, "shell", {"command": "echo hi"}, scopes=scopes) is None
    assert appr.calls
    appr.allow = False
    reason = check_command(policy, "shell", {"command": "echo hi"}, scopes=scopes)
    assert reason and "user denied" in reason


def test_reserved_hardline_still_wins():
    """Reserved scope asks, but hardline patterns block regardless."""
    appr = Approver()
    policy = ApprovalPolicy.from_mode("off", approver=appr)
    reason = check_command(
        policy, "shell", {"command": "rm -rf / --no-preserve-root"}, scopes={"reserved": ["shell"]}
    )
    assert reason and "HARDLINE" in reason


# ----------------------------------------------------------- autonomous


def test_autonomous_never_asks_in_ask_mode():
    appr = Approver()
    policy = ApprovalPolicy.from_mode("ask", approver=appr)
    scopes = {"autonomous": ["pointer"]}
    assert check_command(policy, "pointer", {"action": "click", "x": 1, "y": 2}, scopes=scopes) is None
    assert appr.calls == [], "autonomous must not prompt"


def test_deny_mode_still_blocks_autonomous():
    policy = ApprovalPolicy.from_mode("deny")
    reason = check_command(
        policy, "pointer", {"action": "click", "x": 1, "y": 2}, scopes={"autonomous": ["pointer"]}
    )
    assert reason and "DENIED" in reason


def test_dangerous_pattern_overrides_autonomous():
    appr = Approver()
    appr.allow = False
    policy = ApprovalPolicy.from_mode("ask", approver=appr)
    reason = check_command(
        policy, "shell", {"command": "sudo apt install thing"}, scopes={"autonomous": ["shell"]}
    )
    assert reason and "user denied" in reason and appr.calls, "dangerous ask beats autonomous"


def test_approval_tier_asks_in_ask_mode_only():
    scopes = {"approval": ["shell"]}
    appr = Approver()
    policy = ApprovalPolicy.from_mode("ask", approver=appr)
    assert check_command(policy, "shell", {"command": "echo hi"}, scopes=scopes) is None
    assert len(appr.calls) == 1
    policy_off = ApprovalPolicy.from_mode("off")
    assert check_command(policy_off, "shell", {"command": "echo hi"}, scopes=scopes) is None
    assert len(appr.calls) == 1, "approval tier does not gate in off mode"


# ------------------------------------------- composition with other gates


def test_bg_only_blocks_foreground_even_if_autonomous():
    policy = ApprovalPolicy.from_mode("off")
    reason = check_command(
        policy,
        "pointer",
        {"action": "click", "x": 1, "y": 2},
        background_only=True,
        scopes={"autonomous": ["pointer"]},
    )
    assert reason and "BACKGROUND-ONLY" in reason


def test_bg_delivery_allowed_when_autonomous():
    policy = ApprovalPolicy.from_mode("off")
    assert (
        check_command(
            policy,
            "pointer",
            {"action": "click", "x": 1, "y": 2, "window": "tally"},
            background_only=True,
            scopes={"autonomous": ["pointer"]},
        )
        is None
    )


def test_reserved_gates_any_tool_including_readonly():
    policy = ApprovalPolicy.from_mode("off")
    reason = check_command(policy, "web_fetch", {"url": "https://x"}, scopes={"reserved": ["web_fetch"]})
    assert reason and "AWAITING APPROVAL" in reason, "reserved applies to non-gated tools too"
    appr = Approver()
    policy2 = ApprovalPolicy.from_mode("off", approver=appr)
    assert check_command(policy2, "web_fetch", {"url": "https://x"}, scopes={"reserved": ["web_fetch"]}) is None


def test_hook_passes_scopes():
    policy = ApprovalPolicy.from_mode("off")
    hook = make_approval_hook(policy, scopes={"reserved": ["shell"]})
    reason = hook("shell", {"command": "echo x"})
    assert reason and "AWAITING APPROVAL" in reason


# ------------------------------------------------- agent integration


def test_agent_reserved_scope_blocks_tool_at_runtime():
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from fakes import make_scripted_model

    turns = [{"tool_calls": [{"name": "shell", "arguments": {"command": "echo blocked-echo"}}]}, {"content": "done"}]
    fake = make_scripted_model(turns)
    cfg = AgentConfig(
        provider="openai",
        model="m",
        safety_mode="off",
        auth_scopes={"reserved": ["shell"]},
    )
    agent = Agent(cfg=cfg, client=fake, session_store=None)
    agent._ensure_client = lambda: fake
    traj = agent.run("run echo")
    assert traj.stop_reason == "done"
    tool_msgs = [m for m in traj.messages() if m.get("role") == "tool"]
    assert tool_msgs and "AWAITING APPROVAL unavailable" in str(tool_msgs[0].get("content")), (
        "reserved scope must gate shell even with safety off"
    )


# ------------------------------------------------------- project wiring


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import saturday.config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


def _server(app):

    from saturday.webui import AppServer

    http = AppServer(("127.0.0.1", 0), app, token="tok")
    base = f"http://127.0.0.1:{http.server_address[1]}"
    threading.Thread(target=http.serve_forever, daemon=True).start()

    def call(path, method="GET", payload=None):
        import json

        import urllib.error

        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(base + path, data=data, method=method)
        r.add_header("X-Saturday-Token", "tok")
        r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
        if path == "/api/chat":
            lines = [json.loads(l) for l in body.splitlines() if l.strip()]
            return 200, lines
        return 200, json.loads(body)

    def stream_chat(payload):
        return call("/api/chat", "POST", payload)[1]

    return call, http


def _make_app(tmp_path: Path, turns=None):
    from fakes import make_scripted_model

    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path)},
    )
    fake = make_scripted_model(turns or [{"content": "ok"}])
    orig = app._new_agent

    def patched(cfg):
        agent = orig(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    return app


def test_project_scopes_roundtrip_and_enforcement(tmp_path: Path):
    app = _make_app(tmp_path)
    call, http = _server(app)
    try:
        status, data = call(
            "/api/projects",
            "POST",
            {"name": "Scoped", "scopes": {"reserved": ["shell"], "autonomous": ["read_file", "web_fetch"]}},
        )
        assert status == 200 and data["project"]["scopes"]["reserved"] == ["shell"]

        status, _ = call("/api/chat", "POST", {"text": "start", "project_id": data["project"]["id"]})
        assert status == 200
        sid = app.store.list_sessions()[0]["id"]
        rt = app.runtime_for(sid)
        assert rt.agent.cfg.auth_scopes == {"reserved": ["shell"], "autonomous": ["read_file", "web_fetch"]}

        # validation: unknown tier rejected, state untouched
        status, _ = call(f"/api/project/{data['project']['id']}", "PATCH", {"scopes": {"bogus": ["shell"]}})
        assert status == 400
        assert app.projects.get(data["project"]["id"]).scopes.get("reserved") == ["shell"]

        # patch replaces scopes
        status, data2 = call(f"/api/project/{data['project']['id']}", "PATCH", {"scopes": {"approval": ["pointer"]}})
        assert status == 200 and data2["project"]["scopes"] == {"approval": ["pointer"]}
    finally:
        http.shutdown()
        http.server_close()


def test_global_scopes_via_config_endpoint(tmp_path: Path):
    app = _make_app(tmp_path)
    call, http = _server(app)
    try:
        status, data = call("/api/config", "POST", {"auth_scopes": {"reserved": ["shell"], "autonomous": ["todo"]}})
        assert status == 200 and data["auth_scopes"] == {"reserved": ["shell"], "autonomous": ["todo"]}
        assert app.base_cfg.auth_scopes["reserved"] == ["shell"]

        status, _ = call("/api/config", "POST", {"auth_scopes": {"nope": ["x"]}})
        assert status == 400
    finally:
        http.shutdown()
        http.server_close()


# ------------------------------------------------- review fixes (regressions)


def test_reserved_dangerous_single_prompt():
    """A reserved-scope command matching a dangerous pattern asks exactly once."""
    appr = Approver()
    policy = ApprovalPolicy.from_mode("off", approver=appr)
    reason = check_command(
        policy, "shell", {"command": "sudo apt install thing"}, scopes={"reserved": ["shell"]}
    )
    assert reason is None and len(appr.calls) == 1, "one decision per action, no double prompt"
    appr.allow = False
    reason = check_command(
        policy, "shell", {"command": "sudo apt install thing"}, scopes={"reserved": ["shell"]}
    )
    assert reason and "user denied" in reason and len(appr.calls) == 2
