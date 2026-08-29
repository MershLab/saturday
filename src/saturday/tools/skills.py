from __future__ import annotations

import re
from pathlib import Path

from saturday.tools.base import Tool


def skills_dir() -> Path:
    from saturday.config import CONFIG_DIR

    return CONFIG_DIR / "skills"


class SkillStore:
    """Agent-curated procedures: one folder per skill with a SKILL.md file."""

    MAX_BODY = 16_000

    def _skill_path(self, name: str) -> Path | None:
        safe = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
        if not safe:
            return None
        return skills_dir() / safe / "SKILL.md"

    def save(self, name: str, description: str, body: str) -> tuple[bool, str]:
        path = self._skill_path(name)
        if path is None:
            return False, "invalid skill name"
        if len(body) > self.MAX_BODY:
            return False, f"skill too large ({len(body)} chars; max {self.MAX_BODY})"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            front = f"---\nname: {name}\ndescription: {description[:200]}\n---\n"
            path.write_text(front + body.strip() + "\n", encoding="utf-8")
            return True, f"saved skill '{name}' -> {path}"
        except OSError as exc:
            return False, f"cannot write skill: {exc}"

    def load(self, name: str) -> tuple[bool, str]:
        path = self._skill_path(name)
        if path is None or not path.is_file():
            return False, f"no skill named '{name}'"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            return True, text[-self.MAX_BODY:]
        except OSError as exc:
            return False, str(exc)

    def index(self) -> list[tuple[str, str]]:
        root = skills_dir()
        out: list[tuple[str, str]] = []
        if not root.is_dir():
            return out
        for md in sorted(root.glob("*/SKILL.md")):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
            out.append((md.parent.name, (m.group(1).strip() if m else "")[:150]))
        return out


class SkillSaveTool(Tool):
    name = "skill_save"
    description = (
        "Persist a reusable procedure you just mastered so future sessions can load it. "
        "Use after solving something non-obvious: write the steps compactly."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "short kebab-case id, e.g. 'deploy-vllm'"},
            "description": {"type": "string", "description": "one line: when to use this"},
            "body": {"type": "string", "description": "markdown procedure"},
        },
        "required": ["name", "description", "body"],
    }

    def __init__(self, store: SkillStore) -> None:
        self.store = store

    def run(self, args: dict) -> tuple[bool, str]:
        name = str(args.get("name") or "").strip()
        description = str(args.get("description") or "").strip()
        body = str(args.get("body") or "")
        if not name or not description or not body.strip():
            return False, "name, description and body are required"
        ok, msg = self.store.save(name, description, body)
        return (True, msg) if ok else (False, msg)


class SkillLoadTool(Tool):
    name = "skill_load"
    description = "Load a saved skill's full procedure into context by id."
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def __init__(self, store: SkillStore) -> None:
        self.store = store

    def run(self, args: dict) -> tuple[bool, str]:
        name = str(args.get("name") or "").strip()
        if not name:
            return False, "name required"
        return self.store.load(name)


class SkillsIndexTool(Tool):
    name = "skills_index"
    description = "List all saved skills with their ids and descriptions."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, store: SkillStore) -> None:
        self.store = store

    def run(self, args: dict) -> tuple[bool, str]:
        entries = self.store.index()
        if not entries:
            return True, "(no skills saved yet)"
        return True, "\n".join(f"- {n}: {d}" for n, d in entries)


def build_skill_tools() -> tuple[SkillStore, list[Tool]]:
    store = SkillStore()
    return store, [SkillSaveTool(store), SkillLoadTool(store), SkillsIndexTool(store)]


def skills_prompt_block(store: SkillStore) -> str:
    entries = store.index()
    if not entries:
        return (
            "# Skills\nNo skills saved yet. When you solve something non-obvious and reusable, "
            "capture the procedure with `skill_save`."
        )
    listing = "\n".join(f"- {n}: {d}" for n, d in entries)
    return (
        "# Saved skills\n"
        f"{listing}\n"
        "Prefer loading a matching skill before reinventing a procedure; "
        "improve it with `skill_save` (same id) after discovering better steps."
    )
