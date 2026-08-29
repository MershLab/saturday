from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from saturday.types import Trajectory


class Verifier:
    """Rule-based verifiable reward, in the spirit of DeepSeek-R1's RL checkers."""

    def __init__(self, fn: Callable[[Trajectory], float], name: str = "") -> None:
        self.fn = fn
        self.name = name or getattr(fn, "__name__", "verifier")

    def __call__(self, traj: Trajectory) -> float:
        try:
            return float(self.fn(traj))
        except Exception:
            return 0.0


def contains_any(*needles: str) -> Verifier:
    def check(traj: Trajectory) -> float:
        answer = (traj.final_answer or "").lower()
        return 1.0 if any(n.lower() in answer for n in needles) else 0.0

    check.__name__ = f"contains_any:{needles[0]!r}"
    return Verifier(check)


def regex_matches(pattern: str) -> Verifier:
    import re

    rx = re.compile(pattern)

    def check(traj: Trajectory) -> float:
        return 1.0 if rx.search(traj.final_answer or "") else 0.0

    check.__name__ = f"regex:{pattern[:24]}"
    return Verifier(check)


def file_created(path: str, must_contain: tuple[str, ...] = (), root: str | None = None) -> Verifier:
    def check(traj: Trajectory) -> float:
        p = Path(path)
        if root and not p.is_absolute():
            # agents operate inside the workspace root; resolving against the
            # eval process CWD instead produced false FAILs
            p = Path(root) / p
        if not p.is_file():
            return 0.0
        text = p.read_text(encoding="utf-8", errors="replace")
        for needle in must_contain:
            if needle not in text:
                return 0.0
        return 1.0

    check.__name__ = f"file_created:{path}"
    return Verifier(check)


def command_exits_zero(command: str, root: str | None = None) -> Verifier:
    import subprocess

    def check(traj: Trajectory) -> float:
        proc = subprocess.run(command, shell=True, capture_output=True, timeout=120, cwd=root)
        return 1.0 if proc.returncode == 0 else 0.0

    check.__name__ = f"cmd_zero:{command[:24]}"
    return Verifier(check)


def composite(*verifiers: Verifier) -> Verifier:
    def check(traj: Trajectory) -> float:
        if not verifiers:
            return 1.0
        return sum(v(traj) for v in verifiers) / len(verifiers)

    check.__name__ = "composite:" + "+".join(v.name for v in verifiers)
    return Verifier(check)


@dataclass
class EvalCase:
    id: str
    task: str
    verifier: Verifier


@dataclass
class EvalResult:
    case_id: str
    reward: float
    stop_reason: str | None
    steps: int
    total_tokens: int
    final_answer: str
    trajectory_path: str | None = None


class EvalRunner:
    def __init__(
        self,
        agent_factory: Callable[[], Any],
        out_dir: str | None = None,
        *,
        provenance: dict[str, str] | None = None,
        root: str | None = None,
    ) -> None:
        self.agent_factory = agent_factory
        self.out_dir = Path(out_dir) if out_dir else None
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        # workspace the evaluated agents operate in; suite builders should
        # resolve their relative paths against this, not the process CWD
        self.root = root
        # {"provider":..., "model":..., "session_id":...}; stamped onto saved
        # trajectory records when provenance marking is enabled.
        self.provenance = provenance or {}

    def _save(self, case_id: str, agent: Any, traj: Trajectory) -> Path | None:
        from saturday.provenance import stamp_record

        # case ids are interpolated into filenames; strip path separators etc.
        case_id = re.sub(r"[^\w.-]", "_", case_id)

        record = traj.to_jsonl_record()
        cfg = getattr(agent, "cfg", None)
        marking = getattr(cfg, "provenance_marking", "metadata") or "metadata"
        if marking == "off":
            path = self.out_dir / f"{case_id}.json"
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            return path
        prov = dict(self.provenance or {})
        prov.setdefault("provider", getattr(cfg, "provider", ""))
        prov.setdefault("model", getattr(cfg, "model", ""))
        stamped = stamp_record(record, provider=prov.get("provider", ""), model=prov.get("model", ""), session_id=prov.get("session_id", ""))
        path = self.out_dir / f"{case_id}.json"
        path.write_text(json.dumps(stamped, indent=2), encoding="utf-8")
        return path

    def run(self, cases: list[EvalCase], *, save_trajectories: bool = True) -> list[EvalResult]:
        results: list[EvalResult] = []
        for case in cases:
            agent = self.agent_factory()
            traj = agent.run(case.task)
            traj.reward = case.verifier(traj)
            path = None
            if save_trajectories and self.out_dir:
                path = self._save(case.id, agent, traj)
            results.append(
                EvalResult(
                    case_id=case.id,
                    reward=traj.reward,
                    stop_reason=traj.stop_reason,
                    steps=len(traj.steps),
                    total_tokens=traj.usage.total_tokens,
                    final_answer=(traj.final_answer or "")[:2000],
                    trajectory_path=str(path) if path else None,
                )
            )
        return results

    @staticmethod
    def summarize(results: list[EvalResult]) -> dict:
        n = len(results) or 1
        return {
            "cases": len(results),
            "mean_reward": sum(r.reward for r in results) / n,
            "pass_rate": sum(1 for r in results if r.reward >= 0.999) / n,
            "total_tokens": sum(r.total_tokens for r in results),
        }
