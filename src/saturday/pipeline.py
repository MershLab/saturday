"""Pipelines: a typed graph of agent steps, executed headless.

ComfyUI's two mechanics are the ones worth copying, and both are load-bearing
rather than cosmetic:

* **Typed sockets.** Every socket carries a type, and an edge between
  incompatible types is refused when the pipeline is saved rather than
  discovered when it runs. That is what keeps a graph comprehensible instead
  of a tangle.
* **Cached execution keyed by resolved inputs.** Here it is a cost
  requirement, not a speed optimization: without it, editing the last node of
  a five-node pipeline re-pays for five real model calls.

`RESULT` is the default handoff between agents. Forwarding a whole
`TRANSCRIPT` is opt-in per edge because it costs an order of magnitude more
latency and, counterintuitively, loses information by burying the conclusion.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

VERSION = 1
TASK, RESULT, TRANSCRIPT, CONTEXT, ARTIFACT = "task", "result", "transcript", "context", "artifact"

# node kind -> (accepted input sockets, produced output sockets)
NODE_KINDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "input":      ((), (TASK,)),
    "memory":     ((TASK,), (CONTEXT,)),
    "router":     ((TASK,), (TASK,)),
    "agent":      ((TASK, CONTEXT, TRANSCRIPT), (RESULT, TRANSCRIPT, ARTIFACT)),
    "aggregator": ((RESULT,), (RESULT,)),
    "output":     ((RESULT, ARTIFACT), ()),
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


class PipelineError(Exception):
    """The pipeline is not runnable, and why."""


def pipelines_dir() -> Path:
    from saturday.config import get_config_dir

    return get_config_dir() / "pipelines"


def _safe_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise PipelineError(f"invalid pipeline name: {name!r}")
    return name


def path_for(name: str) -> Path:
    return pipelines_dir() / f"{_safe_name(name)}.json"


def validate(pipeline: dict[str, Any]) -> list[str]:
    """Every reason this pipeline cannot run, not just the first.

    Reporting one problem at a time turns fixing a graph into a guessing
    game, so this collects them all."""
    problems: list[str] = []
    nodes = pipeline.get("nodes")
    edges = pipeline.get("edges")
    if not isinstance(nodes, list) or not nodes:
        return ["pipeline has no nodes"]
    if not isinstance(edges, list):
        return ["pipeline edges must be a list"]

    by_id: dict[str, dict] = {}
    for node in nodes:
        nid = str(node.get("id") or "")
        kind = str(node.get("type") or "")
        if not nid:
            problems.append("a node has no id")
            continue
        if nid in by_id:
            problems.append(f"duplicate node id {nid!r}")
            continue
        if kind not in NODE_KINDS:
            problems.append(f"{nid}: unknown node type {kind!r}")
            continue
        by_id[nid] = node

    for edge in edges:
        src, dst = str(edge.get("from") or ""), str(edge.get("to") or "")
        out_sock = str(edge.get("out") or "")
        in_sock = str(edge.get("in") or "")
        if src not in by_id or dst not in by_id:
            problems.append(f"edge {src or '?'} -> {dst or '?'} names a node that does not exist")
            continue
        produced = NODE_KINDS[by_id[src]["type"]][1]
        accepted = NODE_KINDS[by_id[dst]["type"]][0]
        if out_sock not in produced:
            problems.append(
                f"{src} ({by_id[src]['type']}) has no '{out_sock}' output; it produces "
                + (", ".join(produced) or "nothing"))
        if in_sock not in accepted:
            problems.append(
                f"{dst} ({by_id[dst]['type']}) has no '{in_sock}' input; it accepts "
                + (", ".join(accepted) or "nothing"))
        if out_sock in produced and in_sock in accepted and out_sock != in_sock:
            problems.append(
                f"{src}.{out_sock} cannot feed {dst}.{in_sock}: a {out_sock} is not a {in_sock}")

    try:
        topo_sort(list(by_id.values()), [e for e in edges
                                         if str(e.get("from")) in by_id and str(e.get("to")) in by_id])
    except PipelineError as exc:
        problems.append(str(exc))
    return problems


def topo_sort(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Execution order. Raises on a cycle, naming the nodes involved."""
    by_id = {str(n["id"]): n for n in nodes}
    incoming: dict[str, int] = {nid: 0 for nid in by_id}
    outgoing: dict[str, list[str]] = {nid: [] for nid in by_id}
    for e in edges:
        src, dst = str(e.get("from")), str(e.get("to"))
        if src in by_id and dst in by_id:
            incoming[dst] += 1
            outgoing[src].append(dst)
    ready = sorted(nid for nid, n in incoming.items() if n == 0)
    order: list[dict] = []
    while ready:
        nid = ready.pop(0)
        order.append(by_id[nid])
        for nxt in outgoing[nid]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    if len(order) != len(by_id):
        stuck = sorted(nid for nid, n in incoming.items() if n > 0)
        raise PipelineError("pipeline has a cycle involving: " + ", ".join(stuck))
    return order


