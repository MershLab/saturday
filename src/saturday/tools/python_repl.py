from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import uuid

from saturday.tools.base import Tool


class PythonREPL(Tool):
    name = "python"
    description = (
        "Execute Python code in a persistent interpreter session (variables persist between calls). "
        "Print output is captured. Use for computation, data analysis, and verification."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source code to execute"},
        },
        "required": ["code"],
    }

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _ensure_proc(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        bootstrap = textwrap.dedent(
            """
            import sys, json, traceback
            g = {"__name__": "__saturday_repl__"}
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line)
                except Exception as exc:
                    print(json.dumps({"ok": False, "error": f"bad envelope: {exc}", "sync": False}), flush=True)
                    continue
                code = payload.get("code", "")
                marker = payload.get("marker", "__DF_DONE__")
                err = None
                try:
                    exec(compile(code, "<repl>", "exec"), g)
                except BaseException:
                    tb = traceback.format_exc(limit=6)
                    err = tb[-6000:]
                print(marker, flush=True)
                if err is None:
                    print(json.dumps({"ok": True}), flush=True)
                else:
                    print(json.dumps({"ok": False, "error": err}), flush=True)
            """
        )
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-c", bootstrap],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            # WHY: a child that prints binary would otherwise raise mid-read
            # and desync the marker protocol; replace keeps the stream alive
            errors="replace",
        )
        return self._proc

    def run(self, args: dict) -> tuple[bool, str]:
        with self._lock:
            return self._run_locked(args)

    def _run_locked(self, args: dict) -> tuple[bool, str]:
        code = args.get("code", "")
        if not code.strip():
            return False, "empty code"
        marker = f"__done_{uuid.uuid4().hex[:8]}__"
        proc = self._ensure_proc()
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(json.dumps({"code": code, "marker": marker}) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            proc.kill()
            return False, "interpreter died; restart session"

        import threading as _threading

        timed_out = {"flag": False}

        def watchdog():
            timed_out["flag"] = True
            self.close()

        timer = _threading.Timer(max(1.0, self.timeout), watchdog)
        timer.daemon = True
        timer.start()
        try:
            lines: list[str] = []
            while True:
                line = proc.stdout.readline()
                if line == "":
                    if timed_out["flag"]:
                        return False, f"execution timed out after {self.timeout}s; interpreter restarted"
                    return False, "interpreter terminated unexpectedly:\n" + "\n".join(lines[-40:])
                stripped = line.rstrip("\r\n")
                if stripped == marker:
                    break
                lines.append(stripped)
            status_line = proc.stdout.readline()
        finally:
            timer.cancel()

        if timed_out["flag"]:
            return False, f"execution timed out after {self.timeout}s"
        try:
            status = json.loads(status_line)
        except json.JSONDecodeError:
            return False, "lost interpreter sync:\n" + "\n".join(lines[-40:])
        output = "\n".join(lines)
        if len(output) > 16_000:
            output = output[:16_000] + "\n... [truncated]"
        if status.get("ok"):
            return True, output or "(no output)"
        return False, status.get("error") or output or "unknown error"

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            self._proc.terminate()
