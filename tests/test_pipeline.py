"""Typed pipeline graph: validation, ordering, caching, execution."""
from __future__ import annotations

import json

import pytest

from saturday import pipeline as P


def _linear():
    return {"version": 1, "name": "simple", "nodes": [
        {"id": "n1", "type": "input", "widgets": {}},
        {"id": "a1", "type": "agent", "widgets": {"name": "Writer", "agent": "auto"}},
        {"id": "o1", "type": "output", "widgets": {"target": "chat"}}],
        "edges": [
            {"from": "n1", "out": "task", "to": "a1", "in": "task"},
            {"from": "a1", "out": "result", "to": "o1", "in": "result"}]}


def test_a_valid_pipeline_has_no_problems():
    assert P.validate(_linear()) == []


def test_mismatched_socket_types_are_refused_by_name():
    """The point of typing sockets: a bad wire is caught when the graph is
    saved, not after the nodes before it have spent real model calls."""
    bad = _linear()
    bad["edges"][1] = {"from": "a1", "out": "result", "to": "o1", "in": "artifact"}
    problems = P.validate(bad)
    assert any("a result is not a artifact" in p for p in problems), problems

    worse = {"name": "w", "nodes": [{"id": "a", "type": "agent"}, {"id": "b", "type": "input"}],
             "edges": [{"from": "a", "out": "result", "to": "b", "in": "task"}]}
    assert any("has no 'task' input" in p for p in P.validate(worse))


def test_every_problem_is_reported_not_just_the_first():
    broken = {"name": "b", "nodes": [
        {"id": "a", "type": "agent"}, {"id": "b", "type": "nonsense"},
        {"id": "a", "type": "output"}],
        "edges": [{"from": "a", "out": "nope", "to": "ghost", "in": "result"}]}
    problems = P.validate(broken)
    assert len(problems) >= 3
    assert any("duplicate node id" in p for p in problems)
    assert any("unknown node type" in p for p in problems)


def test_a_cycle_is_caught_and_names_its_nodes():
    cyc = {"name": "c", "nodes": [{"id": "x", "type": "agent"}, {"id": "y", "type": "agent"}],
           "edges": [{"from": "x", "out": "transcript", "to": "y", "in": "transcript"},
                     {"from": "y", "out": "transcript", "to": "x", "in": "transcript"}]}
    problems = P.validate(cyc)
    assert any("cycle" in p and "x" in p and "y" in p for p in problems), problems


def test_topological_order_respects_dependencies():
    pipe = _linear()
    order = [n["id"] for n in P.topo_sort(pipe["nodes"], pipe["edges"])]
    assert order.index("n1") < order.index("a1") < order.index("o1")


def test_run_threads_task_context_and_result(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path)
    pipe = {"version": 1, "name": "ctx", "nodes": [
        {"id": "n1", "type": "input", "widgets": {}},
        {"id": "m1", "type": "memory", "widgets": {}},
        {"id": "a1", "type": "agent", "widgets": {"name": "W"}},
        {"id": "o1", "type": "output", "widgets": {}}],
        "edges": [
            {"from": "n1", "out": "task", "to": "m1", "in": "task"},
            {"from": "n1", "out": "task", "to": "a1", "in": "task"},
            {"from": "m1", "out": "context", "to": "a1", "in": "context"},
            {"from": "a1", "out": "result", "to": "o1", "in": "result"}]}
    seen = {}

    def runner(prompt, widgets):
        seen["prompt"] = prompt
        return "answered"

    out = P.run(pipe, "the task", agent_runner=runner,
                memory_lookup=lambda q: "remembered thing")
    assert out["output"] == "answered"
    assert "remembered thing" in seen["prompt"]
    assert seen["prompt"].endswith("the task"), "the task must come last, after context"


