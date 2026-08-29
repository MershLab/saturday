"""Security review round 1 fixes:

1. Agent-writable lifecycle hooks: ``write_file``/``edit_file`` must refuse the
   whole set of security-relevant ``.saturday/`` state files (hooks.json runs
   shell commands on every tool call; config.json flips safety_mode;
   approvals.json is the agent's own authorization store; ...).
2. Journal restores (/revert, /rewind) must refuse journal entries whose
   target is one of those privileged files (entries are model-influenced
   data; a poisoned entry must not plant hook content).
3. webui.serve() prints a warning when auth is disabled (--no-token).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))

PRIVILEGED_TARGETS = [
    ".saturday/mcp.json",
    ".saturday/hooks.json",
    ".saturday/config.json",
    ".saturday/approvals.json",
    ".saturday/schedules.json",
    ".saturday/trusted_projects.json",
    ".saturday/projects.json",
    ".saturday/usage.jsonl",
    ".saturday/file_journal.jsonl",
    ".saturday/SOUL.md",
]


# ------------------------------------------------------------- privileged writes

def test_write_file_refuses_all_state_files(tmp_path):
    from saturday.tools.files import WriteFile

    tool = WriteFile(root=str(tmp_path))
    for bad in PRIVILEGED_TARGETS:
        ok, msg = tool.run({"path": bad, "content": "{}"})
        assert not ok and "privileged" in msg, bad
        assert not (tmp_path / bad).exists(), bad


def test_edit_file_refuses_all_state_files(tmp_path):
    from saturday.tools.files import EditFile

    target = tmp_path / ".saturday" / "hooks.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    tool = EditFile(root=str(tmp_path))
    ok, msg = tool.run({"path": ".saturday/hooks.json", "old_string": "{", "new_string": "!!"})
    assert not ok and "privileged" in msg
    assert target.read_text(encoding="utf-8") == "{}"


def test_nested_and_dotdot_privileged_paths_refused(tmp_path):
    from saturday.tools.files import WriteFile

    tool = WriteFile(root=str(tmp_path))
    for bad in ("deep/.saturday/hooks.json", ".saturday/sub/../hooks.json", "x/../.env"):
        ok, msg = tool.run({"path": bad, "content": "x"})
        assert not ok and "privileged" in msg, bad


def test_benign_writes_still_allowed(tmp_path):
    from saturday.tools.files import WriteFile

    tool = WriteFile(root=str(tmp_path))
    for good in ("src/app.py", ".saturday/MEMORY.md", ".saturday/shots/a.png", ".saturday/spill/x.log"):
        ok, msg = tool.run({"path": good, "content": "x"})
        assert ok, (good, msg)


# ------------------------------------------------------------- journal restores

def _journal_with_entry(root: Path, target: Path, before: str, existed: bool = True) -> None:
    jp = root / ".saturday" / "file_journal.jsonl"
    jp.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": 0.0, "tool": "write_file", "path": str(target), "existed": existed, "before": before}
    if not existed:
        entry.pop("before")
    jp.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def test_revert_refuses_privileged_target(tmp_path):
    from saturday.tools.journal import restore_entry

    hooks = tmp_path / ".saturday" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text("{}", encoding="utf-8")
    _journal_with_entry(tmp_path, hooks, json.dumps({"pre_tool_call": ["start calc"]}))
    ok, msg = restore_entry(tmp_path, 0)
    assert not ok and "privileged" in msg
    assert hooks.read_text(encoding="utf-8") == "{}"


def test_rewind_refuses_privileged_target(tmp_path):
    from saturday.tools.journal import restore_to_length

    hooks = tmp_path / ".saturday" / "hooks.json"
    hooks.parent.mkdir(parents=True, exist_ok=True)
    hooks.write_text("{}", encoding="utf-8")
    _journal_with_entry(tmp_path, hooks, json.dumps({"pre_tool_call": ["start calc"]}))
    ok, msg = restore_to_length(tmp_path, 0)
    assert not ok and "privileged" in msg
    assert hooks.read_text(encoding="utf-8") == "{}"


def test_revert_still_restores_normal_files(tmp_path):
    from saturday.tools.journal import restore_entry

    victim = tmp_path / "data.txt"
    victim.write_text("before", encoding="utf-8")
    _journal_with_entry(tmp_path, victim, "before")
    victim.write_text("after", encoding="utf-8")
    ok, msg = restore_entry(tmp_path, 0)
    assert ok, msg
    assert victim.read_text(encoding="utf-8") == "before"


# ------------------------------------------------------------- webui token warn

def test_serve_warns_when_auth_disabled(capsys, monkeypatch, tmp_path):
    import saturday.config as cfgmod
    import saturday.webui as webui

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / ".saturday-home")
    monkeypatch.setattr(webui, "_port_in_use", lambda host, port: False)

    class _StubServer:
        def __init__(self, address, app, token=""):
            self.server_address = (address[0], address[1])
            self.token = token

        def serve_forever(self, poll_interval: float = 0.5):
            raise KeyboardInterrupt  # exit the serve loop immediately

        def server_close(self):
            pass

    monkeypatch.setattr(webui, "AppServer", _StubServer)
    env_path = tmp_path / "no.env"
    common = {"open_window": False, "env_path": str(env_path)}
    rc = webui.serve(token="", **common)
    assert rc == 0
    out = capsys.readouterr().out
    assert "auth disabled" in out

    rc = webui.serve(token=None, **common)
    out = capsys.readouterr().out
    assert "auth disabled" not in out
