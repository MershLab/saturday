from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid


class Job:
    def __init__(self, job_id: str, command: str, proc: subprocess.Popen, win_job=None) -> None:
        self.id = job_id
        self.command = command
        self.proc = proc
        self.created = time.time()
        # WHY: Windows Job Object holding the child tree (None elsewhere);
        # lets kill() take out grandchildren that survive proc.kill()
        self.win_job = win_job
        self.output_buf: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, ""):
            with self._lock:
                self.output_buf.append(line)
                if len(self.output_buf) > 5000:
                    del self.output_buf[:2500]

    def status(self) -> str:
        code = self.proc.poll()
        return "running" if code is None else f"exited({code})"

    def tail(self, n: int = 80) -> str:
        with self._lock:
            return "".join(self.output_buf[-n:])

    def kill(self) -> bool:
        if self.proc.poll() is not None:
            return False
        if self.win_job is not None:
            try:
                self.win_job.terminate()
            except Exception:
                pass
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass
        if self.win_job is not None:
            try:
                self.win_job.close()
            except Exception:
                pass
        return True


class AgentJob:
    """JobManager-compatible handle for a background SUBAGENT (not a process):
    status/tail satisfy the job tools; kill is a graceful no-op."""

    def __init__(self, job_id: str, command: str, box: dict) -> None:
        self.id = job_id
        self.command = command
        self._box = box
        self.created = time.time()

    def status(self) -> str:
        return "running" if not self._box.get("done") else "done"

    def tail(self, n: int = 80) -> str:
        return "\n".join(self._box.get("lines", [])[-n:])

    def kill(self) -> bool:
        return False  # cooperative stop for subagents is future work


class JobManager:
    _shared: "JobManager | None" = None

    @classmethod
    def shared(cls) -> "JobManager":
        if cls._shared is None:
            cls._shared = JobManager()
        return cls._shared

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def register(self, job) -> None:
        """Register a non-process job (e.g. background subagent)."""
        with self._lock:
            self._jobs[job.id] = job

    def start(self, command: str, workdir: str | None = None, argv: list[str] | None = None) -> str:
        # WHY: CREATE_NO_WINDOW keeps console hosts from flashing windows;
        # assigning the child to a Job Object (best-effort) makes descendant
        # cleanup deterministic — `start notepad` grandchildren die with the
        # job instead of holding our stdout pipe forever.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(
            argv if argv is not None else command,
            shell=argv is None,  # pre-wrapped argv (no-network isolation) execs directly
            cwd=workdir or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        win_job = None
        if os.name == "nt":
            try:
                from saturday.tools.winjob import JobObject

                win_job = JobObject()
                win_job.assign(proc.pid)
            except Exception:
                if win_job is not None:
                    try:
                        win_job.close()
                    except Exception:
                        pass
                win_job = None  # cleanup stays best-effort; never block start
        job_id = uuid.uuid4().hex[:8]
        job = Job(job_id, command, proc, win_job=win_job)
        with self._lock:
            self._jobs[job_id] = job
        return job_id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> str:
        with self._lock:
            jobs = list(self._jobs.values())
        if not jobs:
            return "no background jobs"
        return "\n".join(f"{j.id}: {j.command[:80]} [{j.status()}]" for j in jobs)

    def reap(self) -> None:
        """Drop jobs FINISHED more than an hour ago.

        Works for both process Jobs and duck-typed AgentJobs (background
        subagents have no ``.proc``): a job qualifies only once its status
        reports it is no longer running — reaping used to crash job_list with
        an AttributeError, and the first duck-typed fix would have dropped
        long-running (>1h) subagents while still alive."""
        now = time.time()
        with self._lock:
            for jid in [
                jid
                for jid, j in self._jobs.items()
                if now - j.created > 3600
                and self._finished(j)
            ]:
                del self._jobs[jid]

    @staticmethod
    def _finished(job) -> bool:
        proc = getattr(job, "proc", None)
        if proc is not None:
            return proc.poll() is not None
        try:
            return str(job.status()) != "running"
        except Exception:
            return True


def make_job_tools(manager: JobManager) -> list:
    class JobListTool:
        name = "job_list"
        description = "List background jobs (started via shell run_in_background)."
        parameters = {"type": "object", "properties": {}, "required": []}
        manager_ref = manager

        def run(self, args: dict) -> tuple[bool, str]:
            manager.reap()
            return True, manager.list()

    class JobOutputTool:
        name = "job_output"
        description = "Read recent output from a background job."
        parameters = {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "lines": {"type": "integer"},
            },
            "required": ["job_id"],
        }
        manager_ref = manager

        def run(self, args: dict) -> tuple[bool, str]:
            job = manager.get(args.get("job_id", ""))
            if job is None:
                return False, f"unknown job '{args.get('job_id')}'"
            n = int(args.get("lines") or 80)
            return True, f"[{job.status()}]\n{job.tail(n)}"

    class JobKillTool:
        name = "job_kill"
        description = "Kill a background job."
        parameters = {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        }
        manager_ref = manager

        def run(self, args: dict) -> tuple[bool, str]:
            job = manager.get(args.get("job_id", ""))
            if job is None:
                return False, f"unknown job '{args.get('job_id')}'"
            return True, f"killed {job.id}" if job.kill() else f"{job.id} already finished"

    return [JobListTool(), JobOutputTool(), JobKillTool()]
