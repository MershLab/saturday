"""Vendored structural code index: pinning, verification, safe unpacking."""
from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from saturday import codemem


def test_every_pinned_asset_has_a_real_sha256():
    for (system, machine), (name, digest) in codemem.ASSETS.items():
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), name
        assert name.startswith("codebase-memory-mcp-")
    assert len(codemem.ASSETS) == 5
    # every pin must be distinct: a copy-paste slip would silently accept the
    # wrong platform's archive
    assert len({d for _n, d in codemem.ASSETS.values()}) == 5


def test_status_is_honest_when_the_binary_is_absent(monkeypatch):
    monkeypatch.setattr(codemem, "find_binary", lambda: None)
    st = codemem.status()
    assert st["available"] is False
    assert st["retrieval"].startswith("lexical")
    assert codemem.mcp_spec() is None


def test_status_reports_structural_when_present(tmp_path, monkeypatch):
    fake = tmp_path / "codebase-memory-mcp"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(codemem, "find_binary", lambda: fake)
    st = codemem.status()
    assert st["available"] is True and st["retrieval"].startswith("structural")
    spec = codemem.mcp_spec()
    assert spec["command"] == str(fake) and "--stdio" in spec["args"]


def test_a_bad_checksum_is_refused_before_extraction(tmp_path, monkeypatch):
    """Verification must happen before unpacking: a mismatched archive should
    never be written to disk, let alone executed."""
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tf:
        data = b"not the real binary"
        info = tarfile.TarInfo("codebase-memory-mcp")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    blob = payload.getvalue()

    monkeypatch.setattr(codemem, "asset_for_platform",
                        lambda: ("evil.tar.gz", "0" * 64))
    monkeypatch.setattr(codemem, "cache_dir", lambda: tmp_path / "cache")

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp(blob))
    with pytest.raises(codemem.VerificationError, match="checksum mismatch"):
        codemem.install()
    assert not (tmp_path / "cache").exists(), "a rejected archive must not be unpacked"


def test_a_traversal_entry_is_refused(tmp_path):
    """A tar entry naming ../ turns an unpack into an arbitrary file write."""
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        data = b"pwned"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(codemem.VerificationError, match="outside the target"):
        codemem._safe_extract(archive, tmp_path / "dest")
    assert not (tmp_path / "escaped.txt").exists()


def test_a_zip_traversal_entry_is_refused(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.txt", "pwned")
    with pytest.raises(codemem.VerificationError, match="outside the target"):
        codemem._safe_extract(archive, tmp_path / "dest")
    assert not (tmp_path / "escaped.txt").exists()


def test_a_good_archive_extracts_and_is_found(tmp_path, monkeypatch):
    inner = b"#!/bin/sh\necho hi\n"
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as tf:
        info = tarfile.TarInfo("codebase-memory-mcp")
        info.size = len(inner)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(inner))
    blob = archive_bytes.getvalue()
    digest = hashlib.sha256(blob).hexdigest()

    cache = tmp_path / "cache"
    monkeypatch.setattr(codemem, "asset_for_platform", lambda: ("good.tar.gz", digest))
    monkeypatch.setattr(codemem, "cache_dir", lambda: cache)
    monkeypatch.setattr(codemem, "bundled_dir", lambda: tmp_path / "nope")

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp(blob))
    path = codemem.install()
    assert path.is_file() and path.name == "codebase-memory-mcp"
    assert codemem.sha256_file(path) == hashlib.sha256(inner).hexdigest()


def test_doctor_names_which_retrieval_is_active(monkeypatch):
    from saturday.config import AgentConfig
    from saturday.diagnostics import run_checks

    monkeypatch.setattr(codemem, "find_binary", lambda: None)
    check = next(c for c in run_checks(AgentConfig.load(), offline=True) if c["id"] == "codemem")
    assert "lexical" in check["detail"]
    assert check["status"] == "ok", "falling back is normal, not a failure"