def test_caching_means_a_second_run_spends_nothing(tmp_path, monkeypatch):
    """Caching is a cost requirement here, not a speed optimization."""
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path)
    calls = []

    def runner(prompt, widgets):
        calls.append(prompt)
        return "answer"

    pipe = _linear()
    assert P.run(pipe, "task one", agent_runner=runner)["output"] == "answer"
    assert len(calls) == 1
    P.run(pipe, "task one", agent_runner=runner)
    assert len(calls) == 1, "an unchanged node must not be paid for twice"

    P.run(pipe, "a different task", agent_runner=runner)
    assert len(calls) == 2, "changed input must invalidate the cache"

    P.run(pipe, "task one", agent_runner=runner, use_cache=False)
    assert len(calls) == 3


def test_changing_a_widget_invalidates_that_node(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path)
    calls = []
    pipe = _linear()
    P.run(pipe, "t", agent_runner=lambda p, w: calls.append(1) or "a")
    pipe["nodes"][1]["widgets"]["role"] = "now with a role"
    P.run(pipe, "t", agent_runner=lambda p, w: calls.append(1) or "a")
    assert len(calls) == 2


def test_aggregator_collects_multiple_results(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path)
    pipe = {"version": 1, "name": "fan", "nodes": [
        {"id": "n1", "type": "input", "widgets": {}},
        {"id": "a1", "type": "agent", "widgets": {"name": "A"}},
        {"id": "a2", "type": "agent", "widgets": {"name": "B"}},
        {"id": "g1", "type": "aggregator", "widgets": {"synthesize": False}},
        {"id": "o1", "type": "output", "widgets": {}}],
        "edges": [
            {"from": "n1", "out": "task", "to": "a1", "in": "task"},
            {"from": "n1", "out": "task", "to": "a2", "in": "task"},
            {"from": "a1", "out": "result", "to": "g1", "in": "result"},
            {"from": "a2", "out": "result", "to": "g1", "in": "result"},
            {"from": "g1", "out": "result", "to": "o1", "in": "result"}]}
    out = P.run(pipe, "t", agent_runner=lambda p, w: f"{w['name']} answered")
    assert "A answered" in out["output"] and "B answered" in out["output"]


def test_save_refuses_an_invalid_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path)
    bad = _linear()
    bad["edges"].append({"from": "o1", "out": "result", "to": "n1", "in": "task"})
    with pytest.raises(P.PipelineError):
        P.save("bad", bad)
    assert not P.path_for("bad").exists()

    P.save("good", _linear())
    assert P.load("good")["name"] == "good"
    assert [r["name"] for r in P.list_pipelines()] == ["good"]


def test_pipeline_names_cannot_escape_the_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path)
    for bad in ("../evil", "a/b", "", ".", "x" * 80):
        with pytest.raises(P.PipelineError):
            P.path_for(bad)


def test_unset_model_and_agent_are_defaults_not_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: tmp_path)
    pipe = _linear()
    pipe["nodes"][1]["widgets"] = {"name": "W", "model": None, "agent": "auto"}
    assert P.validate(pipe) == []
    assert P.run(pipe, "t", agent_runner=lambda p, w: "ok")["output"] == "ok"


def test_every_shipped_template_is_valid():
    """A template that does not validate is worse than none: it hands a
    beginner a broken graph and makes the tool look wrong, not the template."""
    assert P.templates(), "there must be somewhere to start"
    for entry in P.templates():
        pipe = P.from_template(entry["id"], entry["id"])
        assert P.validate(pipe) == [], (entry["id"], P.validate(pipe))
        assert entry["about"], f"{entry['id']} must say what it is for"


def test_from_template_is_a_copy_not_a_reference():
    a = P.from_template("one-agent", "a")
    b = P.from_template("one-agent", "b")
    a["nodes"][1]["widgets"]["name"] = "changed"
    assert b["nodes"][1]["widgets"]["name"] != "changed"
    assert P.TEMPLATES["one-agent"]["nodes"][1]["widgets"]["name"] != "changed"


def test_unknown_template_is_refused():
    with pytest.raises(P.PipelineError, match="unknown template"):
        P.from_template("no-such-template", "x")


def test_research_template_forwards_a_transcript_deliberately():
    """The opt-in edge exists for the case where the next agent needs what the
    previous one saw, not just what it concluded."""
    pipe = P.from_template("research-and-write", "r")
    assert any(e["out"] == P.TRANSCRIPT for e in pipe["edges"])
