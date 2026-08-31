"""Skill install/search/update/remove: Omarchy's model, plus a search."""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from saturday import skillhub


def _repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    for fn, body in files.items():
        (d / fn).write_text(body, encoding="utf-8")
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
        subprocess.run(["git", *cmd], cwd=d, check=True, capture_output=True)
    return d


SKILL_MD = "---\nname: deploy\ndescription: how to deploy the thing\n---\n\nsteps\n"


def test_derive_name_cannot_escape_the_skills_directory():
    """The derived name becomes a directory, so separators and dots must not
    survive into it."""
    assert skillhub.derive_name("https://github.com/x/saturday-skill-deploy.git") == "deploy"
    assert skillhub.derive_name("git@github.com:x/My_Skill.git") == "my_skill"
    for bad in ("https://h/x/..", "../../etc", "..", "/", "%2e%2e"):
        try:
            got = skillhub.derive_name(bad)
        except skillhub.SkillError:
            continue
        assert "/" not in got and got not in ("..", "."), bad


def test_only_sane_schemes_and_real_local_repos_are_cloned(tmp_path):
    for bad in ("ftp://evil/x", "javascript:alert(1)"):
        with pytest.raises(skillhub.SkillError, match="refusing"):
            skillhub.install(bad)
    with pytest.raises(skillhub.SkillError, match="not a local git"):
        skillhub.install(str(tmp_path / "nope"))


def test_install_list_update_remove_roundtrip(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    monkeypatch.setattr(skillhub, "skills_root", lambda: root)
    src = _repo(tmp_path, "saturday-skill-deploy", {"SKILL.md": SKILL_MD})

    out = skillhub.install(str(src))
    assert out["name"] == "deploy"
    listed = skillhub.list_installed()
    assert [s["name"] for s in listed] == ["deploy"]
    assert listed[0]["description"] == "how to deploy the thing"
    assert listed[0]["git"] is True

    with pytest.raises(skillhub.SkillError, match="already installed"):
        skillhub.install(str(src))
    assert skillhub.install(str(src), force=True)["name"] == "deploy"

    assert all(r["ok"] for r in skillhub.update())
    assert skillhub.remove("deploy") is True
    assert skillhub.list_installed() == []
    assert skillhub.remove("deploy") is False


def test_a_repo_without_skill_md_leaves_nothing_behind(tmp_path, monkeypatch):
    """The skills folder's whole contract is that everything in it loads."""
    root = tmp_path / "skills"
    monkeypatch.setattr(skillhub, "skills_root", lambda: root)
    src = _repo(tmp_path, "notaskill", {"README.md": "nope"})
    with pytest.raises(skillhub.SkillError, match="no SKILL.md"):
        skillhub.install(str(src))
    assert not (root / "notaskill").exists()


def test_a_locally_saved_skill_is_not_treated_as_broken(tmp_path, monkeypatch):
    """skill_save writes a folder with no remote; update must not call it a
    failure just because there is nothing to pull."""
    root = tmp_path / "skills"
    (root / "hand-written").mkdir(parents=True)
    (root / "hand-written" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    monkeypatch.setattr(skillhub, "skills_root", lambda: root)
    results = skillhub.update()
    assert results == [{"name": "hand-written", "ok": True, "detail": "local skill, no remote"}]


def test_search_reports_rate_limiting_instead_of_raising(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 403, "rate limited", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    out = skillhub.search("deploy")
    assert out["results"] == [] and "rate limit" in out["error"]


def test_search_parses_results_and_carries_the_caveat(monkeypatch):
    payload = json.dumps({"items": [{
        "name": "deploy", "full_name": "someone/saturday-skill-deploy",
        "description": "d" * 400, "stargazers_count": 12,
        "clone_url": "https://github.com/someone/saturday-skill-deploy.git",
        "pushed_at": "2026-08-01T00:00:00Z"}]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp(payload))
    out = skillhub.search("deploy")
    assert out["results"][0]["stars"] == 12
    assert len(out["results"][0]["description"]) == 200
    assert "lead, not an endorsement" in out["note"]
