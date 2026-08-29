"""hermes-verify parity: recipe detection and verification reporting."""
from __future__ import annotations

import subprocess

from saturday.verify import detect_project, run_verification


def test_detect_pytest_from_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    labels = [label for label, _ in detect_project(tmp_path)]
    assert "pytest" in labels


def test_detect_skips_without_toolchain(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    # npm may or may not exist here; only assert other recipes absent
    labels = [label for label, _ in detect_project(tmp_path)]
    assert "pytest" not in labels and "_test" not in labels


def test_run_verification_reports_failures(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "boom", "trace")

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = run_verification(tmp_path, [("pytest", ["python", "-m", "pytest", "-q"])])
    assert results[0][1] is False and "boom" in results[0][2]


def test_run_verification_timeout_never_raises(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    results = run_verification(tmp_path, [("pytest", ["python", "-m", "pytest", "-q"])])
    assert results[0][1] is False and "timed out" in results[0][2]
