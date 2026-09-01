"""The line Saturday must not cross when delegating to another vendor's CLI.

Anthropic's April 2026 enforcement drew a sharp architectural boundary, and
several projects landed on the wrong side of it without meaning to:

  banned   taking an OAuth token issued to a Free/Pro/Max account and using
           it from another product - including via an agent SDK - so that a
           third-party tool speaks the vendor's API on a subscription's behalf.
  allowed  running the vendor's own CLI as a subprocess, which authenticates
           itself and enforces its own limits.

Saturday delegates by spawning the CLI. Nothing in these tests is a legal
opinion; they exist because the difference between the two is a handful of
lines of code, and it would be easy to drift across it while adding a feature
that felt like a convenience ("read the token so we can skip the subprocess").
A grep in a test is a cheap way to make that drift loud.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "saturday"

# Credential stores the vendors' own clients own. Saturday reads none of them.
BORROWED_CREDENTIALS = [
    r"\.claude[/\\]\.credentials",
    r"claude[._]credentials",
    r"CLAUDE_CODE_OAUTH_TOKEN",
    r"CLAUDE_SESSION_KEY",
    r"sessionKey",
    r"\.codex[/\\]auth",
    r"CURSOR_SESSION",
    r"refresh_token",
]


def _sources():
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8", errors="replace")


def test_saturday_never_reads_another_tools_credentials():
    """Delegation must not reach into a vendor's own credential store."""
    hits = []
    for path, text in _sources():
        for pattern in BORROWED_CREDENTIALS:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                line = text[: m.start()].count("\n") + 1
                hits.append(f"{path.relative_to(SRC)}:{line} matches {pattern!r}")
    assert not hits, (
        "Saturday appears to read a credential belonging to another vendor's "
        "client. Delegation must spawn that vendor's CLI and let it "
        "authenticate itself:\n  " + "\n  ".join(hits))


def test_delegation_spawns_the_vendors_own_binary():
    """Every shipped external agent runs a binary; none is a wrapped API call."""
    from saturday.tools.external_agent import AGENTS

    for name, spec in AGENTS.items():
        if spec.is_provider:
            continue  # provider entries run through Saturday's own configured key
        assert spec.binaries, f"{name} declares no binary to spawn"
        argv = spec.build_argv(spec.binaries[0], "a prompt")
        assert argv and argv[0] == spec.binaries[0], (
            f"{name} must invoke its own binary, got {argv!r}")
        joined = " ".join(argv).lower()
        for smell in ("http://", "https://", "api.", "bearer"):
            assert smell not in joined, (
                f"{name} builds what looks like a direct API call: {argv!r}")


def test_the_external_agent_tool_makes_a_subprocess_not_a_request():
    """The delegation path shells out; it does not open a socket itself."""
    text = (SRC / "tools" / "external_agent.py").read_text(encoding="utf-8")
    assert "subprocess.run" in text
    for forbidden in ("urllib.request", "http.client", "requests.", "httpx"):
        assert forbidden not in text, (
            f"external_agent.py should not speak HTTP directly; found {forbidden!r}")


def test_quota_limits_are_respected_rather_than_routed_around():
    """A real limit backs the agent off. Immediately retrying elsewhere on the
    same account, or ignoring it, is the behaviour the enforcement targeted."""
    from saturday import routing

    assert routing.QUOTA_BACKOFF_SECONDS >= 600, "backing off must actually mean waiting"
    for text in ("429", "rate limit", "quota", "usage limit", "too many requests"):
        assert routing.looks_like_quota_error(f"error: {text} reached"), text
    assert not routing.looks_like_quota_error("wrote 429 lines to the file")


@pytest.mark.parametrize("agent", ["claude-code", "codex", "cursor"])
def test_a_delegate_is_reached_only_through_its_installed_binary(agent):
    from saturday.tools.external_agent import AGENTS, find_binary

    spec = AGENTS[agent]
    # find_binary looks on PATH: an absent CLI is absent, never substituted
    assert find_binary(spec) is None or Path(find_binary(spec)).exists()


def test_an_agent_named_in_an_enforcement_carries_a_visible_caution():
    """opencode was named in Anthropic's April 2026 enforcement. Saturday only
    runs the binary, so this is not a block - what someone does with their own
    account is theirs. It is a warning shown where the choice is made, rather
    than something they discover from a suspended account."""
    from saturday.tools.external_agent import AGENTS

    assert AGENTS["opencode"].caution, "opencode must carry a caution"
    assert "subscription" in AGENTS["opencode"].caution.lower()

    js = (Path(__file__).parent.parent / "src" / "saturday" / "webui_assets" / "app.js").read_text()
    assert "a.caution" in js, "the caution must reach the agent list in the UI"
