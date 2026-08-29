"""Project verification (hermes verify parity).

Recipe detection: look at a project directory, run the test suite the
project implies (pytest / npm / cargo / go / make), and report per-recipe
outcomes. Stdlib-only; missing toolchains are simply skipped at detection.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _has_pytest(root: Path) -> bool:
    if (root / "pytest.ini").is_file():
        return True
    if (root / "tests").is_dir():
        return True
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        return "pytest" in text and ("tool.pytest" in text or "pytest" in text)
    except OSError:
        return False


def _which(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def detect_project(root: Path) -> list[tuple[str, list[str]]]:
    """Return [(label, argv)] recipes for the project at ``root``."""
    root = Path(root)
    out: list[tuple[str, list[str]]] = []
    if _has_pytest(root):
        out.append(("pytest", ["python", "-m", "pytest", "-q"]))
    if (root / "package.json").is_file() and _which("npm"):
        out.append(("npm test", ["npm", "test"]))
    if (root / "Cargo.toml").is_file() and _which("cargo"):
        out.append(("cargo test", ["cargo", "test"]))
    if (root / "go.mod").is_file() and _which("go"):
        out.append(("go test", ["go", "test", "./..."]))
    if (root / "Makefile").is_file() and _which("make"):
        out.append(("make test", ["make", "test"]))
    return out


def run_verification(
    root: Path, detections: list[tuple[str, list[str]]], timeout: float = 600.0
) -> list[tuple[str, bool, str]]:
    """Run each detected recipe; returns [(label, ok, tail)]. Never raises."""
    results: list[tuple[str, bool, str]] = []
    for label, argv in detections:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            results.append((label, proc.returncode == 0, tail[-2000:]))
        except subprocess.TimeoutExpired:
            results.append((label, False, f"timed out after {timeout}s"))
        except OSError as exc:
            results.append((label, False, f"{type(exc).__name__}: {exc}"))
    return results
