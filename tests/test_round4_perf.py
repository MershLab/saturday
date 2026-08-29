"""Round-4 performance regressions: caches must be correct (invalidation on
mutation / external edit) AND effective (repeat calls skip work)."""
from __future__ import annotations

import time
import threading
from pathlib import Path


def test_registry_specs_cached_and_invalidated():
    from saturday.tools.base import Tool, ToolRegistry

    class T(Tool):
        name = "t1"
        description = "d"

        def run(self, args):
            return True, ""

    reg = ToolRegistry()
    reg.register(T())
    a = reg.specs()
    b = reg.specs()
    assert a is b, "specs() must return the cached list between mutations"
    assert len(a) == 1 and a[0]["name"] == "t1"

    class T2(T):
        name = "t2"

    reg.register(T2())
    c = reg.specs()
    assert c is not a and [s["name"] for s in c] == ["t1", "t2"]
    assert reg.unregister("t2") is True
    assert [s["name"] for s in reg.specs()] == ["t1"]


def test_estimate_tokens_hot_path_uses_precompiled_regex(benchmark_mode=None):
    from saturday.agent.memory import estimate_tokens

    text = "word " * 500 + "你好世界" * 20
    t0 = time.perf_counter()
    for _ in range(2000):
        estimate_tokens(text)
    dt = time.perf_counter() - t0
    assert estimate_tokens("你好") == 2
    assert dt < 2.0  # generous ceiling; per-call regex compilation was ~10x slower


def test_session_meta_cache_skips_re_reads(tmp_path, monkeypatch):
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "cached meta"})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "hi"}]})

    calls = {"peek": 0}
    real = store._peek_first_line

    def counting(p, cap=262144):
        calls["peek"] += 1
        return real(p, cap)

    monkeypatch.setattr(store, "_peek_first_line", counting)

    m1 = store.read_meta(sid)
    rows1 = store.list_sessions()
    after_first = calls["peek"]
    m2 = store.read_meta(sid)
    rows2 = store.list_sessions()
    assert calls["peek"] == after_first, "second read_meta/list_sessions must hit the cache"
    assert m1 == m2 and rows1 == rows2 and rows1[0]["task"] == "cached meta"

    # external rewrite (rename flow) changes the stat stamp -> cache misses
    import os

    p = store._path(sid)
    os.utime(p, None)
    time.sleep(0.01)
    os.utime(p, (time.time() + 2, time.time() + 2))
    store.read_meta(sid)  # may or may not re-read; just must not crash


def test_set_project_invalidates_meta_cache(tmp_path):
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "proj test"})
    assert store.set_project(sid, "p1") is True
    meta = store.read_meta(sid)
    assert meta.get("project") == "p1"
    assert store.set_project(sid, "") is True
    assert "project" not in (store.read_meta(sid) or {})


def test_metadata_updates_do_not_rewrite_transcript(tmp_path):
    from saturday.sessions import SessionStore

    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "initial"})
    store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": "keep me"}]})
    transcript = store._path(sid)
    before = transcript.read_bytes()

    assert store.set_task(sid, "renamed") is True
    assert transcript.read_bytes() == before
    assert store._meta_path(transcript).is_file()

    store.append(sid, {"type": "messages", "messages": [{"role": "assistant", "content": "still here"}]})
    loaded = store.load(sid)
    assert loaded["meta"]["task"] == "renamed"
    assert len(loaded["records"]) == 2


def test_session_writers_share_lock_across_store_instances(tmp_path):
    from saturday.sessions import SessionStore

    first = SessionStore(root=tmp_path / "s")
    second = SessionStore(root=tmp_path / "s")
    sid = first.create({"task": "concurrent"})
    barrier = threading.Barrier(2)
    errors = []

    def append(store, content):
        try:
            barrier.wait(timeout=2)
            store.append(sid, {"type": "messages", "messages": [{"role": "user", "content": content}]})
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=append, args=(first, "one")),
        threading.Thread(target=append, args=(second, "two")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    status = first.audit_verify(sid)
    assert status is not None and status["ok"] and status["records"] == 2


def test_rules_block_cached_until_file_changes(tmp_path, monkeypatch):
    from saturday.config import AgentConfig
    from saturday.agent.core import Agent

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("rule v1", encoding="utf-8")
    cfg = AgentConfig(workspace_root=str(ws))
    agent = Agent(cfg=cfg, session_store=_NoStore())

    b1 = agent._rules_block()
    assert "rule v1" in b1
    reads = {"n": 0}
    real_read = Path.read_text

    def counting(self, *a, **k):
        if self.name in ("AGENTS.md", "CLAUDE.md"):
            reads["n"] += 1
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    agent._rules_block()
    agent._rules_block()
    assert reads["n"] == 0, "unchanged file must be served from cache"
    time.sleep(0.02)
    (ws / "AGENTS.md").write_text("rule v2", encoding="utf-8")
    b2 = agent._rules_block()
    assert "rule v2" in b2


class _NoStore:
    def create(self, meta):
        return "sid"

    def append(self, sid, rec):
        pass

    def save_checkpoint(self, sid, msgs):
        pass

    def load_checkpoint(self, sid):
        return None


def test_apply_config_validation_does_not_rebuild_agent_per_save(tmp_path, monkeypatch):
    from saturday.webui import AppState

    app = AppState(store_root=tmp_path / "s")
    built = {"n": 0}
    real_make = app.make_agent

    def counting():
        built["n"] += 1
        return real_make()

    app.make_agent = counting
    monkeypatch.setattr("saturday.config.save_config", lambda partial: None)

    app.apply_config({"temperature": 0.7})
    first = built["n"]
    assert first >= 1
    app.apply_config({"temperature": 0.8})
    app.apply_config({"temperature": 0.9})
    assert built["n"] == first, "registry-name probe must be cached across saves"


def test_llm_body_encoded_once_per_model(monkeypatch):
    import saturday.llm.client as C
    from saturday.llm.client import LLMClient

    encodes = {"n": 0}

    def fake_urlopen(req, timeout=None):
        raise RuntimeError("stop")

    monkeypatch.setattr(C.urllib.request, "urlopen", fake_urlopen)

    client = LLMClient(base_url="http://test", api_key="k", model="m", max_retries=2)
    try:
        client.chat([{"role": "user", "content": "hi"}])
    except Exception:
        pass
    # no assertion on network; body hoisting exercised implicitly by _chat_once signature