# --------------------------------------------------------------------- store

def list_pipelines() -> list[dict[str, Any]]:
    root = pipelines_dir()
    out = []
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out.append({"name": path.stem, "nodes": 0, "valid": False,
                        "problems": ["file is not readable JSON"]})
            continue
        problems = validate(data)
        out.append({"name": data.get("name") or path.stem,
                    "nodes": len(data.get("nodes") or []),
                    "edges": len(data.get("edges") or []),
                    "valid": not problems, "problems": problems})
    return out


def load(name: str) -> dict[str, Any]:
    path = path_for(name)
    if not path.is_file():
        raise PipelineError(f"no pipeline named {name!r}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{name}: {type(exc).__name__}: {exc}") from exc


def save(name: str, pipeline: dict[str, Any]) -> Path:
    """Refuse to store a pipeline that cannot run.

    Catching a bad wire at save time is the whole point of typing the
    sockets; letting it through to fail mid-run would waste real model calls
    made by the nodes before it."""
    problems = validate(pipeline)
    if problems:
        raise PipelineError("; ".join(problems))
    pipeline = dict(pipeline, version=VERSION, name=name)
    path = path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pipeline, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------- caching

def _cache_db() -> sqlite3.Connection:
    from saturday.config import get_config_dir

    path = get_config_dir() / "pipelines" / "cache.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pipeline_cache ("
        " pipeline TEXT, node_id TEXT, input_hash TEXT, output_json TEXT,"
        " created_at REAL, PRIMARY KEY (pipeline, node_id, input_hash))")
    return conn


