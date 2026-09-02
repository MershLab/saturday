"""Skill ranking: confidence-weighted shortlist ordering.

Design: `internal-docs/saturday/skill-ranking-architecture.md` (private). The
problem it answers: a flat, unordered skill list is useless once there are
more than a handful of skills installed, and a user who watches a fabricated
ranking pick wrong twice trusts the feature less than the flat list it
replaced. So every position has to be earned by real evidence, and the
system has to say so when it has none (`untried`).

    score = pin
          + (1 - c) * relevance
          + c * outcome
          + c * attention
          + w_rec * recency

    c = confidence = n / (n + K)     n = times this skill was actually loaded

At n=0 the score is pure text relevance and the entry reads `untried`; as a
skill accrues real `skill_load` calls, its own record starts to dominate.

Two things this pass deliberately does NOT build, stated here rather than
left silent:

* **task_kind stays "" (global) only.** The doc's specific/domain/global
  three-level backoff needs task text clustered via `memscore.bands` (LSH),
  which is real machinery but a second, separable piece of work — half of it
  (a global bucket with no per-cluster narrowing) is still strictly better
  than the flat list this replaces, and shipping the whole three-level model
  half-verified would be worse than shipping the honest global-only slice.
* **`ema_success` has no writer yet.** `record_outcome` exists and is
  tested, but nothing calls it: attributing a later tool failure back to an
  earlier `skill_load` correctly needs turn-scoped tracking through five
  separate loop entry points (cli/webui/gateway/schedule/repl), and a wrong
  attribution is worse than no signal at all — the doc's own rule ("never
  infer an outcome that does not exist") applies exactly here. Every skill
  therefore reads `ema_success=0.5` (neutral) until that wiring lands as its
  own pass. `used`/`considered`/`recency`/pin are all real, observed signals
  today and drive the ranking on their own.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from saturday import memscore

K = 4.0            # confidence half-saturation point, per the design doc
W_RECENCY = 0.15    # small, fixed: a skill used yesterday nudges ahead of an
                    # identical-looking one used a year ago, nothing more
RECENCY_HALF_LIFE_DAYS = 14.0
PIN_BONUS = 10.0    # "pin beats any learned score, absolute"
CONSIDERED_ONLY_FLOOR = 5  # considered this many times with 0 uses -> flag it


def _db_path() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "skills.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_stats (
          skill TEXT NOT NULL,
          task_kind TEXT NOT NULL DEFAULT '',
          used INTEGER DEFAULT 0,
          considered INTEGER DEFAULT 0,
          ema_success REAL DEFAULT 0.5,
          last_used REAL,
          pin INTEGER DEFAULT 0,
          PRIMARY KEY (skill, task_kind)
        );
        """
    )
    return con


def _row(con: sqlite3.Connection, skill: str, task_kind: str) -> dict:
    r = con.execute(
        "SELECT used, considered, ema_success, last_used, pin FROM skill_stats "
        "WHERE skill=? AND task_kind=?",
        (skill, task_kind),
    ).fetchone()
    if r is None:
        return {"used": 0, "considered": 0, "ema_success": 0.5, "last_used": None, "pin": 0}
    return {"used": r[0], "considered": r[1], "ema_success": r[2], "last_used": r[3], "pin": r[4]}


def stats(skill: str, task_kind: str = "") -> dict:
    with _connect() as con:
        return _row(con, skill, task_kind)


def record_considered(skill: str, task_kind: str = "") -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO skill_stats(skill, task_kind, considered) VALUES(?,?,1) "
            "ON CONFLICT(skill, task_kind) DO UPDATE SET considered=considered+1",
            (skill, task_kind),
        )


def record_used(skill: str, task_kind: str = "", now: float | None = None) -> None:
    now = now if now is not None else time.time()
    with _connect() as con:
        con.execute(
            "INSERT INTO skill_stats(skill, task_kind, used, last_used) VALUES(?,?,1,?) "
            "ON CONFLICT(skill, task_kind) DO UPDATE SET used=used+1, last_used=excluded.last_used",
            (skill, task_kind, now),
        )


