"""Subagent tool: one-shot, CONTINUABLE, and BACKGROUND children.

Continuable (dsh parity): each child keeps its own conversation; pass the
returned ``id`` back as ``continue_id`` with a follow-up prompt instead of
re-explaining context.
Background: ``background=true`` runs the child on a daemon thread registered
with the shared JobManager, so job_list/job_output track it like a shell job.

The factory callable builds a fresh configured agent per child; the tool owns
child lifecycle/history. Stdlib-only."""
from __future__ import annotations

import threading
import time
from typing import Callable


class _Child:
    def __init__(self, cid: str, agent_factory: Callable[[], object] | None) -> None:
        self.id = cid
        self.history: list[dict] = []
        self.turns = 0
        self.created = time.time()
        # one agent INSTANCE per child for the child's whole life
        self._agent = None
        self._factory = agent_factory

    @property
    def agent(self):
        if self._agent is None and self._factory is not None:
            self._agent = self._factory()
        return self._agent


class SubagentTask:
    name = "task"
    description = (
        "Delegate a subtask to a sub-agent with its own context window and tool access. "
        "Provide a complete standalone prompt. Returns a report plus an id: pass that id "
        "as continue_id later to keep the same sub-agent in context (it remembers). "
        "background=true runs it async - you get a job_id for job_list/job_output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3-5 word summary of the subtask"},
            "prompt": {"type": "string", "description": "Full standalone instructions"},
            "continue_id": {"type": "string", "description": "id from a previous call to continue that child"},
            "background": {"type": "boolean", "description": "run asynchronously; poll via job_output"},
        },
        "required": ["description", "prompt"],
    }

    def __init__(
        self,
        runner: Callable[[str], str] | None = None,
        max_depth: int = 2,
        *,
        agent_factory: Callable[[], object] | None = None,
    ) -> None:
        # legacy contract: runner(prompt) -> report
        self._runner = runner
        self._factory = agent_factory
        self.max_depth = max_depth
        self._children: dict[str, _Child] = {}
        self._lock = threading.Lock()
        self._seq = 0
        # Optional live-progress hook set by a surface (the web app forwards
        # child activity to the transcript as subagent rows). Signature:
        # (child_id, kind, kwargs_dict) -> None; must never raise.
        self._event_fn: Callable[[str, str, dict], None] | None = None

    # -- internals ---------------------------------------------------------------
    def _emit(self, cid: str, kind: str, **kw) -> None:
        fn = self._event_fn
        if fn is None:
            return
        try:
            fn(cid, kind, kw)
        except Exception:
            pass

    def _new_child(self) -> _Child:
        with self._lock:
            self._seq += 1
            child = _Child(f"sub-{self._seq}", self._factory)
            self._children[child.id] = child
        return child

    def _run_child(self, child: _Child, prompt: str) -> tuple[str, list[dict]]:
        """Returns (report, new_history_slice)."""
        if self._runner is not None:
            return self._runner(prompt), []
        import inspect

        agent = child.agent
        cid = child.id
        self._emit(cid, "start", description=prompt[:120])
        # forward progress only when the child's run() actually supports the
        # callback kwargs (third-party/fake agents may have narrower signatures)
        kwargs: dict = {}
        try:
            params = inspect.signature(agent.run).parameters
            if "on_step_start" in params:
                kwargs["on_step_start"] = lambda n, _c=cid: self._emit(_c, "step", n=n)
            if "on_tool_result" in params:
                kwargs["on_tool_result"] = lambda r, _c=cid: self._emit(
                    _c,
                    "tool",
                    name=getattr(r, "name", "?"),
                    ok=bool(getattr(r, "ok", False)),
                    output=str(getattr(r, "output", "") or "")[:400],
                    error=str(getattr(r, "error", "") or "")[:400],
                )
        except (TypeError, ValueError):
            pass
        traj = agent.run(prompt, initial_history=list(child.history) or None, **kwargs)
        # keep BOTH sides of each exchange so continuations replay faithfully
        new_msgs = [{"role": "user", "content": prompt}, *traj.messages()[2:]]
        child.history.extend(new_msgs)
        child.turns += 1
        report = traj.final_answer or f"[subagent stopped: {traj.stop_reason}]"
        self._emit(cid, "done", summary=str(report)[:300])
        return f"{report}\n[id={child.id} continue_id={child.id} turns={child.turns}]", new_msgs

    def run(self, args: dict) -> tuple[bool, str]:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return False, "empty prompt"
        try:
            continue_id = str(args.get("continue_id") or "").strip()
            background = bool(args.get("background"))
            notice = ""
            if continue_id:
                child = self._children.get(continue_id)
                if child is None:
                    child = self._new_child()
                    notice = (
                        f"[note: unknown continue_id '{continue_id}'; "
                        f"started a fresh child {child.id}]\n"
                    )
            else:
                child = self._new_child()

            if self._running_background_job(child) is not None:
                # two threads mutating child.history concurrently corrupts the
                # replay transcript; direct the caller to poll instead
                return False, (
                    f"sub-agent ag-{child.id} is still running in the background; "
                    "poll job_output for it instead of continuing it now"
                )

            if background:
                ok, msg = self._start_background(child, prompt)
                return ok, (notice + msg if notice else msg)

            report, _ = self._run_child(child, prompt)
            output = (notice + report)[-20_000:]
            return True, output
        except Exception as exc:
            return False, f"subagent failed: {type(exc).__name__}: {exc}"

    @staticmethod
    def _running_background_job(child: "_Child"):
        """Best-effort duck-typed manager lookup; both Job and AgentJob expose
        status(). None means no live background run for this child."""
        try:
            from saturday.tools.jobs import JobManager

            job = JobManager.shared().get(f"ag-{child.id}")
        except Exception:
            return None
        if job is None:
            return None
        try:
            return job if str(job.status()) == "running" else None
        except Exception:
            return None

    # -- background --------------------------------------------------------------
    def _start_background(self, child: _Child, prompt: str) -> tuple[bool, str]:
        from saturday.tools.jobs import AgentJob, JobManager

        mgr = JobManager.shared()
        job_id = f"ag-{child.id}"
        if mgr.get(job_id) is not None and mgr.get(job_id).status() == "running":
            return False, f"background sub-agent {job_id} already running"

        box = {"lines": [f"delegating to {child.id}: {prompt[:80]}"], "done": False}

        def worker() -> None:
            try:
                report, _ = self._run_child(child, prompt)
                box["lines"].append(report)
            except Exception as exc:
                box["lines"].append(f"subagent failed: {type(exc).__name__}: {exc}")
            finally:
                box["done"] = True

        # register BEFORE starting the thread: a fast-failing child must
        # already be observable via job_list/job_output
        mgr.register(AgentJob(job_id, f"task {child.id}", box))
        threading.Thread(target=worker, daemon=True).start()
        return True, f"started background sub-agent job_id={job_id} (poll job_output)"

    def list_children(self) -> list[str]:
        with self._lock:
            return [f"{c.id} turns={c.turns}" for c in self._children.values()]


def build_task_tool(agent_factory: Callable[[], Callable[[str], str]]) -> SubagentTask:
    return SubagentTask(runner=agent_factory())
