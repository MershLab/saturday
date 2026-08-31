"""Install, search, update and remove skills.

`SkillStore` already reads ``~/.saturday/skills/*/SKILL.md``, so installing is
just cloning into that directory - Omarchy's model exactly, and no change to
the loading path.

The format stays plain ``SKILL.md`` with ``name`` and ``description``
frontmatter. Three independent projects converged on it; adding a
Saturday-specific extension would fork a standard for no gain.

Search is the one addition beyond Omarchy, which has none. It queries GitHub's
topic index rather than a registry Saturday would have to run, so authors
self-tag and there is nothing to maintain. **A search result is a lead, not an
endorsement** - that phrasing is in the output on purpose, because an installed
skill's text is injected into the model's prompt, and nobody has reviewed it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TOPIC = "saturday-skill"
SEARCH_URL = "https://api.github.com/search/repositories"
ALLOWED_SCHEMES = ("https", "http", "git", "ssh")
_NAME_RE = re.compile(r"[^a-z0-9_-]+")


class SkillError(Exception):
    """Install/update refused or failed."""


def derive_name(url: str) -> str:
    """Folder name for a clone URL, sanitized to a single path segment.

    The name becomes a directory under the skills root, so anything that could
    climb out of it - separators, dots, empty - is rejected rather than
    cleaned into something surprising."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"\.git$", "", tail)
    tail = re.sub(r"^saturday[-_]skill[-_]?", "", tail, flags=re.IGNORECASE)
    name = _NAME_RE.sub("-", tail.lower()).strip("-")
    if not name or name in (".", ".."):
        raise SkillError(f"cannot derive a skill name from {url!r}")
    return name


def _check_url(url: str) -> None:
    """Accept a remote URL, or a path that is genuinely a local git repo.

    Local paths matter for offline and air-gapped installs, so refusing them
    outright would block a legitimate case; requiring the path to exist AND
    contain a .git means a typo cannot quietly clone something unintended."""
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or ("ssh" if url.startswith("git@") else "")
    if scheme in ALLOWED_SCHEMES or scheme == "file":
        return
    if not scheme:
        candidate = Path(url).expanduser()
        if candidate.is_dir() and (candidate / ".git").exists():
            return
        raise SkillError(f"{url!r} is not a URL and not a local git repository")
    raise SkillError(f"refusing to clone a {scheme} URL: {url!r}")


def skills_root() -> Path:
    from saturday.tools.skills import skills_dir

    return skills_dir()


def list_installed() -> list[dict[str, Any]]:
    """Local only, no network."""
    root = skills_root()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for md in sorted(root.glob("*/SKILL.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        desc = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
        out.append({
            "name": md.parent.name,
            "description": (desc.group(1).strip() if desc else "")[:200],
            "path": str(md.parent),
            "git": (md.parent / ".git").exists(),
            "bytes": md.stat().st_size if md.is_file() else 0,
        })
    return out


def _git(args: list[str], cwd: Path | None = None, timeout: float = 120.0) -> str:
    if shutil.which("git") is None:
        raise SkillError("git is not installed")
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SkillError(f"git timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        raise SkillError((proc.stderr or proc.stdout or "git failed").strip()[:400])
    return proc.stdout


def install(url: str, name: str | None = None, force: bool = False) -> dict[str, Any]:
    """Clone a skill repo into the skills directory.

    A clone that turns out not to contain SKILL.md is removed again: leaving it
    would put an unloadable directory in a folder whose whole contract is that
    everything in it loads."""
    _check_url(url)
    folder = derive_name(name or url)
    dest = skills_root() / folder
    if dest.exists():
        if not force:
            raise SkillError(f"{folder} is already installed (use force to replace it)")
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--depth", "1", url, str(dest)])
    if not (dest / "SKILL.md").is_file():
        shutil.rmtree(dest, ignore_errors=True)
        raise SkillError(f"{url} has no SKILL.md at its root; nothing was installed")
    return {"name": folder, "path": str(dest), "url": url}


def update(name: str | None = None) -> list[dict[str, Any]]:
    """git pull in place. A skill saved by the agent has no remote; skip it."""
    root = skills_root()
    targets = [root / name] if name else [p for p in sorted(root.glob("*")) if p.is_dir()]
    out = []
    for path in targets:
        if not path.is_dir():
            out.append({"name": path.name, "ok": False, "detail": "not installed"})
            continue
        if not (path / ".git").exists():
            out.append({"name": path.name, "ok": True, "detail": "local skill, no remote"})
            continue
        try:
            detail = _git(["pull", "--ff-only"], cwd=path).strip().splitlines()
            out.append({"name": path.name, "ok": True,
                        "detail": (detail[-1] if detail else "up to date")[:200]})
        except SkillError as exc:
            out.append({"name": path.name, "ok": False, "detail": str(exc)[:200]})
    return out


def remove(name: str) -> bool:
    folder = derive_name(name)
    dest = skills_root() / folder
    if not dest.is_dir():
        return False
    shutil.rmtree(dest)
    return True


def search(query: str, limit: int = 10, timeout: float = 12.0) -> dict[str, Any]:
    """GitHub repositories tagged with the skill topic.

    Zero maintenance by design: no index Saturday has to run, authors self-tag.
    The results carry no trust signal beyond stars, which is why the caller is
    expected to repeat the lead-not-endorsement line to the user."""
    q = f"topic:{TOPIC} {query}".strip()
    url = f"{SEARCH_URL}?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page={int(limit)}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "saturday-skill-search",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return {"results": [], "error": "GitHub rate limit reached; try again later"}
        return {"results": [], "error": f"search failed: HTTP {exc.code}"}
    except Exception as exc:
        return {"results": [], "error": f"search unavailable: {type(exc).__name__}"}
    results = [{
        "name": item.get("name") or "",
        "full_name": item.get("full_name") or "",
        "description": (item.get("description") or "")[:200],
        "stars": int(item.get("stargazers_count") or 0),
        "url": item.get("clone_url") or item.get("html_url") or "",
        "updated": item.get("pushed_at") or "",
    } for item in (data.get("items") or [])]
    return {"results": results, "topic": TOPIC,
            "note": "a search result is a lead, not an endorsement"}
