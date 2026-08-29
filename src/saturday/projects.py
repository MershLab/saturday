"""Projects: Codex/Claude-Desktop-style containers that group sessions and
carry their own context — a name, project-scoped instructions (merged into the
agent persona) and an optional workspace folder (used as the agent's sandbox
root for that project's sessions). Persisted as one JSON file under CONFIG_DIR.
Stdlib-only, like the rest of the core.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_NAME = 80
MAX_INSTRUCTIONS = 8000
MAX_FILES = 12
MAX_SCOPED_TOOLS = 40

# preset accent colors ("" = default flame)
COLORS = ("", "red", "orange", "green", "cyan", "blue", "purple", "pink")


def clean_scopes(scopes: Any) -> dict[str, list[str]]:
    """Three-tier authorization scopes (reserved / approval / autonomous):
    tool-name lists per tier. Reserved tools always require approval; approval
    tools ask in ask mode; autonomous tools never ask (deny + hardline still
    apply). Full dict replace; unknown keys rejected."""
    if scopes is None:
        return {}
    if not isinstance(scopes, dict):
        raise ValueError("scopes must be an object with reserved/approval/autonomous lists")
    unknown = [k for k in scopes if k not in ("reserved", "approval", "autonomous")]
    if unknown:
        raise ValueError(f"unknown scope tiers: {', '.join(sorted(unknown))}")
    out: dict[str, list[str]] = {}
    for tier in ("reserved", "approval", "autonomous"):
        raw = scopes.get(tier) or []
        if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
            raise ValueError(f"scopes.{tier} must be a list of tool names")
        names: list[str] = []
        for t in raw[: MAX_SCOPED_TOOLS * 2]:
            name = t.strip().lower()
            if name and name not in names:
                names.append(name)
        if len(names) > MAX_SCOPED_TOOLS:
            raise ValueError(f"scopes.{tier}: max {MAX_SCOPED_TOOLS} tools")
        if names:
            out[tier] = names
    return out


@dataclass
class Project:
    id: str
    name: str
    instructions: str = ""
    workspace: str = ""
    color: str = ""
    files: list[str] = field(default_factory=list)
    scopes: dict[str, list[str]] = field(default_factory=dict)
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "instructions": self.instructions,
            "workspace": self.workspace,
            "color": self.color,
            "files": list(self.files),
            "scopes": {k: list(v) for k, v in self.scopes.items()},
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        raw_files = data.get("files") or []
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            instructions=str(data.get("instructions") or ""),
            workspace=str(data.get("workspace") or ""),
            color=str(data.get("color") or ""),
            files=[str(f) for f in raw_files][:MAX_FILES],
            scopes=clean_scopes(data.get("scopes")),
            created=float(data.get("created") or time.time()),
        )


def clean_name(name: str) -> str:
    out = " ".join(str(name or "").split())[:MAX_NAME]
    if not out:
        raise ValueError("project name is required")
    return out


def clean_instructions(text: str) -> str:
    return str(text or "").strip()[:MAX_INSTRUCTIONS]


def clean_workspace(path: str) -> str:
    p = str(path or "").strip()
    if not p:
        return ""
    expanded = Path(p).expanduser()
    if not expanded.is_dir():
        raise ValueError(f"workspace directory does not exist: {p}")
    return str(expanded.resolve())


def clean_color(color: str) -> str:
    c = str(color or "").strip().lower()
    if c not in COLORS:
        raise ValueError(f"unknown color '{c}'; available: {', '.join(c for c in COLORS if c)}")
    return c


def clean_files(paths: Any) -> list[str]:
    """Knowledge-file paths: must exist as files; deduped; capped at MAX_FILES."""
    if paths is None:
        return []
    if not isinstance(paths, (list, tuple)):
        raise ValueError("files must be a list of paths")
    out: list[str] = []
    for raw in paths[: MAX_FILES * 2]:
        p = Path(str(raw).strip()).expanduser()
        if not p.is_file():
            raise ValueError(f"knowledge file does not exist: {raw}")
        rp = str(p.resolve())
        if rp not in out:
            out.append(rp)
    if len(out) > MAX_FILES:
        raise ValueError(f"max {MAX_FILES} knowledge files per project")
    return out


class ProjectStore:
    """JSON-file-backed project registry."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            from saturday.config import CONFIG_DIR

            path = CONFIG_DIR / "projects.json"
        self.path = Path(path)

    # -- io ----------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, projects: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(projects, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # -- api ---------------------------------------------------------------------
    def list(self) -> list[Project]:
        rows = [Project.from_dict(v) for v in self._load().values()]
        rows.sort(key=lambda p: (p.created, p.id))
        return rows

    def get(self, pid: str) -> Project | None:
        raw = self._load().get(str(pid))
        return Project.from_dict(raw) if raw else None

    def create(
        self,
        name: str,
        instructions: str = "",
        workspace: str = "",
        color: str = "",
        files: Any = None,
        scopes: Any = None,
    ) -> Project:
        projects = self._load()
        base = re.sub(r"[^a-z0-9]+", "-", clean_name(name).lower()).strip("-")[:40] or "project"
        pid, n = base, 2
        while pid in projects:
            pid = f"{base}-{n}"
            n += 1
        proj = Project(
            id=pid,
            name=clean_name(name),
            instructions=clean_instructions(instructions),
            workspace=clean_workspace(workspace),
            color=clean_color(color),
            files=clean_files(files),
            scopes=clean_scopes(scopes),
        )
        projects[pid] = proj.to_dict()
        self._save(projects)
        return proj

    def update(
        self,
        pid: str,
        *,
        name: str | None = None,
        instructions: str | None = None,
        workspace: str | None = None,
        color: str | None = None,
        files: Any = None,
        scopes: Any = None,
    ) -> Project:
        projects = self._load()
        raw = projects.get(str(pid))
        if raw is None:
            raise KeyError(pid)
        proj = Project.from_dict(raw)
        if name is not None:
            proj.name = clean_name(name)
        if instructions is not None:
            proj.instructions = clean_instructions(instructions)
        if workspace is not None:
            proj.workspace = clean_workspace(workspace)
        if color is not None:
            proj.color = clean_color(color)
        if files is not None:
            proj.files = clean_files(files)
        if scopes is not None:
            proj.scopes = clean_scopes(scopes)
        projects[proj.id] = proj.to_dict()
        self._save(projects)
        return proj

    def delete(self, pid: str) -> bool:
        projects = self._load()
        if str(pid) not in projects:
            return False
        del projects[str(pid)]
        self._save(projects)
        return True