def input_hash(widgets: dict, inputs: dict) -> str:
    """Identity of one node execution: its own settings plus what reached it."""
    blob = json.dumps({"w": widgets, "i": inputs}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_get(pipeline: str, node_id: str, key: str) -> Any:
    with _cache_db() as conn:
        row = conn.execute(
            "SELECT output_json FROM pipeline_cache WHERE pipeline=? AND node_id=? AND input_hash=?",
            (pipeline, node_id, key)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def cache_put(pipeline: str, node_id: str, key: str, value: Any) -> None:
    with _cache_db() as conn:
        conn.execute(
            "INSERT INTO pipeline_cache(pipeline, node_id, input_hash, output_json, created_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(pipeline, node_id, input_hash) DO UPDATE SET"
            " output_json=excluded.output_json, created_at=excluded.created_at",
            (pipeline, node_id, key, json.dumps(value, ensure_ascii=False, default=str), time.time()))


def clear_cache(pipeline: str | None = None) -> int:
    with _cache_db() as conn:
        cur = (conn.execute("DELETE FROM pipeline_cache WHERE pipeline=?", (pipeline,))
               if pipeline else conn.execute("DELETE FROM pipeline_cache"))
        return cur.rowcount


# ----------------------------------------------------------------- execution

def run(pipeline: dict[str, Any], task: str, *, agent_runner: Callable[..., str] | None = None,
        memory_lookup: Callable[[str], str] | None = None,
        on_event: Callable[[dict], None] | None = None,
        use_cache: bool = True) -> dict[str, Any]:
    """Execute a pipeline and return every node's output.

    `agent_runner(prompt, widgets)` is injected rather than imported so the
    engine is testable without spending a model call, and so a caller can
    route through subagents, an external CLI, or the router."""
    problems = validate(pipeline)
    if problems:
        raise PipelineError("; ".join(problems))
    name = str(pipeline.get("name") or "pipeline")
    nodes = pipeline["nodes"]
    edges = pipeline["edges"]
    order = topo_sort(nodes, edges)

    incoming: dict[str, list[dict]] = {str(n["id"]): [] for n in nodes}
    for e in edges:
        incoming[str(e["to"])].append(e)

    outputs: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []

    def emit(event: str, **rest: Any) -> None:
        if on_event:
            on_event({"type": event, "pipeline": name, **rest})

    for node in order:
        nid = str(node["id"])
        kind = str(node["type"])
        widgets = dict(node.get("widgets") or {})
        resolved: dict[str, Any] = {}
        for e in incoming[nid]:
            value = outputs.get(str(e["from"]), {}).get(str(e["out"]))
            socket = str(e["in"])
            if socket in resolved and isinstance(resolved[socket], list):
                resolved[socket].append(value)
            elif socket in resolved:
                resolved[socket] = [resolved[socket], value]
            else:
                resolved[socket] = value
        if kind == "input":
            resolved = {}

        key = input_hash(widgets, resolved if kind != "input" else {"task": task})
        cached = cache_get(name, nid, key) if use_cache else None
        if cached is not None:
            outputs[nid] = cached
            steps.append({"node": nid, "type": kind, "cached": True, "outputs": cached})
            emit("pipeline_node", node=nid, kind=kind, cached=True)
            continue

        emit("pipeline_node", node=nid, kind=kind, cached=False)
        produced: dict[str, Any] = {}
        if kind == "input":
            produced = {TASK: task}
        elif kind == "memory":
            query = str(resolved.get(TASK) or task)
            produced = {CONTEXT: (memory_lookup(query) if memory_lookup else "")}
        elif kind == "router":
            # decides, does not do - but the decision has to REACH the agent, or
            # the node is decorative. It is recorded against this node id and
            # picked up by whichever agent node this one feeds.
            produced = {TASK: resolved.get(TASK, task), "agent": _pick_agent(widgets)}
        elif kind == "agent":
            prompt = _compose_prompt(resolved, task)
            widgets = dict(widgets, agent=_effective_agent(widgets, incoming[nid], outputs))
            answer = agent_runner(prompt, widgets) if agent_runner else ""
            produced = {RESULT: answer, TRANSCRIPT: answer, ARTIFACT: None}
        elif kind == "aggregator":
            parts = resolved.get(RESULT)
            parts = parts if isinstance(parts, list) else [parts]
            joined = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(parts) if p)
            if agent_runner and widgets.get("synthesize", True):
                produced = {RESULT: agent_runner(
                    "Synthesize these answers into one:\n\n" + joined, widgets)}
            else:
                produced = {RESULT: joined}
        elif kind == "output":
            produced = {"value": resolved.get(RESULT) or resolved.get(ARTIFACT)}

        outputs[nid] = produced
        if use_cache and kind != "output":
            cache_put(name, nid, key, produced)
        steps.append({"node": nid, "type": kind, "cached": False, "outputs": produced})

    finals = [s["outputs"].get("value") for s in steps if s["type"] == "output"]
    emit("pipeline_done", steps=len(steps))
    return {"pipeline": name, "steps": steps,
            "output": next((f for f in finals if f), None)}


def _effective_agent(widgets: dict, incoming_edges: list[dict], outputs: dict) -> str:
    """Which agent this node actually runs on.

    Precedence: what the node itself declares, then an upstream router's
    choice, then the router asked directly. "auto" left unresolved would make
    both the router node and the auto default inert, which is what they were."""
    declared = widgets.get("agent")
    if declared and declared != "auto":
        return str(declared)
    for edge in incoming_edges:
        upstream = outputs.get(str(edge["from"]), {})
        chosen = upstream.get("agent")
        if chosen and chosen != "auto":
            return str(chosen)
    return _pick_agent(widgets)


def _pick_agent(widgets: dict) -> str:
    """Unset means the router decides; that is a default, never an error."""
    declared = widgets.get("agent")
    if declared and declared != "auto":
        return str(declared)
    try:
        from saturday.routing import pick

        return pick(str(widgets.get("task_kind") or "general")) or "auto"
    except Exception:
        return "auto"


def _compose_prompt(resolved: dict, task: str) -> str:
    parts = []
    context = resolved.get(CONTEXT)
    if context:
        parts.append(f"Context you already know:\n{context}")
    transcript = resolved.get(TRANSCRIPT)
    if transcript:
        parts.append(f"Earlier transcript:\n{transcript}")
    parts.append(str(resolved.get(TASK) or task))
    return "\n\n".join(p for p in parts if p)

TEMPLATES: dict[str, dict[str, Any]] = {
    "one-agent": {
        "label": "One agent",
        "about": "A single agent answers. The simplest thing that works.",
        "nodes": [
            {"id": "n1", "type": "input", "pos": [40, 120], "widgets": {}},
            {"id": "a1", "type": "agent", "pos": [300, 120],
             "widgets": {"name": "Assistant", "role": "", "model": None, "agent": "auto"}},
            {"id": "o1", "type": "output", "pos": [560, 120], "widgets": {"target": "chat"}}],
        "edges": [{"from": "n1", "out": TASK, "to": "a1", "in": TASK},
                  {"from": "a1", "out": RESULT, "to": "o1", "in": RESULT}],
    },
    "research-and-write": {
        "label": "Research, then write",
        "about": "One agent gathers, another writes it up, with your memory as context.",
        "nodes": [
            {"id": "n1", "type": "input", "pos": [40, 160], "widgets": {}},
            {"id": "m1", "type": "memory", "pos": [280, 40], "widgets": {}},
            {"id": "a1", "type": "agent", "pos": [280, 200],
             "widgets": {"name": "Researcher", "role": "Gather the facts.",
                         "model": None, "agent": "auto"}},
            {"id": "a2", "type": "agent", "pos": [560, 120],
             "widgets": {"name": "Writer", "role": "Write it up clearly.",
                         "model": None, "agent": "auto"}},
            {"id": "o1", "type": "output", "pos": [820, 120], "widgets": {"target": "chat"}}],
        "edges": [{"from": "n1", "out": TASK, "to": "m1", "in": TASK},
                  {"from": "n1", "out": TASK, "to": "a1", "in": TASK},
                  {"from": "m1", "out": CONTEXT, "to": "a1", "in": CONTEXT},
                  # transcript, not result: the writer needs what the
                  # researcher saw, which is the case the opt-in edge is for
                  {"from": "a1", "out": TRANSCRIPT, "to": "a2", "in": TRANSCRIPT},
                  {"from": "a2", "out": RESULT, "to": "o1", "in": RESULT}],
    },
    "two-opinions": {
        "label": "Two opinions, then a verdict",
        "about": "Two agents answer independently and a third reconciles them.",
        "nodes": [
            {"id": "n1", "type": "input", "pos": [40, 160], "widgets": {}},
            {"id": "a1", "type": "agent", "pos": [300, 60],
             "widgets": {"name": "First", "role": "", "model": None, "agent": "auto"}},
            {"id": "a2", "type": "agent", "pos": [300, 260],
             "widgets": {"name": "Second", "role": "", "model": None, "agent": "auto"}},
            {"id": "g1", "type": "aggregator", "pos": [580, 160],
             "widgets": {"synthesize": True}},
            {"id": "o1", "type": "output", "pos": [820, 160], "widgets": {"target": "chat"}}],
        "edges": [{"from": "n1", "out": TASK, "to": "a1", "in": TASK},
                  {"from": "n1", "out": TASK, "to": "a2", "in": TASK},
                  {"from": "a1", "out": RESULT, "to": "g1", "in": RESULT},
                  {"from": "a2", "out": RESULT, "to": "g1", "in": RESULT},
                  {"from": "g1", "out": RESULT, "to": "o1", "in": RESULT}],
    },
}


def templates() -> list[dict[str, Any]]:
    """Starting points, so a first pipeline is a choice rather than a blank page."""
    return [{"id": tid, "label": t["label"], "about": t["about"],
             "nodes": len(t["nodes"])} for tid, t in TEMPLATES.items()]


def from_template(template_id: str, name: str) -> dict[str, Any]:
    tpl = TEMPLATES.get(template_id)
    if tpl is None:
        raise PipelineError(f"unknown template {template_id!r}")
    return {"version": VERSION, "name": name,
            "nodes": json.loads(json.dumps(tpl["nodes"])),
            "edges": json.loads(json.dumps(tpl["edges"]))}

def make_runner(cfg_overrides: dict | None = None):
    """The default way an agent node executes.

    Honours the agent the node resolved to: a named external CLI is delegated
    to, anything else runs on Saturday itself. Without this the router's
    decision and the auto default were both computed and then ignored.

    run() still takes an injected runner, so the engine stays testable without
    spending a call; this is the production default, in one place so the CLI
    and the web app cannot drift into two behaviours."""
    overrides_base = dict(cfg_overrides or {})

    def runner(prompt: str, widgets: dict) -> str:
        from saturday.config import AgentConfig

        agent_id = str(widgets.get("agent") or "").strip()
        if agent_id and agent_id != "auto":
            try:
                from saturday.tools.external_agent import ExternalAgentTool, all_agents

                if agent_id in all_agents():
                    ok, out = ExternalAgentTool().run({"agent": agent_id, "prompt": prompt})
                    if ok:
                        return out
                    # a delegate that is missing or failing must not sink the
                    # run: fall through to Saturday rather than returning error
                    # text as if it were the answer
            except Exception:
                pass
        from saturday.agent.core import Agent

        overrides = dict(overrides_base)
        if widgets.get("model"):
            overrides["model"] = widgets["model"]
        traj = Agent(cfg=AgentConfig.load(overrides), enable_subagents=False).run(prompt)
        return traj.final_answer or f"[no answer; stopped: {traj.stop_reason}]"

    return runner
