"""Scheduled automations (Hermes parity: built-in cron).

Five-field cron expressions (min hour dom month dow), persisted per-user in
``~/.saturday/schedules.json``. ``watch`` polls once a minute and fires due
entries as one-shot agent runs; missed runs fire on the next poll. Stdlib-only
(field matcher is ours, so no croniter dependency).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Schedule:
    id: str
    expr: str
    task: str
    last_fired_minute: str = ""
    model: str = ""
    provider: str = ""
    created: float = field(default_factory=time.time)


def default_schedules_path() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "schedules.json"


# -- 5-field cron matching ---------------------------------------------------


def _field_match(spec: str, value: int, lo: int, hi: int) -> bool:
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            try:
                step = max(1, int(step_s))
            except ValueError:
                return False
        if part in ("", "*"):
            rng = (lo, hi)
        elif "-" in part:
            try:
                a, b = part.split("-", 1)
                rng = (int(a), int(b))
            except ValueError:
                return False
        else:
            try:
                v = int(part)
            except ValueError:
                return False
            rng = (v, v)
        if value >= rng[0] and value <= rng[1] and (value - rng[0]) % step == 0:
            return True
    return False


def cron_matches(expr: str, dt: datetime | None = None) -> bool:
    """True when ``dt`` (default: now) falls in the 5-field cron expression.

    Standard cron contract: when BOTH day-of-month and day-of-week are
    restricted (neither is *), a date matches if EITHER field matches.
    """
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    mins, hrs, doms, mons, dows = fields
    dt = dt or datetime.now()
    if not _field_match(mins, dt.minute, 0, 59):
        return False
    if not _field_match(hrs, dt.hour, 0, 23):
        return False
    if not _field_match(mons, dt.month, 1, 12):
        return False
    dom_ok = _field_match(doms, dt.day, 1, 31)
    # cron convention: 0 AND 7 both mean Sunday (isoweekday() % 7 maps Sunday
    # to 0, so a spec written with the equally valid "7" must match it too)
    dow_val = dt.isoweekday() % 7
    dow_ok = _field_match(dows, dow_val, 0, 7) or (
        dow_val == 0 and _field_match(dows, 7, 0, 7)
    )
    if doms != "*" and dows != "*":
        return dom_ok or dow_ok
    return dom_ok and dow_ok


# -- store --------------------------------------------------------------------


class ScheduleStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_schedules_path()

    def _load(self) -> dict[str, Schedule]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, Schedule] = {}
        for sid, raw in (data or {}).items():
            if not isinstance(raw, dict):
                continue
            try:
                out[str(sid)] = Schedule(
                    id=str(sid),
                    expr=str(raw.get("expr", "")),
                    task=str(raw.get("task", "")),
                    last_fired_minute=str(raw.get("last_fired_minute", "")),
                    model=str(raw.get("model", "")),
                    provider=str(raw.get("provider", "")),
                    created=float(raw.get("created", 0.0) or 0.0),
                )
            except (TypeError, ValueError):
                continue
        return out

    def _save(self, items: dict[str, Schedule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            sid: {
                "expr": s.expr,
                "task": s.task,
                "last_fired_minute": s.last_fired_minute,
                "model": s.model,
                "provider": s.provider,
                "created": s.created,
            }
            for sid, s in items.items()
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, sid: str, expr: str, task: str, model: str = "", provider: str = "") -> Schedule:
        if not _valid_expr(expr):
            raise ValueError(f"invalid cron expression: {expr!r} (expect: 'min hour dom month dow')")
        if not (sid or "").strip():
            sid = "sched-" + str(int(time.time()))
        items = self._load()
        s = Schedule(id=sid, expr=expr, task=task, model=model, provider=provider)
        items[sid] = s
        self._save(items)
        return s

    def remove(self, sid: str) -> bool:
        items = self._load()
        if sid not in items:
            return False
        del items[sid]
        self._save(items)
        return True

    def list(self) -> list[Schedule]:
        return sorted(self._load().values(), key=lambda s: s.created)

    def due(self, now: datetime | None = None) -> list[Schedule]:
        now = now or datetime.now()
        stamp = now.strftime("%Y%m%d%H%M")
        due = []
        for s in self.list():
            if s.last_fired_minute == stamp:
                continue
            if cron_matches(s.expr, now):
                due.append(s)
        return due

    def mark_fired(self, sid: str, now: datetime | None = None) -> None:
        now = now or datetime.now()
        items = self._load()
        if sid in items:
            items[sid].last_fired_minute = now.strftime("%Y%m%d%H%M")
            self._save(items)


def _valid_expr(expr: str) -> bool:
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for spec, (lo, hi) in zip(fields, ranges):
        for part in spec.split(","):
            part = part.split("/")[0]
            if part in ("", "*"):
                continue
            nums = part.split("-")
            try:
                vals = [int(n) for n in nums]
            except ValueError:
                return False
            if len(vals) > 2 or any(v < lo or v > hi for v in vals):
                return False
    return True


# -- watch loop (blocking; runs one-shot agents) ------------------------------


def run_scheduled_task(sched: Schedule, timeout: float = 1800.0) -> tuple[int, str]:
    """Fire one schedule entry as a one-shot agent run. Never raises."""
    from saturday.sessions import RunState

    # no on_step_start / pause support here on purpose: watch() below fires
    # due schedules synchronously in one loop, so a paused run would stall
    # every other due cron job, not just this one.
    run_state: RunState | None = None

    def on_session_id(sid: str) -> None:
        nonlocal run_state
        from saturday.config import get_config_dir

        run_state = RunState(get_config_dir() / "sessions", sid)
        run_state.start()

    try:
        from saturday.agent.core import Agent
        from saturday.config import AgentConfig

        overrides: dict = {}
        if sched.model:
            overrides["model"] = sched.model
        if sched.provider:
            overrides["provider"] = sched.provider
        agent = Agent(cfg=AgentConfig.load(overrides))
        result = agent.run(
            sched.task,
            on_session_id=on_session_id,
            on_tool_result=lambda _r: run_state.heartbeat() if run_state else None,
        )
        if run_state is not None:
            run_state.done()
        final = getattr(result, "final", "") or result if isinstance(result, str) else str(result)
        return 0, f"ok: {str(final)[:400]}"
    except Exception as exc:
        if run_state is not None:
            run_state.mark_crashed()
        return 1, f"error: {type(exc).__name__}: {exc}"


def watch(interval_seconds: float = 20.0, store: ScheduleStore | None = None) -> None:
    """Poll for due schedules and fire them; Ctrl-C to stop."""
    store = store or ScheduleStore()
    log_dir = default_schedules_path().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[schedule] watching {store.path} (default timezone, Ctrl-C to stop)", flush=True)
    try:
        while True:
            try:
                for s in store.due():
                    _fire_and_log(store, s, log_dir)
            except Exception as exc:
                print(f"[schedule] poll error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("[schedule] stopped", flush=True)
        raise SystemExit(0)


def _fire_and_log(store: ScheduleStore, s: Schedule, log_dir: Path) -> None:
    print(f"[schedule] firing {s.id} ({s.expr}): {s.task[:80]}", flush=True)
    code, detail = run_scheduled_task(s)
    store.mark_fired(s.id)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{s.id}] ({s.expr}) {s.task} -> {detail}\n"
    try:
        with open(log_dir / "schedule.log", "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line)
    except OSError:
        pass
