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
        ok, msg = self.store.load(name)
        if ok:
            # an explicit load is the strongest signal this module ever
            # gets - not inferred from a ranked search, a real pick
            try:
                from saturday import attention

                desc = next((d for n, d in self.store.index() if n == name), "")
                attention.emit(attention.SKILL, name, attention.USED, 1.0, desc)
            except Exception:
                pass
            try:
                from saturday import skill_stats

                skill_stats.record_used(name)
            except Exception:
                pass
        return ok, msg


class SkillInstallTool(Tool):
    name = "skill_install"
    description = (
        "Install a skill by cloning its git repo into the skills directory, the same "
        "place skill_save writes to. Its SKILL.md becomes visible to skill_load and shows "
        "up in every future prompt afterward, same as any other installed skill - a search "
        "result is a lead, not an endorsement, since nobody has reviewed its instructions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "git clone URL, or a local path to a git repo"},
            "name": {"type": "string", "description": "folder name override (default: derived from the URL)"},
            "force": {"type": "boolean", "description": "replace an existing skill with this name"},
        },
        "required": ["url"],
    }

    def run(self, args: dict) -> tuple[bool, str]:
        url = str(args.get("url") or "").strip()
        if not url:
            return False, "url required"
        name = str(args.get("name") or "").strip() or None
        force = bool(args.get("force", False))
        from saturday import skillhub

        try:
            out = skillhub.install(url, name=name, force=force)
        except skillhub.SkillError as exc:
            return False, str(exc)
        return True, f"installed {out['name']} -> {out['path']}"


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
    return store, [SkillSaveTool(store), SkillLoadTool(store), SkillsIndexTool(store), SkillInstallTool()]


SHORTLIST_SIZE = 5


def skills_prompt_block(store: SkillStore, task_text: str = "") -> str:
    entries = store.index()
    if not entries:
        return (
            "# Skills\nNo skills saved yet. When you solve something non-obvious and reusable, "
            "capture the procedure with `skill_save`."
        )
    try:
        from saturday import attention

        for n, d in entries:
            attention.emit(attention.SKILL, n, attention.CONSIDERED, 0.0, d)
    except Exception:
        pass
    try:
        from saturday import skill_stats

        for n, _ in entries:
            skill_stats.record_considered(n)
        ranked = skill_stats.rank(entries, task_text)
    except Exception:
        # ranking must never take the skill list down with it
        ranked = [{"name": n, "description": d, "reason": "", "pin": 0} for n, d in entries]

    shortlist = [r for r in ranked if r["pin"] >= 0][:SHORTLIST_SIZE]
    shortlist_names = {r["name"] for r in shortlist}
    rest = [r for r in ranked if r["name"] not in shortlist_names]

    lines = ["# Saved skills", "Most likely relevant here"]
    for i, r in enumerate(shortlist, 1):
        reason = f"   {r['reason']}" if r["reason"] else ""
        lines.append(f"{i}. {r['name']}: {r['description']}{reason}")
    if rest:
        lines.append("Also installed: " + ", ".join(r["name"] for r in rest))
    lines.append(
        "Prefer loading a matching skill before reinventing a procedure; "
        "improve it with `skill_save` (same id) after discovering better steps."
    )
    return "\n".join(lines)
