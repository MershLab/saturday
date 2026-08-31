"""Attention events: the scores retrieval already produced, carried out."""
from __future__ import annotations

import threading

import pytest

from saturday import attention


@pytest.fixture(autouse=True)
def _clean_sinks():
    yield
    for fn in list(attention._sinks):
        attention.remove_sink(fn)
    attention.set_step(0)
    attention.set_run("")


def test_events_carry_tier_score_and_step():
    seen = []
    attention.add_sink(seen.append)
    attention.set_step(4)
    attention.emit(attention.MEMORY, "a-note", attention.USED, 0.87, "the note text")
    assert seen == [{"region": "memory", "node": "a-note", "kind": "used",
                     "score": 0.87, "label": "the note text", "step": 4, "run": ""}]


def test_emit_ranked_marks_the_cutoff_rather_than_dropping_losers():
    """The considered tier is the point: what retrieval scored and rejected is
    invisible everywhere else, and is what you need when an answer is wrong."""
    seen = []
    attention.add_sink(seen.append)
    hits = [{"slug": f"n{i}", "score": 1.0 - i / 10, "text": f"note {i}"} for i in range(5)]
    attention.emit_ranked(attention.MEMORY, hits, used=2, node_key="slug")
    kinds = [e["kind"] for e in seen]
    assert kinds == ["used", "used", "considered", "considered", "considered"]
    assert [e["score"] for e in seen[:2]] == [1.0, 0.9]


def test_a_failing_sink_never_breaks_the_run_that_produced_it():
    calls = []

    def boom(_e):
        raise RuntimeError("watcher exploded")

    attention.add_sink(boom)
    attention.add_sink(calls.append)
    attention.emit(attention.CODE, "a.py")
    assert len(calls) == 1, "one bad watcher must not starve the others"


def test_steps_do_not_leak_between_threads():
    """Several sessions run at once; one session's step number must not label
    another's events."""
    attention.set_step(7)
    other = {}

    def worker():
        other["before"] = attention.current_step()
        attention.set_step(99)
        other["after"] = attention.current_step()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert other == {"before": 0, "after": 99}
    assert attention.current_step() == 7


def test_empty_node_names_are_dropped():
    seen = []
    attention.add_sink(seen.append)
    attention.emit(attention.CODE, "")
    attention.emit_ranked(attention.CODE, [{"score": 1.0}], used=1)
    assert seen == []


def test_memory_search_publishes_what_it_ranked(tmp_path):
    from saturday.memindex import MemoryIndex

    seen = []
    attention.add_sink(seen.append)
    idx = MemoryIndex(db_path=tmp_path / "m.db")
    idx.reindex("- kubernetes ingress uses nginx\n"
                "- kubernetes ingress rules live in the chart\n"
                "- kubernetes nodes autoscale on cpu\n"
                "- billing reconciliation runs monthly\n")
    seen.clear()
    idx.search("kubernetes ingress", k=2)
    assert seen, "a real retrieval must publish what it looked at"
    assert [e["kind"] for e in seen].count("used") == 2
    assert any(e["kind"] == "considered" for e in seen)
    # nothing that failed to match is reported as looked at
    assert all("billing" not in e["label"] for e in seen)
    idx.close()


def test_context_survives_the_tool_pool(tmp_path, monkeypatch):
    """Tools execute on a ThreadPoolExecutor. A context that stops at the loop
    thread made every event report step 0 and belong to no run, which broke
    both the time dimension and per-session attribution at once."""
    from fakes import FakeLLM, assistant

    from saturday.agent.core import Agent
    from saturday.config import AgentConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("def verify():\n    return 1\n", encoding="utf-8")

    attention.set_run("sess-abc")
    seen = []
    attention.add_sink(lambda e: seen.append((e["run"], e["step"], e["region"])))

    script = [assistant(tool_calls=[("repo_search", {"query": "verify"})]),
              assistant(tool_calls=[("repo_search", {"query": "verify"})]),
              assistant(content="done")]
    cfg = AgentConfig.load({"workspace_root": str(ws), "provider": "ollama", "model": "x"})
    Agent(cfg=cfg, client=FakeLLM(script), enable_subagents=False).run("go")

    assert seen, "a real run must publish what its tools looked at"
    assert all(run == "sess-abc" for run, _s, _r in seen), seen
    steps = [s for _r, s, _g in seen]
    assert steps == sorted(steps) and len(set(steps)) > 1, \
        f"steps must advance with the loop, got {steps}"


def test_restore_installs_a_captured_context():
    import threading as _th

    attention.set_run("outer")
    attention.set_step(5)
    captured = attention.snapshot()
    got = {}

    def worker():
        got["blank"] = attention.snapshot()
        attention.restore(captured)
        got["restored"] = attention.snapshot()

    t = _th.Thread(target=worker)
    t.start()
    t.join()
    assert got["blank"] == ("", 0), "a fresh thread starts with no context"
    assert got["restored"] == ("outer", 5)
