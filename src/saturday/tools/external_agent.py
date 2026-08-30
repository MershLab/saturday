"""Delegate a task to another installed CLI agent (Claude Code, Codex,
Cursor, Antigravity) instead of Saturday's own subagent system - useful when
a task genuinely calls for a different model/tool ecosystem, not as a
replacement for `task` (Saturday's own subagents stay the default delegate).

Invocation flags are verified against each tool's own published docs at time
of writing, but external CLIs change their surface between versions (Google
retired the entire Gemini CLI mid-2026, which is why `agy` is here instead).
A wrong flag surfaces as a real, catchable failure - bad flag, non-zero exit,
stderr passed through - rather than silently misbehaving, which is why this
registry deliberately has no stale-detection heuristic."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable

from saturday.tools.base import Tool


@dataclass(frozen=True)
class ExternalAgentSpec:
    id: str
    binaries: tuple[str, ...]  # tried in order; first found wins
    install_hint: str
    build_argv: Callable[[str, str], list[str]]


def _claude_code_argv(binary: str, prompt: str) -> list[str]:
    return [binary, "-p", prompt]


def _codex_argv(binary: str, prompt: str) -> list[str]:
    return [binary, "exec", prompt]


def _cursor_argv(binary: str, prompt: str) -> list[str]:
    return [binary, "-p", prompt]


def _antigravity_argv(binary: str, prompt: str) -> list[str]:
    return [binary, "-p", prompt]


AGENTS: dict[str, ExternalAgentSpec] = {
    "claude-code": ExternalAgentSpec(
        id="claude-code",
        binaries=("claude",),
        install_hint="npm install -g @anthropic-ai/claude-code",
        build_argv=_claude_code_argv,
    ),
    "codex": ExternalAgentSpec(
        id="codex",
        binaries=("codex",),
        install_hint="npm install -g @openai/codex",
        build_argv=_codex_argv,
    ),
    "cursor": ExternalAgentSpec(
        id="cursor",
        binaries=("cursor-agent",),
        install_hint="curl https://cursor.com/install -fsS | bash",
        build_argv=_cursor_argv,
    ),
    # Gemini CLI was retired 2026-06-18; agy is its replacement
    "antigravity": ExternalAgentSpec(
        id="antigravity",
        binaries=("agy",),
        install_hint="curl -fsSL https://antigravity.google/cli/install.sh | bash",
        build_argv=_antigravity_argv,
    ),
}

AGENTS["gemini"] = AGENTS["antigravity"]


def _templated_argv(arg_template: list[str]):
    def build(binary: str, prompt: str) -> list[str]:
        return [binary] + [a.replace("{prompt}", prompt) for a in arg_template]
    return build


def load_custom_agents() -> dict[str, ExternalAgentSpec]:
    """User-defined agents from CONFIG_DIR/agents.json; a built-in name can be overridden."""
    from saturday.config import get_config_dir

    path = get_config_dir() / "agents.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ExternalAgentSpec] = {}
    for name, cfg in raw.items():
        if not isinstance(cfg, dict):
            continue
        binaries = cfg.get("binaries") or [name]
        if isinstance(binaries, str):
            binaries = [binaries]
        args = cfg.get("args") or ["-p", "{prompt}"]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            continue
        out[str(name)] = ExternalAgentSpec(
            id=str(name),
            binaries=tuple(str(b) for b in binaries),
            install_hint=str(cfg.get("install_hint") or ""),
            build_argv=_templated_argv(args),
        )
    return out


def all_agents() -> dict[str, ExternalAgentSpec]:
    return {**AGENTS, **load_custom_agents()}


def find_binary(spec: ExternalAgentSpec) -> str | None:
    for name in spec.binaries:
        found = shutil.which(name)
        if found:
            return found
    return None


class ExternalAgentTool(Tool):
    name = "external_agent"
    description = (
        "Delegate a task to a different installed CLI agent (claude-code, codex, cursor, antigravity) "
        "instead of Saturday's own subagents - for when a task specifically calls for a different "
        "model/tool ecosystem. Installs the CLI automatically if it's missing and install=true."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent": {"type": "string", "enum": list(AGENTS.keys())},  # replaced per-instance
            "prompt": {"type": "string", "description": "Full standalone instructions for the delegate"},
            "install": {"type": "boolean", "description": "auto-install the CLI if missing (default false - asks first otherwise)"},
            "timeout": {"type": "number", "description": "seconds before giving up (default 600)"},
        },
        "required": ["agent", "prompt"],
    }

    def __init__(self, installer=None) -> None:
        # injection point for tests; real default shells out for real
        self._installer = installer or self._default_install
        self._agents = all_agents()
        names = list(self._agents)
        self.parameters = {**type(self).parameters}
        self.parameters["properties"] = {
            **type(self).parameters["properties"],
            "agent": {"type": "string", "enum": names},
        }
        self.description = (
            f"Delegate a task to a different installed CLI agent ({', '.join(names)}) "
            "instead of Saturday's own subagents - for when a task specifically calls for a "
            "different model/tool ecosystem. Installs the CLI automatically if install=true. "
            "Add your own in CONFIG_DIR/agents.json."
        )

    @staticmethod
    def _default_install(spec: ExternalAgentSpec) -> tuple[bool, str]:
        try:
            r = subprocess.run(spec.install_hint, shell=True, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"install command failed to run: {exc}"
        if r.returncode != 0:
            return False, f"install failed (exit {r.returncode}): {(r.stderr or r.stdout)[-500:]}"
        return True, "installed"

    def run(self, args: dict) -> tuple[bool, str]:
        agent_id = args.get("agent")
        spec = self._agents.get(agent_id or "")
        if spec is None:
            return False, f"unknown agent {agent_id!r}; choose one of {list(self._agents)}"
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return False, "prompt is required"
        timeout = float(args.get("timeout") or 600.0)

        binary = find_binary(spec)
        if binary is None:
            if not args.get("install"):
                how = f" Install it with: {spec.install_hint}  (or pass install=true)" if spec.install_hint else ""
                return False, f"{agent_id} is not installed ({'/'.join(spec.binaries)} not on PATH).{how}"
            if not spec.install_hint:
                return False, f"{agent_id} has no install command configured; install it manually"
            ok, detail = self._installer(spec)
            if not ok:
                return False, f"auto-install failed: {detail}"
            binary = find_binary(spec)
            if binary is None:
                return False, f"install reported success but {spec.binaries[0]} still isn't on PATH"

        argv = spec.build_argv(binary, prompt)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"{agent_id} timed out after {timeout}s"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{agent_id} failed to run: {exc}"
        if r.returncode != 0:
            return False, f"{agent_id} exited {r.returncode}: {(r.stderr or r.stdout)[-1000:]}"
        return True, r.stdout.strip() or "(no output)"
