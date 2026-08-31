"""Where the agent actually looked, as it works.

Retrieval already scores everything it considers. This module carries those
numbers out to whoever is watching instead of computing them a second time -
a view that recomputes its own scores is showing you a model of the agent
rather than the agent.

Three tiers, and the middle one is the point:

* **used** - entered the context. What the answer was built from.
* **considered** - scored, and lost. This is the tier worth looking at when
  the agent gets something wrong, and it is invisible everywhere else.
* **adjacent** - one hop from something used. Structural context only.

Emission is fire and forget: a sink that raises, or a step that nobody is
watching, must never disturb the run that produced it.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

USED, CONSIDERED, ADJACENT = "used", "considered", "adjacent"
MEMORY, CODE, SKILL, CHAT = "memory", "code", "skill", "chat"

_sinks: list[Callable[[dict], None]] = []
_lock = threading.Lock()
# Per-thread run context. It has to be per thread because several sessions run
# at once; it has to be COPYABLE because tool calls execute on a pool, and a
# context that stops at the loop thread means every tool reports step 0 and
# belongs to no session.
_ctx = threading.local()


def add_sink(fn: Callable[[dict], None]) -> Callable[[dict], None]:
    with _lock:
        _sinks.append(fn)
    return fn


def remove_sink(fn: Callable[[dict], None]) -> None:
    with _lock:
        if fn in _sinks:
            _sinks.remove(fn)


def set_step(step: int) -> None:
    """Tag subsequent events with the loop step they belong to."""
    _ctx.step = int(step)


def set_run(run_id: str) -> None:
    """Name the run these events belong to, so a watcher can filter to its own.

    Thread identity cannot do this job: tools execute on a worker pool, so the
    thread raising an event is not the thread that began the run."""
    _ctx.run = str(run_id or "")


def current_step() -> int:
    return int(getattr(_ctx, "step", 0) or 0)


def current_run() -> str:
    return str(getattr(_ctx, "run", "") or "")


def snapshot() -> tuple[str, int]:
    return current_run(), current_step()


def restore(ctx: tuple[str, int]) -> None:
    """Install a captured context on this thread. Used to carry it into the
    tool pool, which otherwise starts blank."""
    run, step = ctx
    _ctx.run = run
    _ctx.step = step


def emit(region: str, node: str, kind: str = USED, score: float = 0.0,
         label: str = "", **extra: Any) -> None:
    if not node:
        return
    event = {"region": region, "node": str(node), "kind": kind,
             "score": round(float(score or 0.0), 4), "label": label or str(node),
             "step": current_step(), "run": current_run(), **extra}
    with _lock:
        sinks = list(_sinks)
    for fn in sinks:
        try:
            fn(event)
        except Exception:
            pass  # watching must never break the work being watched


def emit_ranked(region: str, hits: list[dict], *, used: int, node_key: str = "id",
                score_key: str = "score", label_key: str = "text") -> None:
    """Publish a ranked retrieval in one call.

    ``used`` is how many of the hits actually entered the context; everything
    below that line is reported as considered rather than dropped silently,
    which is what makes a wrong answer diagnosable."""
    for i, hit in enumerate(hits or []):
        node = hit.get(node_key) or hit.get("slug") or hit.get("path") or ""
        if not node:
            continue
        emit(region, str(node), USED if i < used else CONSIDERED,
             float(hit.get(score_key) or 0.0), str(hit.get(label_key) or "")[:120])
