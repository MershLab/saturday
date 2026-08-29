from __future__ import annotations

import threading


class GoalStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.goal: dict | None = None

    def create(self, text: str) -> str:
        with self._lock:
            if self.goal and self.goal["status"] == "active":
                return f"goal already active: {self.goal['text']}. complete or block it first."
            self.goal = {"text": text[:500], "status": "active", "round": 0}
            return f"goal created: {text[:500]}"

    def get(self) -> str:
        with self._lock:
            if not self.goal:
                return "no goal set"
            g = self.goal
            line = f"goal: {g['text']} | status: {g['status']} | round: {g['round']}"
            notes = g.get("notes") or []
            if notes:
                line += "\n" + "\n".join(f"- {n}" for n in notes[-5:])
            return line

    def update(self, action: str, note: str = "") -> str:
        with self._lock:
            if not self.goal:
                return "no goal to update"
            valid = ("complete", "block", "note")
            if action not in valid:
                return f"action must be one of {valid}"
            if note:
                self.goal.setdefault("notes", []).append(note[:300])
            if action == "complete":
                self.goal["status"] = "done"
            elif action == "block":
                self.goal["status"] = "blocked"
            self.goal["round"] += 1
            return self.get()

    def export_state(self) -> dict:
        """Checkpoint payload fragment (goal survives restart/resume)."""
        with self._lock:
            return {"goal": dict(self.goal) if self.goal else None}

    def import_state(self, state: dict) -> None:
        with self._lock:
            goal = state.get("goal")
            self.goal = dict(goal) if isinstance(goal, dict) else None


class CreateGoalTool:
    name = "create_goal"
    description = "Set the single high-level goal this session pursues. One active goal at a time."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self, store: GoalStore) -> None:
        self.store = store

    def export_state(self) -> dict:
        return self.store.export_state()

    def import_state(self, state: dict) -> None:
        self.store.import_state(state)

    def run(self, args: dict) -> tuple[bool, str]:
        text = (args.get("text") or "").strip()
        if not text:
            return False, "empty goal text"
        out = self.store.create(text)
        return True, out


class GetGoalTool:
    name = "get_goal"
    description = "Read the current goal, its status, and round counter."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, store: GoalStore) -> None:
        self.store = store

    def export_state(self) -> dict:
        return self.store.export_state()

    def import_state(self, state: dict) -> None:
        self.store.import_state(state)

    def run(self, args: dict) -> tuple[bool, str]:
        return True, self.store.get()


class UpdateGoalTool:
    name = "update_goal"
    description = "Mark the goal complete/blocked or attach a progress note."
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["complete", "block", "note"]},
            "note": {"type": "string"},
        },
        "required": ["action"],
    }

    def __init__(self, store: GoalStore) -> None:
        self.store = store

    def export_state(self) -> dict:
        return self.store.export_state()

    def import_state(self, state: dict) -> None:
        self.store.import_state(state)

    def run(self, args: dict) -> tuple[bool, str]:
        return True, self.store.update(args.get("action", ""), args.get("note", ""))


def build_goal_tools() -> tuple[GoalStore, list]:
    store = GoalStore()
    return store, [CreateGoalTool(store), GetGoalTool(store), UpdateGoalTool(store)]
