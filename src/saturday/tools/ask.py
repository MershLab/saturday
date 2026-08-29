"""Interactive question tool: ask the human a clarifying question mid-run.

Lovable/Windsurf-style question cards. The web surface publishes an ``ask``
event and resolves the answer via POST /api/ask; surfaces without a hook
(headless CLI) return a hint so the agent proceeds with best judgment instead
of stalling. Stdlib-only."""
from __future__ import annotations

from typing import Callable

from saturday.tools.base import Tool


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "Ask the human a clarifying question and WAIT for their answer. Use when a "
        "decision genuinely needs their input (ambiguous goal, missing credentials, "
        "a destructive or irreversible choice). Provide 2-8 short options when "
        "possible; the user may also type a free-text answer. Returns their answer "
        "verbatim, or a note that they did not answer (then proceed with your best "
        "judgment). Never call this twice in a row."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-8 short candidate answers the user can pick with one click",
            },
        },
        "required": ["question"],
    }

    # Per-instance callable set by the surface (web: rt.approver.ask_question).
    # Signature: (question: str, options: list[str], ttl: float | None) -> str
    ask_fn: Callable[[str, list[str], float | None], str] | None = None

    def run(self, args: dict):
        q = str(args.get("question") or "").strip()
        if not q:
            return False, "empty question"
        options = [str(o).strip()[:200] for o in (args.get("options") or []) if str(o).strip()][:8]
        fn = self.ask_fn
        if fn is None:
            return True, "(no user surface available to answer; proceed with your best judgment)"
        try:
            answer = str(fn(q, options, None) or "")
        except Exception as exc:
            return False, f"ask_user failed: {type(exc).__name__}: {exc}"
        if not answer:
            return True, "(user did not answer in time; proceed with your best judgment)"
        return True, f'user answered: "{answer}"'
