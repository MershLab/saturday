"""Tamper-evident session audit: hash-chained records, legacy migration,
tamper detection, export bundles, and the CLI surface."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from saturday.sessions import (  # noqa: E402
    GENESIS_HASH,
    SessionStore,
    canonical_json,
    record_hash,
    verify_chain,
)


def test_new_records_are_hash_chained(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    sid = store.create({"task": "chain"})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "a"}]})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "b"}]})
    data = store.load(sid)
    recs = data["records"]
    assert len(recs) == 2 and all(r.get("hash") for r in recs)
    # each record commits to the previous hash
    expected_first = record_hash(GENESIS_HASH, {k: v for k, v in recs[0].items() if k != "hash"})
    assert recs[0]["hash"] == expected_first
    expected_second = record_hash(recs[0]["hash"], {k: v for k, v in recs[1].items() if k != "hash"})
    assert recs[1]["hash"] == expected_second
    status = store.audit_verify(sid)
    assert status["ok"] is True and status["hashed"] == 2 and status["legacy"] == 0
    assert status["head"] == recs[1]["hash"]


def test_tamper_detection(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    sid = store.create({"task": "t"})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "honest"}]})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "more"}]})
    p = store._path(sid)
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["messages"][0]["content"] = "forged"
    lines[1] = json.dumps(rec, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    status = store.audit_verify(sid)
    assert status["ok"] is False and status["broken_at"] == 0


def test_deletion_breaks_chain(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    sid = store.create({"task": "t"})
    store.append(sid, {"type": "messages", "messages": []})
    store.append(sid, {"type": "messages", "messages": []})
    p = store._path(sid)
    lines = p.read_text(encoding="utf-8").splitlines()
    del lines[1]  # remove the first record
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert store.audit_verify(sid)["ok"] is False


def test_legacy_session_migrates_and_commits_history(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    sid = store.create({"task": "legacy"})
    # simulate a pre-chain session: records without hashes
    p = store._path(sid)
    legacy = [
        {"type": "messages", "messages": [{"role": "user", "content": "old-1"}]},
        {"type": "messages", "messages": [{"role": "user", "content": "old-2"}]},
    ]
    p.write_text(
        json.dumps({"type": "meta", "id": sid}) + "\n" + "\n".join(json.dumps(r) for r in legacy) + "\n",
        encoding="utf-8",
    )
    status = store.audit_verify(sid)
    assert status["ok"] is True and status["legacy"] == 2 and status["hashed"] == 0

    # a new append chains FROM the legacy block (all legacy records committed)
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "new"}]})
    status = store.audit_verify(sid)
    assert status["ok"] is True and status["hashed"] == 1 and status["legacy"] == 2

    # tampering with a LEGACY record must now break the chain
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["messages"][0]["content"] = "rewritten history"
    lines[1] = json.dumps(rec)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert store.audit_verify(sid)["ok"] is False


def test_verify_chain_standalone():
    recs = [{"n": 1, "hash": record_hash(GENESIS_HASH, {"n": 1})}]
    recs.append({"n": 2, "hash": record_hash(recs[0]["hash"], {"n": 2})})
    assert verify_chain(recs)["ok"] is True
    assert verify_chain([{"n": 9, "hash": recs[0]["hash"]}])["ok"] is False
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_audit_export_bundle(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    sid = store.create({"task": "export me", "surface": "cli"})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "x"}]})
    bundle = store.audit_export(sid)
    assert bundle["schema"] == 1
    assert bundle["meta"]["task"] == "export me"
    assert bundle["chain"]["ok"] is True and len(bundle["records"]) == 1
    # bundle is deterministic apart from nothing - chain verifies independently
    assert verify_chain(bundle["records"])["ok"] is True


def test_cli_audit_command(tmp_path: Path, capsys):
    from saturday.cli import cmd_audit

    store = SessionStore(tmp_path / "sessions")
    sid = store.create({"task": "cli"})
    store.append(sid, {"type": "messages", "messages": []})

    class A:
        session_id = sid
        export = str(tmp_path / "bundle.json")
        root = str(tmp_path / "sessions")

    assert cmd_audit(A()) == 0
    out = capsys.readouterr().out
    assert "chain OK" in out
    bundle = json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))
    assert bundle["session_id"] == sid and bundle["chain"]["ok"] is True

    # tamper -> non-zero exit
    p = store._path(sid)
    lines = p.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["type"] = "tampered"
    lines[1] = json.dumps(rec)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    class B:
        session_id = sid
        export = None
        root = str(tmp_path / "sessions")

    assert cmd_audit(B()) == 1
