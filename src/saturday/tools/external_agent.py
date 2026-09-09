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
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from saturday.tools.base import Tool


@dataclass(frozen=True)
class ExternalAgentSpec:
    id: str
    binaries: tuple[str, ...]  # tried in order; first found wins
    install_hint: str
    # (binary, prompt, model) -> argv. model is "" for "whatever the CLI is
    # already configured to use", which stays the default.
    build_argv: Callable[[str, str, str], list[str]]
    # How to ask this CLI what models it can reach, so nothing here is a
    # hardcoded list that goes stale when the vendor changes their lineup.
    # models_argv: a subcommand that prints one model per line.
    # models_from_help: the binary names its models in --help instead.
    # Neither set means the CLI cannot be asked, and the model is free text.
    models_argv: tuple[str, ...] = ()
    models_from_help: bool = False
    # provider-backed agents run through Saturday itself instead of a binary
    provider: str = ""
    model: str = ""
    tier: int | None = None
    # a caution to repeat wherever this agent is offered - not a block. What a
    # user may do with their own account is between them and their vendor; the
    # least Saturday can do is not let them find out afterwards.
    caution: str = ""
    # user-registered (agents.json), not one of the ones built into this file -
    # the CLI and Settings use this to decide what can be removed
    custom: bool = False

    @property
    def is_provider(self) -> bool:
        return bool(self.provider)


def _claude_code_argv(binary: str, prompt: str, model: str = "") -> list[str]:
    # Verified live (2026-09-07): `claude -p "edit a file"` alone answers
    # "the edit is ready but blocked - write permission hasn't been granted",
    # leaves the file untouched, and exits 0. A caller reading the return code
    # records that as success, so a delegated edit was a silent no-op - the
    # same defect the codex entry below fixed, never applied here (T10).
    #
    # acceptEdits, not bypassPermissions: it is the least privilege that lets
    # the delegated task actually edit, and anything else that would prompt is
    # still refused. Same reasoning as codex's workspace-write.
    argv = [binary, "-p", "--permission-mode", "acceptEdits"]
    # Verified live: `claude --model <model>` takes an alias ("opus", "sonnet")
    # or a full name. The prompt stays last so it is never read as the flag's
    # value.
    if model:
        argv += ["--model", model]
    return argv + [prompt]


