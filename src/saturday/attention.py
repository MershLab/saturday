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
_step = threading.local()


def add_sink(fn: Callable[[dict], None]) -> Callable[[dict], None]:
    with _lock:
        _sinks.append(fn)
    return fn


def remove_sink(fn: Callable[[dict], None]) -> None:
    with _lock:
        if fn in _sinks:
            _sinks.remove(fn)


def set_step(step: int) -> None:
    """Tag subsequent events with the loop step they belong to.

    Thread local because several sessions run at once and a step number from
    one would otherwise label another's events."""
    _step.value = int(step)


def current_step() -> int:
    return int(getattr(_step, "value", 0) or 0)


def emit(region: str, node: str, kind: str = USED, score: float = 0.0,
         label: str = "", **extra: Any) -> None:
    if not node:
        return
    event = {"region": region, "node": str(node), "kind": kind,
             "score": round(float(score or 0.0), 4), "label": label or str(node),
             "step": current_step(), **extra}
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
