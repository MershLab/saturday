"""Destructive-action guardrails: pattern coverage, precedence vs safety-off,
approver wiring, DB-file auto-backup in the shell tool, and the config knob."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from saturday.safety import ApprovalPolicy, check_command, guardrail_reason  # noqa: E402
from saturday.tools.shell import ShellTool  # noqa: E402


# --------------------------------------------------------------- patterns

@pytest.mark.parametrize(
    "cmd",
    [
        "DROP DATABASE prod;",
        "drop schema main",
        "sqlite3 app.db 'DROP TABLE users'",
        "TRUNCATE TABLE events",
        "redis-cli FLUSHALL",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "rm -rf build/",
        "rm -r old_stuff",
        "Remove-Item ./node_modules -Recurse -Force",
        "del /s /q C:\\temp\\data",
        "rd /s data",
        "shred -u secrets.txt",
    ],
)
def test_guardrail_pattern_hits(cmd):
    assert guardrail_reason(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -la",
        "git status",
        "git clean -n",
        "rm notes.txt",  # single-file rm is handled by backup, not a block
        "DELETE FROM logs WHERE ts < 100",
        "UPDATE users SET name = 'x' WHERE id = 3",
        "echo dropping-the-idea",  # word-boundary safety
    ],
)
def test_guardrail_clean_commands_pass(cmd):
    assert guardrail_reason(cmd) is None, cmd


def test_sql_missing_where_detected():
    assert "without WHERE" in guardrail_reason("DELETE FROM logs")
    assert "without WHERE" in guardrail_reason("UPDATE users SET admin = 1")
    # multi-statement: only the unbounded one trips
    both = guardrail_reason("UPDATE t SET a = 1; DELETE FROM logs WHERE x = 1")
    assert both and both.startswith("UPDATE")


# --------------------------------------------------------------- policy flow

def test_off_mode_blocks_without_approver_when_guardrails_on():
    pol = ApprovalPolicy.from_mode("off")
    reason = check_command(pol, "shell", {"command": "DROP DATABASE prod"}, guardrails=True)
    assert reason and reason.startswith("GUARDRAIL BLOCK") and "destructive_guardrails" in reason


def test_off_mode_asks_approver_and_respects_decision():
    allowed = ApprovalPolicy.from_mode("off", approver=lambda c, r: True)
    denied = ApprovalPolicy.from_mode("off", approver=lambda c, r: False)
    assert check_command(allowed, "shell", {"command": "DROP TABLE users"}, guardrails=True) is None
    out = check_command(denied, "shell", {"command": "DROP TABLE users"}, guardrails=True)
    assert out and "user denied" in out


def test_guardrails_disabled_restores_legacy_off_mode():
    pol = ApprovalPolicy.from_mode("off")
    assert check_command(pol, "shell", {"command": "DROP DATABASE prod"}, guardrails=False) is None


def test_python_tool_gated_too():
    pol = ApprovalPolicy.from_mode("off")
    code = 'import os; cur.execute("DROP TABLE users")'
    assert check_command(pol, "python", {"code": code}, guardrails=True)


def test_ask_mode_still_works_with_guardrails_off_for_normal_cmds():
    seen: list[tuple[str, str]] = []
    pol = ApprovalPolicy.from_mode("ask", approver=lambda c, r: seen.append((c, r)) or True)
    assert check_command(pol, "shell", {"command": "echo hi"}, guardrails=False) is None
    assert not seen


def test_python_rmtree_guardrailed():
    pol = ApprovalPolicy.from_mode("off")
    code = 'import shutil; shutil.rmtree("build")'
    assert check_command(pol, "python", {"code": code}, guardrails=True)
    ok_code = "import os; os.path.join(a, b)"
    assert check_command(pol, "python", {"code": ok_code}, guardrails=True) is None


def test_deny_mode_denies_guardrail_hits():
    pol = ApprovalPolicy.from_mode("deny", approver=lambda c, r: True)
    out = check_command(pol, "shell", {"command": "TRUNCATE TABLE t"}, guardrails=True)
    assert out and out.startswith("DENIED")


# --------------------------------------------------------------- shell backup

def _mk_db(tmp_path: Path, name: str = "app.db", size: int = 128) -> Path:
    p = tmp_path / name
    p.write_bytes(b"SQLite format 3\x00" + b"\x00" * size)
    return p


def test_shell_backs_up_referenced_db_before_delete(tmp_path: Path):
    import os

    db = _mk_db(tmp_path)
    tool = ShellTool(timeout=20, root=str(tmp_path))
    del_cmd = "del" if os.name == "nt" else "rm"
    ok, out = tool.run({"command": f'{del_cmd} "{db.name}"'})
    assert ok
    assert "[guardrail] backed up" in out
    backups = list((tmp_path / ".saturday" / "backup").glob(f"*_{db.name}"))
    assert len(backups) == 1
    assert not db.exists(), "the delete itself still ran"


def test_shell_backup_wildcard_targets(tmp_path: Path):
    import os

    for n in ("a.db", "b.sqlite"):
        _mk_db(tmp_path, n)
    (tmp_path / "keep.txt").write_text("x")
    tool = ShellTool(timeout=20, root=str(tmp_path))
    del_cmd = "del" if os.name == "nt" else "rm"
    ok, out = tool.run({"command": f"{del_cmd} *.db *.sqlite keep.txt"})
    assert ok
    bdir = tmp_path / ".saturday" / "backup"
    backed = {p.name.rsplit("_", 1)[-1] for p in bdir.iterdir()}
    assert {"a.db", "b.sqlite"} <= backed


def test_shell_no_backup_for_benign_commands(tmp_path: Path):
    import os

    db = _mk_db(tmp_path)
    tool = ShellTool(timeout=20, root=str(tmp_path))
    cmd = f'type "{db.name}" > NUL' if os.name == "nt" else f'cat "{db.name}" > /dev/null'
    ok, out = tool.run({"command": cmd})
    assert ok
    assert "[guardrail]" not in out
    assert not (tmp_path / ".saturday" / "backup").exists()


def test_shell_backup_prunes_old_copies(tmp_path: Path):
    from saturday.tools.shell_guard import GUARDRAIL_BACKUP_KEEP

    db = _mk_db(tmp_path)
    tool = ShellTool(timeout=30, root=str(tmp_path))
    for i in range(GUARDRAIL_BACKUP_KEEP + 3):
        ok, _ = tool.run({"command": f"rem marker{i}"}) if False else (True, "")
        # direct calls to avoid spawning shells repeatedly
        from saturday.tools.shell_guard import backup_destructible_targets

        backup_destructible_targets(f"rm {db.name}", tmp_path)
        import time as _t

        _t.sleep(0.01)
    bdir = tmp_path / ".saturday" / "backup"
    assert len(list(bdir.iterdir())) <= GUARDRAIL_BACKUP_KEEP
    assert db.exists(), "backups never touch the original"