def record_outcome(skill: str, ok: bool, task_kind: str = "", alpha: float = 0.3) -> None:
    """Update the success EMA. Real signal only — see the module docstring
    for why nothing calls this yet."""
    with _connect() as con:
        row = _row(con, skill, task_kind)
        new_ema = (1 - alpha) * row["ema_success"] + alpha * (1.0 if ok else 0.0)
        con.execute(
            "INSERT INTO skill_stats(skill, task_kind, ema_success) VALUES(?,?,?) "
            "ON CONFLICT(skill, task_kind) DO UPDATE SET ema_success=excluded.ema_success",
            (skill, task_kind, new_ema),
        )


def set_pin(skill: str, value: int, task_kind: str = "") -> None:
    """value: 1 = pinned (always shortlisted), -1 = buried (never, unless
    named outright), 0 = neutral. Absolute: outranks any learned score."""
    value = 1 if value > 0 else (-1 if value < 0 else 0)
    with _connect() as con:
        con.execute(
            "INSERT INTO skill_stats(skill, task_kind, pin) VALUES(?,?,?) "
            "ON CONFLICT(skill, task_kind) DO UPDATE SET pin=excluded.pin",
            (skill, task_kind, value),
        )


def _confidence(n: float, k: float = K) -> float:
    n = max(0.0, n)
    return n / (n + k)


def _relevance(task_sig: tuple[int, ...] | None, skill_text: str) -> float:
    if not task_sig:
        return 0.0
    return memscore.jaccard(task_sig, memscore.minhash(skill_text))


def score_one(row: dict, relevance: float, now: float) -> float:
    n = float(row["used"])
    c = _confidence(n)
    attention_sig = min(1.0, n / 3.0)
    last_used = row["last_used"]
    rec = memscore.recency(last_used, now, RECENCY_HALF_LIFE_DAYS) if last_used else 0.0
    raw = (1 - c) * relevance + c * row["ema_success"] + c * attention_sig + W_RECENCY * rec
    if row["pin"] > 0:
        raw += PIN_BONUS
    elif row["pin"] < 0:
        raw -= PIN_BONUS
    return raw


def _reason(row: dict) -> str:
    used, considered, pin = row["used"], row["considered"], row["pin"]
    bits = []
    if used > 0:
        bits.append(f"used {used}x")
    elif considered >= CONSIDERED_ONLY_FLOOR:
        bits.append(f"considered {considered}x, never loaded — description may not be matching real tasks")
    else:
        bits.append("untried" if considered == 0 else "untried - matched on text")
    if pin > 0:
        bits.append("pinned")
    elif pin < 0:
        bits.append("buried")
    return ", ".join(bits)


def rank(entries: list[tuple[str, str]], task_text: str = "", task_kind: str = "",
         now: float | None = None) -> list[dict]:
    """entries: (name, description) pairs, e.g. from SkillStore.index().

    Returns every entry, scored and sorted (pinned first, buried last), each
    with a `reason` string safe to show a user. Nothing is dropped — buried
    skills sink to the bottom but stay reachable, per the design doc's rule
    that bury means "never shortlist unless named outright", not "delete"."""
    now = now if now is not None else time.time()
    task_sig = memscore.minhash(task_text) if task_text.strip() else None
    with _connect() as con:
        rows = {name: _row(con, name, task_kind) for name, _ in entries}
    out = []
    for name, desc in entries:
        row = rows[name]
        rel = _relevance(task_sig, f"{name}: {desc}")
        out.append({
            "name": name,
            "description": desc,
            "score": score_one(row, rel, now),
            "reason": _reason(row),
            "untried": row["used"] == 0,
            "pin": row["pin"],
        })
    out.sort(key=lambda e: e["score"], reverse=True)
    return out
