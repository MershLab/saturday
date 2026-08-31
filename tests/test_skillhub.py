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


def test_search_returns_what_a_detail_pane_needs(monkeypatch, tmp_path):
    """One line per result cannot answer "should I install this". The browser
    shows licence, age, size and reach, so the search has to carry them."""
    payload = json.dumps({"total_count": 41, "items": [{
        "name": "deploy", "full_name": "someone/saturday-skill-deploy",
        "owner": {"login": "someone"},
        "description": "d" * 600, "stargazers_count": 12, "forks_count": 3,
        "open_issues_count": 1, "size": 2048, "language": "Python",
        "license": {"spdx_id": "MIT"},
        "topics": ["saturday-skill", "deployment"],
        "homepage": "https://example.invalid", "archived": False,
        "html_url": "https://github.com/someone/saturday-skill-deploy",
        "clone_url": "https://github.com/someone/saturday-skill-deploy.git",
        "created_at": "2025-01-02T00:00:00Z",
        "pushed_at": "2026-08-01T00:00:00Z"}]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(skillhub, "skills_root", lambda: tmp_path / "skills")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp(payload))
    out = skillhub.search("deploy")
    r = out["results"][0]
    assert out["total"] == 41
    assert r["stars"] == 12 and r["forks"] == 3 and r["issues"] == 1
    assert r["license"] == "MIT" and r["language"] == "Python"
    assert r["size_kb"] == 2048 and r["owner"] == "someone"
    assert r["updated"] == "2026-08-01" and r["created"] == "2025-01-02"
    # the tag every result shares carries no information, so it is dropped
    assert r["topics"] == ["deployment"]
    assert len(r["description"]) == 400
    assert r["installed"] is False
    assert "lead, not an endorsement" in out["note"]


def test_search_marks_what_is_already_installed(monkeypatch, tmp_path):
    """Offering to install something twice is how a browser wastes your time."""
    root = tmp_path / "skills"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    monkeypatch.setattr(skillhub, "skills_root", lambda: root)
    payload = json.dumps({"items": [{
        "name": "deploy", "full_name": "x/saturday-skill-deploy",
        "clone_url": "https://github.com/x/saturday-skill-deploy.git"}]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp(payload))
    assert skillhub.search("")["results"][0]["installed"] is True


def test_an_unrecognised_licence_is_named_not_shown_as_a_code(monkeypatch, tmp_path):
    """GitHub returns NOASSERTION for a licence it cannot identify. Showing
    that verbatim tells a reader nothing."""
    monkeypatch.setattr(skillhub, "skills_root", lambda: tmp_path / "s")
    payload = json.dumps({"items": [{
        "name": "x", "full_name": "a/x", "clone_url": "https://h/a/x.git",
        "license": {"spdx_id": "NOASSERTION"}}]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp(payload))
    assert skillhub.search("")["results"][0]["license"] == "unrecognised"