def _codex_argv(binary: str, prompt: str, model: str = "") -> list[str]:
    # Verified live (codex-cli 0.149.1): `exec` alone already runs with
    # approval:never, so it never blocks on a missing tty - but its default
    # sandbox is read-only, and a task that needs to write just apologizes
    # and exits 0. That is a silent no-op a caller reading only the return
    # code would record as success. workspace-write is the minimum privilege
    # that lets a delegated task actually do anything (confined to the
    # workdir, /tmp and $TMPDIR - not danger-full-access).
    # --skip-git-repo-check: codex refuses to run at all in a directory it
    # has not been separately trusted in interactively, and Saturday's
    # workspace_root is not guaranteed to be one.
    argv = [binary, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write"]
    if model:  # verified live: codex exec -m/--model <MODEL>
        argv += ["--model", model]
    return argv + [prompt]


def _cursor_argv(binary: str, prompt: str, model: str = "") -> list[str]:
    # NOT changed, deliberately: cursor is not installed on the machine this
    # was investigated on, so the claude fix above could not be checked
    # against it. T10 names cursor alongside claude, and the shape is likely
    # the same, but this file's standard for these argv specs is "verified
    # live" and guessing a permission flag into a delegation path is exactly
    # the kind of untested default that produced the bug. (T10, part open)
    argv = [binary, "-p"]
    if model:
        argv += ["--model", model]
    return argv + [prompt]


def _antigravity_argv(binary: str, prompt: str, model: str = "") -> list[str]:
    argv = [binary, "-p"]
    if model:
        argv += ["--model", model]
    return argv + [prompt]


def _opencode_argv(binary: str, prompt: str, model: str = "") -> list[str]:
    # --auto: without it, `run` can sit on a permission prompt with no tty to
    # answer it - the same failure shape codex's sandbox default has, per
    # opencode's own --help ("auto-approve permissions that are not
    # explicitly denied").
    argv = [binary, "run", "--auto"]
    # verified live: opencode run -m/--model, "in the format of provider/model"
    if model:
        argv += ["--model", model]
    return argv + [prompt]


AGENTS: dict[str, ExternalAgentSpec] = {
    "claude-code": ExternalAgentSpec(
        id="claude-code",
        binaries=("claude",),
        install_hint="npm install -g @anthropic-ai/claude-code",
        build_argv=_claude_code_argv,
        # no `claude models` subcommand exists; the binary names its aliases in
        # --help, so the list tracks whatever version is installed.
        models_from_help=True,
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
    "opencode": ExternalAgentSpec(
        id="opencode",
        binaries=("opencode",),
        install_hint="curl -fsSL https://opencode.ai/install | bash",
        build_argv=_opencode_argv,
        # verified live: prints one "provider/model" per line, and only for
        # providers this install is actually authenticated to.
        models_argv=("models",),
        # Named in Anthropic's April 2026 enforcement against third-party
        # harnesses billing to a consumer subscription. Saturday only runs the
        # binary, but if it is configured to bill a Claude subscription it is
        # the USER'S account at risk, so say so where they choose it.
        caution="check how you have this configured: billing a Claude "
                "subscription through a third-party harness is restricted",
    ),
}

AGENTS["gemini"] = AGENTS["antigravity"]


def _templated_argv(arg_template: list[str]):
    def build(binary: str, prompt: str, model: str = "") -> list[str]:
        out = [binary]
        for a in arg_template:
            if "{model}" in a and not model:
                continue  # no model chosen: drop the flag rather than pass ""
            out.append(a.replace("{prompt}", prompt).replace("{model}", model))
        return out
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
        if cfg.get("provider"):
            out[str(name)] = ExternalAgentSpec(
                id=str(name), binaries=(), install_hint="",
                build_argv=_templated_argv([]),
                provider=str(cfg["provider"]), model=str(cfg.get("model") or ""),
                tier=int(cfg["tier"]) if cfg.get("tier") is not None else None,
                custom=True,
            )
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
            custom=True,
            build_argv=_templated_argv(args),
        )
    return out


def _read_agents_json() -> dict:
    from saturday.config import get_config_dir

    path = get_config_dir() / "agents.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_custom_agent(
    name: str, binaries: list[str], args: list[str] | None = None, install_hint: str = ""
) -> None:
    """Register a CLI Saturday doesn't already know about, without hand-editing
    agents.json - this is the same file `load_custom_agents()` reads, so it
    shows up everywhere a built-in agent does (routing, `saturday agents`,
    Settings) the moment it's saved."""
    from saturday.config import get_config_dir

    name = name.strip()
    if not name:
        raise ValueError("agent name is required")
    binaries = [b.strip() for b in binaries if b.strip()]
    if not binaries:
        raise ValueError("at least one binary name is required")
    raw = _read_agents_json()
    raw[name] = {
        "binaries": binaries,
        "args": args or ["-p", "{prompt}"],
        "install_hint": install_hint,
    }
    path = get_config_dir() / "agents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")


def remove_custom_agent(name: str) -> bool:
    """True if an agent named `name` was actually removed."""
    from saturday.config import get_config_dir

    raw = _read_agents_json()
    if name not in raw:
        return False
    del raw[name]
    path = get_config_dir() / "agents.json"
    path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    return True


def all_agents() -> dict[str, ExternalAgentSpec]:
    return {**AGENTS, **load_custom_agents()}


def find_binary(spec: ExternalAgentSpec) -> str | None:
    for name in spec.binaries:
        found = shutil.which(name)
        if found:
            return found
    return None


# Model discovery is cached per binary path and mtime: a CLI that was upgraded
# reports a different lineup, and the mtime changing is exactly that signal.
_MODELS_CACHE: dict[tuple[str, str, float], tuple[float, list[str]]] = {}
_MODELS_TTL = 900.0


