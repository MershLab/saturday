"""Structural code memory: the vendored codebase-memory-mcp binary.

Vendored, not depended on at runtime. Upstream's PyPI wrapper downloads a
binary from its own GitHub Releases on first run, which would hand Saturday's
release integrity to someone else's infrastructure and require network at a
moment the user did not ask for one. Saturday instead pins the checksums here,
fetches deliberately, and verifies before anything is executed.

It already speaks MCP and Saturday is already an MCP client, so integration is
registering it as a server - no bespoke subprocess protocol.

Absent, Saturday falls back to the lexical `repo_search` index, the same gate
pattern `browser_playwright.py` uses. Structural retrieval is an upgrade, never
a requirement.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

VERSION = "v0.10.8"
BASE_URL = f"https://github.com/DeusData/codebase-memory-mcp/releases/download/{VERSION}"

# SHA256s taken from upstream's signed checksums.txt, which was itself verified
# against the digest the GitHub API reports for that asset. Never transcribe
# these by hand from a web page - re-derive them with scripts/pin_codemem.py.
ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("Linux", "x86_64"): (
        "codebase-memory-mcp-linux-amd64-portable.tar.gz",
        "6eef49652bc0c7820f43114125044d40bf7f4d97c11b2592f6b0f6a307702325"),
    ("Linux", "aarch64"): (
        "codebase-memory-mcp-linux-arm64-portable.tar.gz",
        "5697d986d9716c913163b4bff7b3a294287f3b843e993bc1ff71e78dcdc21781"),
    ("Darwin", "x86_64"): (
        "codebase-memory-mcp-darwin-amd64.tar.gz",
        "2b193085410af3801634a522f4b17dcd6699695e015a068393c87817c1d260d4"),
    ("Darwin", "arm64"): (
        "codebase-memory-mcp-darwin-arm64.tar.gz",
        "9bd840dfb3ec7eaef4f310382057adaa5b0e904df883104d03ffcf39836afd07"),
    ("Windows", "AMD64"): (
        "codebase-memory-mcp-windows-amd64.zip",
        "b43ad982994c4d829670749e08d3b622a74bb20041fc0a7d02bef6113f81c34d"),
}
BINARY_NAME = "codebase-memory-mcp"
MCP_ALIAS = "codebase-memory"
_MACHINE_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}


class VerificationError(Exception):
    """The downloaded asset did not match its pinned checksum."""


def _machine() -> str:
    m = platform.machine()
    if platform.system() == "Darwin":
        return "arm64" if m in ("arm64", "aarch64") else "x86_64"
    if platform.system() == "Windows":
        return "AMD64"
    return _MACHINE_ALIASES.get(m.lower(), m)


def asset_for_platform() -> tuple[str, str] | None:
    """(filename, sha256) for this machine, or None if unsupported."""
    return ASSETS.get((platform.system(), _machine()))


def cache_dir() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "vendor" / "codebase-memory-mcp" / VERSION


def bundled_dir() -> Path:
    """Where a Saturday installer puts the binary it shipped with."""
    return Path(__file__).resolve().parent / "vendor" / "codebase-memory-mcp"


def find_binary() -> Path | None:
    """Bundled first, then the verified cache, then PATH.

    PATH last on purpose: a user who installed it themselves is trusted, but a
    binary Saturday verified beats one it merely found."""
    exe = BINARY_NAME + (".exe" if os.name == "nt" else "")
    for root in (bundled_dir(), cache_dir()):
        candidate = root / exe
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        for nested in root.glob(f"*/{exe}"):   # archives often carry a top dir
            if nested.is_file():
                return nested
    found = shutil.which(BINARY_NAME)
    return Path(found) if found else None


def available() -> bool:
    return find_binary() is not None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _safe_extract(archive: Path, dest: Path) -> None:
    """Extract without letting an entry escape dest.

    A tar entry may name ../ or an absolute path; trusting the archive layout
    is how an unpack becomes an arbitrary file write."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def safe(name: str) -> bool:
        target = (dest / name).resolve()
        return target == root or root in target.parents

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if safe(m)]
            if len(members) != len(zf.namelist()):
                raise VerificationError("archive contains entries outside the target directory")
            zf.extractall(dest, members=members)
        return
    with tarfile.open(archive) as tf:
        members = [m for m in tf.getmembers() if safe(m.name) and not m.issym() and not m.islnk()]
        if len(members) != len(tf.getmembers()):
            raise VerificationError("archive contains entries outside the target directory")
        tf.extractall(dest, members=members)


def install(progress=None, timeout: float = 180.0) -> Path:
    """Download, verify against the pinned checksum, then extract.

    Verification happens BEFORE extraction, so a mismatched archive is never
    unpacked, let alone run."""
    import urllib.request

    spec = asset_for_platform()
    if spec is None:
        raise VerificationError(
            f"no pinned build for {platform.system()}/{_machine()}")
    name, want = spec
    url = f"{BASE_URL}/{name}"
    tmp = Path(tempfile.mkdtemp(prefix="saturday-codemem-"))
    archive = tmp / name
    try:
        if progress:
            progress(f"downloading {name}")
        with urllib.request.urlopen(url, timeout=timeout) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        got = sha256_file(archive)
        if got != want:
            raise VerificationError(
                f"checksum mismatch for {name}: expected {want}, got {got}")
        if progress:
            progress("checksum verified; extracting")
        dest = cache_dir()
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        _safe_extract(archive, dest)
        binary = find_binary()
        if binary is None:
            raise VerificationError("archive did not contain the expected binary")
        try:
            binary.chmod(binary.stat().st_mode | 0o111)
        except OSError:
            pass
        return binary
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mcp_spec() -> dict[str, Any] | None:
    """The MCP server entry for the vendored binary, or None when absent."""
    binary = find_binary()
    if binary is None:
        return None
    return {"command": str(binary), "args": ["serve", "--stdio"]}


def status() -> dict[str, Any]:
    """What `doctor`, the CLI and the web UI all report from."""
    binary = find_binary()
    spec = asset_for_platform()
    return {
        "available": binary is not None,
        "binary": str(binary) if binary else "",
        "version": VERSION,
        "platform": f"{platform.system()}/{_machine()}",
        "supported": spec is not None,
        "asset": spec[0] if spec else "",
        "retrieval": "structural (codebase-memory)" if binary else "lexical (repo_search)",
    }
