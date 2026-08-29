from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

# Prefix-aware bullet stripper. The previous lstrip("-*0123456789. ") removed
# any leading characters FROM THE SET, eating digits that belong to the step
# text itself ("2+2 checks" -> "+2 checks"); a regex only removes a real
# leading marker ("-", "*", "1.", "2)").
_BULLET_PREFIX_RX = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")


@dataclass
class PlanStep:
    text: str
    done: bool = False


@dataclass
class Plan:
    goal: str = ""
    steps: list[PlanStep] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"goal: {self.goal or '(unset)'}"]
        for i, s in enumerate(self.steps, 1):
            mark = "x" if s.done else " "
            lines.append(f"{i}. [{mark}] {s.text}")
        return "\n".join(lines)

    def progress(self) -> tuple[int, int]:
        return sum(1 for s in self.steps if s.done), len(self.steps)


class TodoTool:
    """Agent-intercepted-style todo tool (cf. hermes-agent): mutates loop-visible task state."""

    name = "todo"
    description = (
        "Create and maintain your task plan. Call with 'write' to set the full step list "
        "(one per line), 'mark' with a 1-based index to complete a step, or 'read' to view it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["write", "mark", "read"]},
            "steps_text": {"type": "string", "description": "for write: one step per line"},
            "index": {"type": "integer", "description": "for mark: 1-based step index"},
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        self.plan = Plan()
        self._lock = threading.Lock()

    def run(self, args: dict) -> tuple[bool, str]:
        action = args.get("action")
        with self._lock:
            if action == "read":
                done, total = self.plan.progress()
                return True, f"progress: {done}/{total}\n{self.plan.render()}"
            if action == "write":
                text = args.get("steps_text", "")
                steps = []
                for raw in text.splitlines():
                    line = _BULLET_PREFIX_RX.sub("", raw.strip()).strip()
                    if line:
                        steps.append(PlanStep(text=line[:300]))
                if not steps:
                    return False, "no steps parsed from steps_text"
                self.plan.steps = steps
                return True, f"plan set with {len(steps)} steps\n{self.plan.render()}"
            if action == "mark":
                idx = int(args.get("index") or 0)
                if not 1 <= idx <= len(self.plan.steps):
                    return False, f"index out of range (have {len(self.plan.steps)} steps)"
                self.plan.steps[idx - 1].done = True
                done, total = self.plan.progress()
                return True, f"marked #{idx}; progress {done}/{total}"
            return False, f"unknown action '{action}'"

    def export_state(self) -> dict:
        """Checkpoint payload fragment (plan survives restart/resume)."""
        with self._lock:
            return {
                "goal": self.plan.goal,
                "steps": [{"text": s.text, "done": s.done} for s in self.plan.steps],
            }

    def import_state(self, state: dict) -> None:
        with self._lock:
            self.plan.goal = str(state.get("goal") or "")
            self.plan.steps = [
                PlanStep(text=str(s.get("text", ""))[:300], done=bool(s.get("done")))
                for s in state.get("steps") or []
                if isinstance(s, dict)
            ]