def _run_for_lines(argv: list[str], timeout: float) -> list[str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def _models_from_help_text(text: str) -> list[str]:
    """Model names a CLI quotes in its own --help.

    `claude --help` documents --model as taking "an alias for the latest model
    (e.g. 'fable', 'opus', or 'sonnet') or a model's full name (e.g.
    'claude-fable-5')". Reading them from the installed binary means the list
    follows the CLI's own updates instead of being pinned here."""
    import re

    seg = ""
    for line in text.splitlines():
        if "--model" in line:
            seg = line
            continue
        if seg:
            # the description wraps; keep taking indented continuation lines
            if line.startswith((" " * 6, "\t")) and not line.strip().startswith("-"):
                seg += " " + line
            else:
                break
    if not seg:
        return []
    out, seen = [], set()
    for m in re.findall(r"'([A-Za-z0-9][A-Za-z0-9._-]{1,60})'", seg):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def discover_models(spec: ExternalAgentSpec, timeout: float = 20.0,
                    refresh: bool = False) -> list[str]:
    """Ask an installed CLI which models it can reach. [] when it cannot say.

    Never a hardcoded lineup: every name here comes from the binary on this
    machine, so upgrading the CLI or authenticating a new provider changes the
    answer without touching Saturday."""
    binary = find_binary(spec)
    if binary is None:
        return []
    try:
        stamp = os.path.getmtime(binary)
    except OSError:
        stamp = 0.0
    key = (spec.id, binary, stamp)
    hit = _MODELS_CACHE.get(key)
    if hit and not refresh and (time.time() - hit[0]) < _MODELS_TTL:
        return list(hit[1])

    models: list[str] = []
    if spec.models_argv:
        models = _run_for_lines([binary, *spec.models_argv], timeout)
    elif spec.models_from_help:
        try:
            r = subprocess.run([binary, "--help"], capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
            models = _models_from_help_text(r.stdout or "")
        except (OSError, subprocess.SubprocessError):
            models = []
    _MODELS_CACHE[key] = (time.time(), list(models))
    return list(models)


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
            "timeout": {
                "type": "number",
                "description": "seconds before giving up (default 600). A delegate spins up a "
                               "whole other agent that plans and acts on its own; a multi-step "
                               "task (browser automation, real file edits) routinely needs minutes, "
                               "not seconds. Passing a low number here almost never speeds up a "
                               "genuine task - it just turns a delegate that was working into a "
                               "false timeout, indistinguishable from a real failure to the caller. "
                               "Leave this unset unless there is a specific reason to cut it short.",
            },
            "task_kind": {"type": "string", "description": "for agent=auto: task category, so routing learns per kind"},
        },
        "required": ["agent", "prompt"],
    }

    def __init__(self, installer=None, provider_runner=None, workspace_root_fn=None,
                 agent_models_fn=None) -> None:
        # () -> {agent_id: model}. Read per call, not captured, so a model
        # changed in Settings applies to the next delegation without a rebuild.
        self._agent_models_fn = agent_models_fn
        # injection point for tests; real default shells out for real
        self._installer = installer or self._default_install
        # (provider, model, prompt) -> (ok, text); None disables provider-backed agents
        self._provider_runner = provider_runner
        # binary delegates otherwise inherit Saturday's own process cwd,
        # which has nothing to do with the task's actual workspace - None
        # means "wherever Saturday's process happens to be", same as before
        # this was wired, for any caller that has no workspace to offer
        self._workspace_root_fn = workspace_root_fn
        self._agents = all_agents()
        names = ["auto"] + list(self._agents)
        self.parameters = {**type(self).parameters}
        self.parameters["properties"] = {
            **type(self).parameters["properties"],
            "agent": {"type": "string", "enum": names},
            "model": {
                "type": "string",
                "description": "Model for this one delegation, overriding the "
                               "agent's configured default. Omit to use it.",
            },
        }
        self.description = (
            f"Delegate a task to a different installed CLI agent ({', '.join(names)}) "
            "instead of Saturday's own subagents - for when a task specifically calls for a "
            "different model/tool ecosystem. agent='auto' picks the cheapest enabled one that "
            "can do it and escalates on failure. Installs the CLI automatically if install=true. "
            "Add your own in CONFIG_DIR/agents.json."
        )

    @staticmethod
    def _default_install(spec: ExternalAgentSpec) -> tuple[bool, str]:
        # Three of the built-in hints are `curl ... | bash`: fetch a script over
        # the network and run it unread. That is the exact shape check_command
        # stops for `shell`, and reaching it through install=true walked around
        # the question. Approving a delegation is not approving an install, so
        # this one is handed back for the user to run deliberately.
        from saturday.safety import looks_like_remote_pipe_to_shell

        if looks_like_remote_pipe_to_shell(spec.install_hint):
            return False, (
                f"{spec.id} is not installed, and its installer pipes a downloaded "
                f"script straight into a shell. Saturday will not run that for you.\n"
                f"Run it yourself if you trust it:\n    {spec.install_hint}"
            )
        try:
            r = subprocess.run(spec.install_hint, shell=True, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"install command failed to run: {exc}"
        if r.returncode != 0:
            return False, f"install failed (exit {r.returncode}): {(r.stderr or r.stdout)[-500:]}"
        return True, "installed"

    def run(self, args: dict) -> tuple[bool, str]:
        if args.get("agent") == "auto":
            return self._run_auto(args)
        return self._run_one(args.get("agent"), args)

    def _run_auto(self, args: dict) -> tuple[bool, str]:
        """Cheapest enabled agent first, escalating one tier per failure."""
        from saturday import routing

        task_kind = str(args.get("task_kind") or "general")
        tried: set[str] = set()
        errors: list[str] = []
        for _ in range(3):
            agent = routing.pick(task_kind, exclude=tried)
            if agent is None:
                break
            tried.add(agent)
            started = time.time()
            ok, msg = self._run_one(agent, args)
            routing.record(agent, task_kind, ok, time.time() - started, note="" if ok else msg)
            if ok:
                return True, msg
            if routing.looks_like_quota_error(msg):
                routing.mark_quota_exhausted(agent)
            errors.append(f"{agent}: {msg[:200]}")
        if not tried:
            return False, (
                "no agent available for auto-delegation. Enable one with "
                "`saturday agents --enable <name>` (see `saturday agents`)."
            )
        return False, "all candidates failed:\n" + "\n".join(errors)

    def _run_one(self, agent_id: str | None, args: dict) -> tuple[bool, str]:
        spec = self._agents.get(agent_id or "")
        if spec is not None and spec.is_provider:
            if self._provider_runner is None:
                return False, f"{agent_id} is provider-backed but no runner is wired"
            prompt = (args.get("prompt") or "").strip()
            if not prompt:
                return False, "prompt is required"
            try:
                return self._provider_runner(spec.provider, spec.model, prompt)
            except Exception as exc:
                return False, f"{agent_id} failed: {type(exc).__name__}: {exc}"
        if spec is None:
            return False, f"unknown agent {agent_id!r}; choose one of {['auto'] + list(self._agents)}"
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

        model = str(args.get("model") or "").strip()
        if not model and self._agent_models_fn is not None:
            try:
                model = str((self._agent_models_fn() or {}).get(agent_id) or "").strip()
            except Exception:
                model = ""
        argv = spec.build_argv(binary, prompt, model)
        cwd = None
        if self._workspace_root_fn is not None:
            try:
                cwd = self._workspace_root_fn() or None
            except Exception:
                cwd = None
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            return False, f"{agent_id} timed out after {timeout}s"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{agent_id} failed to run: {exc}"
        if r.returncode != 0:
            return False, f"{agent_id} exited {r.returncode}: {(r.stderr or r.stdout)[-1000:]}"
        return True, r.stdout.strip() or "(no output)"
