"""Preflight checks, computed once and rendered twice.

`saturday doctor` printed these as text and the web UI had no way to ask the
same questions, so a user who could not diagnose a broken setup from a
terminal could not diagnose it at all - which is exactly backwards, since
that is the user who needs the answer most.

run_checks() returns structured results; cli.cmd_doctor formats them as the
lines it always printed, and the web UI renders them as rows.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

OK, WARN, FAIL = "ok", "warn", "fail"

# label column width the CLI has always used: "python        : ..."
LABEL_W = 13


def _check(cid: str, label: str, status: str, detail: str, hint: str = "") -> dict[str, Any]:
    return {"id": cid, "label": label, "status": status, "detail": detail, "hint": hint}


def format_check(c: dict) -> str:
    """The exact line `saturday doctor` has always printed."""
    return f"{c['label']:<{LABEL_W}} : {c['detail']}"


def run_checks(cfg, offline: bool = False) -> list[dict[str, Any]]:
    """Every preflight check, in the order doctor has always reported them.

    Never raises: a check that cannot run reports itself as failed, because a
    diagnostic that dies is worse than one that says it could not look."""
    out: list[dict[str, Any]] = []

    ver = sys.version.split()[0]
    old = sys.version_info < (3, 10)
    out.append(_check("python", "python", FAIL if old else OK,
                      f"{ver} " + ("TOO OLD" if old else "ok"),
                      "Saturday needs Python 3.10 or newer" if old else ""))

    try:
        profile = cfg.profile()
    except ValueError as exc:
        out.append(_check("provider", "provider", FAIL, f"FAIL - {exc}",
                          "pick a supported provider in Settings"))
        return out  # nothing downstream is meaningful without a provider

    out.append(_check("provider", "provider", OK, f"{cfg.provider} ({profile.resolve_base_url()})"))
    out.append(_check("model", "model", OK, str(cfg.model)))

    key = profile.resolve_api_key()
    needs_key = profile.name not in ("ollama", "vllm")
    if needs_key and not key:
        out.append(_check("api_key", "api key", FAIL, f"MISSING ({profile.api_key_env})",
                          f"add {profile.api_key_env} in Settings or ~/.saturday/.env"))
    else:
        out.append(_check("api_key", "api key", OK,
                          "present" if key or not needs_key else "n/a (local provider)"))

    # --offline skips the probe entirely (CI/smoke: a provider that isn't
    # running must not fail the harness check)
    if offline:
        ok, detail = True, "skipped (--offline)"
    else:
        from saturday.llm.probe import probe_connection

        ok, detail, _models = probe_connection(profile, key, timeout=8)
    if ok:
        out.append(_check("endpoint", "endpoint", OK, detail))
    elif "auth rejected" in detail:
        out.append(_check("endpoint", "endpoint", FAIL, "reachable (auth rejected -> check key)",
                          "the endpoint answered but refused the key"))
    elif needs_key and not key:
        out.append(_check("endpoint", "endpoint", WARN,
                          "unverified (no key; expected for cloud providers)"))
    elif detail.startswith("endpoint answered with HTTP "):
        out.append(_check("endpoint", "endpoint", OK,
                          f"reachable ({detail.removeprefix('endpoint answered with ')})"))
    else:
        out.append(_check("endpoint", "endpoint", FAIL, f"UNREACHABLE - {detail}",
                          "check the base URL, your network, or whether the local server is running"))

    ws = Path(cfg.workspace_root)
    try:
        ws.mkdir(parents=True, exist_ok=True)
        probe_file = ws / ".saturday-write-test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        out.append(_check("workspace", "workspace", OK, f"writable ({ws})"))
    except OSError as exc:
        out.append(_check("workspace", "workspace", FAIL, f"NOT WRITABLE - {exc}",
                          "the agent cannot edit files here; pick another folder"))

    try:
        # Build it the way the agent does, rather than counting something
        # adjacent. default_registry() alone left out memory, skills, goals,
        # todo, delegation and the whole desktop suite - about half the real
        # total, and precisely the half a user wants confirmed - and counting
        # the plugins by hand still missed subagents. One construction path
        # means doctor and the tools list cannot report different numbers.
        from saturday.agent.core import Agent

        n = len(Agent(cfg=cfg)._build_registry().names())
        out.append(_check("tools", "tools", OK, f"{n} registered"))
    except Exception as exc:
        out.append(_check("tools", "tools", FAIL, f"FAILED to build registry - {exc}"))

    from saturday.config import get_config_dir
    from saturday.sessions import RunState

    try:
        runs = RunState.scan(get_config_dir() / "sessions")
    except Exception:
        runs = []
    orphaned = [r for r in runs if r["orphaned"]]
    live = [r for r in runs if r["alive"] and r["status"] == "running"]
    if orphaned:
        ids = ", ".join(r["id"] for r in orphaned[:5])
        more = f" (+{len(orphaned) - 5} more)" if len(orphaned) > 5 else ""
        out.append(_check("runs", "runs", WARN, f"{len(orphaned)} orphaned (crashed mid-run) - {ids}{more}",
                          "resume with: saturday chat --resume <session-id>"))
    elif live:
        out.append(_check("runs", "runs", OK, f"{len(live)} currently active, no orphaned runs"))
    else:
        out.append(_check("runs", "runs", OK, "none tracked as running"))

    try:
        from saturday import codemem

        cm = codemem.status()
        if cm["available"]:
            out.append(_check("codemem", "code memory", OK,
                              f"structural ({cm['version']})"))
        elif cm["supported"]:
            out.append(_check("codemem", "code memory", OK,
                              "lexical repo_search (structural index not installed)",
                              "saturday codemem install adds structural code retrieval"))
        else:
            out.append(_check("codemem", "code memory", OK,
                              f"lexical repo_search (no build for {cm['platform']})"))
    except Exception as exc:
        out.append(_check("codemem", "code memory", WARN, f"unknown - {exc}"))

    guardrails = bool(getattr(cfg, "destructive_guardrails", True))
    out.append(_check("guardrails", "guardrails", OK if guardrails else WARN,
                      "on - irreversible data ops ask + db files auto-backed-up" if guardrails
                      else "OFF (destructive_guardrails=false)"))

    mode = getattr(cfg, "persona_mode", "agent") or "agent"
    if mode == "assistant":
        out.append(_check("mode", "mode", OK, "personal assistant (curated toolset)"))

    # local config files must parse or every surface silently falls back to
    # defaults - surface that here instead of letting users discover it late
    home = get_config_dir()
    for name in ("hooks.json", "approvals.json", "config.json"):
        p = home / name
        if p.is_file():
            try:
                json.loads(p.read_text(encoding="utf-8-sig"))
                out.append(_check(name, name, OK, "ok"))
            except (json.JSONDecodeError, OSError) as exc:
                out.append(_check(name, name, FAIL, f"INVALID JSON - {exc}",
                                  f"fix or delete {p}"))
    return out


def failure_count(checks: list[dict]) -> int:
    return sum(1 for c in checks if c["status"] == FAIL)
