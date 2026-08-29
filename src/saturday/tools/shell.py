from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from saturday.tools.base import Tool


class ShellTool(Tool):
    name = "shell"
    description = (
        "Execute a shell command in the workspace and return stdout/stderr. "
        "Set run_in_background=true for long-running processes; poll with job_output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run"},
            "workdir": {"type": "string", "description": "Optional working directory relative to workspace root"},
            "run_in_background": {"type": "boolean", "description": "start without waiting; returns job_id"},
        },
        "required": ["command"],
    }

    def __init__(
        self,
        timeout: float = 60.0,
        root: str | None = None,
        job_manager=None,
        allow_network_fn=None,
    ) -> None:
        self.timeout = timeout
        self.root = root
        self.jobs = job_manager
        # Zero-arg callable returning the CURRENT shell_allow_network setting
        # (same dynamic-wiring pattern as repo_search's workspace_root_fn), so
        # Settings changes apply without rebuilding the agent. None = allowed.
        self.allow_network_fn = allow_network_fn

    def _network_allowed(self) -> bool:
        return True if self.allow_network_fn is None else bool(self.allow_network_fn())

    def _isolation_argv(self, command: str) -> tuple[list[str] | None, str | None]:
        """(argv, None) when a no-network wrapper exists on this platform, or
        (None, refusal) — fail-closed: a user who disabled shell networking
        must never get silent unisolated execution."""
        if os.name == "nt":
            return None, (
                "shell_allow_network=false: per-process network isolation is not "
                "enforceable on this platform, so the command was NOT run. "
                "Re-enable shell_allow_network in Settings to run shell commands."
            )
        unshare = shutil.which("unshare")
        if not unshare:
            return None, (
                "shell_allow_network=false: no 'unshare' binary available to isolate "
                "the network namespace, so the command was NOT run. Install util-linux "
                "(unshare) or re-enable shell_allow_network."
            )
        return [unshare, "--net", "sh", "-c", command], None

    def run(self, args: dict) -> tuple[bool, str]:
        command = args.get("command", "").strip()
        if not command:
            return False, "empty command"
        isolated_argv: list[str] | None = None
        if not self._network_allowed():
            isolated_argv, refusal = self._isolation_argv(command)
            if isolated_argv is None:
                return False, refusal
        workdir = args.get("workdir") or self.root or "."
        wd = Path(workdir).resolve()
        root = Path(self.root).resolve() if self.root else None
        if root is not None and wd != root and root not in wd.parents:
            return False, "workdir escapes workspace root"
        if not wd.exists():
            return False, f"workdir does not exist: {wd}"
        guardrail_notes: list[str] = []
        try:
            from saturday.tools.shell_guard import backup_destructible_targets

            guardrail_notes = backup_destructible_targets(command, wd)
        except Exception:
            guardrail_notes = []
        if args.get("run_in_background"):
            from saturday.tools.jobs import JobManager

            manager = self.jobs or JobManager()
            job_id = manager.start(command, workdir=str(wd), argv=isolated_argv)
            result = f"started background job '{job_id}' for: {command[:100]}"
            if guardrail_notes:
                result += "\n" + "\n".join(guardrail_notes)
            return True, result
        proc = None
        timed_out = False
        try:
            proc = subprocess.Popen(
                isolated_argv if isolated_argv is not None else command,
                shell=isolated_argv is None,
                cwd=str(wd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                # WHY: without a fresh session, os.killpg(os.getpgid(child))
                # below resolves to OUR process group and kills the harness;
                # start_new_session makes the child its own group leader so
                # the group kill targets only the child tree.
                start_new_session=(os.name != "nt"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return False, f"failed to spawn command: {exc}"
        if os.name == "nt":
            try:
                from saturday.tools.winjob import JobObject

                job = JobObject()
                job.assign(proc.pid)
                proc._saturday_job = job
            except Exception:
                pass
        try:
            out, err = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = "", ""
        finally:
            if os.name == "nt" and getattr(proc, "_saturday_job", None):
                try:
                    proc._saturday_job.close()
                except Exception:
                    pass
        exit_code = proc.returncode if proc.returncode is not None else -1
        suffix = f"\n[command timed out after {self.timeout}s and was terminated]" if timed_out else ""
        out = (out or "").strip()
        err = (err or "").strip()
        parts = [f"exit_code: {exit_code}{suffix}"]
        parts.extend(guardrail_notes)
        if out:
            if len(out) > 16_000:
                spill_path = self._spill(out, wd)
                parts.append(f"stdout (tail; full output spilled):\n{out[-8000:]}")
                parts.append(f"[output truncated; full output: {spill_path}]")
            else:
                parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err[-8000:]}")
        return True, "\n".join(parts)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Kill the process and every descendant so pipe handles are released."""
        import os

        if os.name == "nt":
            if getattr(proc, "_saturday_job", None):
                try:
                    proc._saturday_job.terminate()
                    return
                except Exception:
                    pass
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            import signal

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

    def _spill(self, text: str, wd: Path) -> str:
        import time as _t

        spill_dir = Path(wd) / ".saturday" / "spill"
        try:
            spill_dir.mkdir(parents=True, exist_ok=True)
            p = spill_dir / f"{_t.strftime('%Y%m%d-%H%M%S')}_{id(text) & 0xffff:x}.log"
            p.write_text(text, encoding="utf-8")
            return str(p)
        except OSError:
            return "(spill unavailable)"
