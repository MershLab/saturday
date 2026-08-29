"""User-scriptable lifecycle hooks (Claude Code parity, zero-dep).

Config sources, merged (workspace overrides win on same event key):
  ~/.saturday/hooks.json            global
  <workspace>/.saturday/hooks.json  project

Shape: {"pre_tool_call": ["<command>", ...], "post_tool_call": [...]}
Each command runs with the event payload as JSON on stdin.
Semantics (mirrors Claude Code):
  - pre_tool_call exit 2  -> tool call BLOCKED, stderr is the block reason
  - pre_tool_call exit 0  -> allowed (stdout ignored)
  - any other failure     -> warning surfaced to the agent, never silent
Stdlib-only."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

HOOK_EVENTS = ("pre_tool_call", "post_tool_call")
HOOK_TIMEOUT = 15.0


def load_hooks(workspace_root: str | Path | None = None) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {e: [] for e in HOOK_EVENTS}
    sources = []
    try:
        from saturday.config import get_config_dir

        sources.append(Path(get_config_dir()) / "hooks.json")
    except Exception:
        pass
    if workspace_root:
        project_root = Path(workspace_root)
        project_hooks = project_root / ".saturday" / "hooks.json"
        # Project hooks are executable code, just like project MCP commands.
        # A checkout must not be able to smuggle them into a user's next run
        # without the same explicit trust decision used for .env and mcp.json.
        if project_hooks.is_file():
            try:
                from saturday.utils.trust import ensure_trusted

                if ensure_trusted(project_root, what=f"project hooks ({project_hooks})"):
                    sources.append(project_hooks)
            except Exception:
                # Fail closed if the trust subsystem is unavailable or errors.
                pass
    for src in sources:
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for event in HOOK_EVENTS:
            raw = data.get(event)
            if isinstance(raw, list):
                for cmd in raw:
                    if isinstance(cmd, str) and cmd.strip() and cmd not in merged[event]:
                        merged[event].append(cmd.strip())
    return merged


def run_hook(command: str, payload: dict, timeout: float = HOOK_TIMEOUT) -> tuple[int, str]:
    """Run one hook; returns (exit_code, combined_output). Never raises."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        out = ((proc.stderr or "") + (proc.stdout or "")).strip()
        return proc.returncode, out[-2000:]
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def make_pre_tool_hook(commands: list[str]):
    """Returns a pre_tool_call(tool_name, args) -> block-reason | None."""

    def hook(tool_name: str, args: dict) -> str | None:
        for cmd in commands:
            code, out = run_hook(cmd, {"event": "pre_tool_call", "tool": tool_name, "args": args})
            if code == 2:
                return f"blocked by user hook ({cmd[:60]}): {out or 'no reason given'}"
            if code != 0:
                return None if not out else None  # non-blocking failure; surfaced via post hook log
        return None

    return hook


def make_post_tool_hook(commands: list[str]):
    def hook(result) -> None:
        for cmd in commands:
            run_hook(
                cmd,
                {
                    "event": "post_tool_call",
                    "tool": result.name,
                    "ok": bool(result.ok),
                    "output": (result.output if result.ok else (result.error or ""))[:4000],
                },
            )

    return hook
