"""Saturday as an MCP *server*: the other half of `mcp_client.py`.

Speaks JSON-RPC 2.0 over stdio, newline delimited, deliberately reusing
`mcp_client.PROTOCOL_VERSION` so the two halves can never drift apart.
Any MCP speaking client (Claude Code, Cursor, Codex, an editor with an MCP
panel) can spawn `saturday mcp-serve` and delegate work in.

The protocol layer here is pure: `McpServer.handle` maps one request dict
to one response dict, so it is testable without a subprocess, a socket or
a model. `serve_stdio` is the only part that touches real I/O.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from saturday.mcp_client import PROTOCOL_VERSION

SERVER_NAME = "saturday"

# JSON-RPC 2.0 error codes (spec section 5.1).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass
class ExposedTool:
    """One tool this server advertises over MCP.

    ``handler`` returns ``(ok, text)`` — the same shape ``Tool.run`` uses,
    so registry tools can be adapted with no translation layer.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    handler: Callable[[dict[str, Any]], tuple[bool, str]] = lambda args: (False, "not implemented")


def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _text_content(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpServer:
    """Stateless request handler for the subset of MCP Saturday implements.

    Tools only. `resources/*` and `prompts/*` are answered with
    METHOD_NOT_FOUND rather than an empty list, because claiming a
    capability the initialize response never advertised is worse for a
    client than a clean "no".
    """

    def __init__(self, tools: list[ExposedTool] | None = None, version: str | None = None) -> None:
        self.tools = list(tools or [])
        if version is None:
            from saturday import __version__

            version = __version__
        self.version = version
        self.initialized = False

    def _by_name(self, name: str) -> ExposedTool | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Map one JSON-RPC message to one response, or None for a notification.

        A notification (no ``id``) MUST NOT get a response, even an error
        one; a client that sees a reply to a notification it never keyed
        has no way to correlate it.
        """
        if not isinstance(msg, dict):
            return _error(None, INVALID_REQUEST, "request must be a JSON object")
        rid = msg.get("id")
        is_notification = "id" not in msg
        method = msg.get("method")
        if not isinstance(method, str) or not method:
            return None if is_notification else _error(rid, INVALID_REQUEST, "missing method")
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None if is_notification else _error(rid, INVALID_PARAMS, "params must be an object")

        if is_notification:
            # notifications/initialized is the only one that matters; the rest
            # are ignored on purpose rather than erroring back into the void.
            if method == "notifications/initialized":
                self.initialized = True
            return None

        try:
            return self._dispatch(rid, method, params)
        except Exception as exc:  # a tool bug must not take the stream down
            return _error(rid, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _dispatch(self, rid: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return _result(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": self.version},
            })
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self.tools
            ]})
        if method == "tools/call":
            return self._call_tool(rid, params)
        return _error(rid, METHOD_NOT_FOUND, f"method not found: {method}")

    def _call_tool(self, rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(rid, INVALID_PARAMS, "tools/call requires a 'name'")
        tool = self._by_name(name)
        if tool is None:
            return _error(rid, INVALID_PARAMS, f"unknown tool '{name}'")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _error(rid, INVALID_PARAMS, "'arguments' must be an object")
        try:
            ok, text = tool.handler(args)
        except Exception as exc:
            # A failing tool is a tool *result* with isError, not a protocol
            # error: the client should show it to the model, not to a log.
            return _result(rid, _text_content(f"{type(exc).__name__}: {exc}", is_error=True))
        return _result(rid, _text_content(text if isinstance(text, str) else str(text), is_error=not ok))


def serve_stdio(server: McpServer, stdin=None, stdout=None) -> int:
    """Read framed requests until EOF. Returns a process exit code.

    stdout is the protocol channel, so the real handle is captured here and
    `sys.stdout` is rebound to stderr for the rest of the process: any tool
    or plugin that prints (they all assume a CLI) would otherwise inject
    plain text into the JSON-RPC stream, which surfaces to the user as an
    unexplained parse error on the client side. Redirected output shows up
    in the client's MCP server log, which is where a human would look.
    """
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    # captured BEFORE the rebind below, so frames keep going to the real
    # handle while everything else in the process is diverted to stderr
    prev_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(out, _error(None, PARSE_ERROR, f"invalid JSON: {exc}"))
                continue
            if isinstance(msg, list):
                # Batches are valid JSON-RPC but were removed in MCP 2025-06-18;
                # say so instead of silently handling half of one.
                _write(out, _error(None, INVALID_REQUEST, "batch requests are not supported"))
                continue
            response = server.handle(msg)
            if response is not None:
                _write(out, response)
    except KeyboardInterrupt:
        return 130
    finally:
        sys.stdout = prev_stdout
    return 0


def _write(out, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload) + "\n")
    out.flush()


# -- what Saturday exposes --------------------------------------------------
#
# Two layers, selected by `expose`:
#   "agent"  delegate a whole task to Saturday's loop (the default)
#   "tools"  hand the caller Saturday's own tool registry, raw
#   "all"    both
#
# `agent` is the default because raw passthrough puts `shell`, `write_file`
# and `python` on the host in the hands of whatever spawned this process.
# That is a legitimate thing to want, but not a thing to turn on silently.

RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "what Saturday should do"},
        "session": {"type": "string", "description": "resume/continue this session id"},
        "max_steps": {"type": "integer", "description": "cap on agent steps for this run"},
    },
    "required": ["task"],
}

SESSIONS_SCHEMA = {
    "type": "object",
    "properties": {"limit": {"type": "integer", "description": "how many recent sessions (default 20)"}},
}


def _run_task(args: dict[str, Any], cfg_overrides: dict[str, Any] | None, read_only: bool) -> tuple[bool, str]:
    task = str(args.get("task") or "").strip()
    if not task:
        return False, "a non-empty 'task' is required"
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig

    overrides = dict(cfg_overrides or {})
    if args.get("max_steps") is not None:
        overrides["max_steps"] = int(args["max_steps"])
    if read_only:
        # reuse plan mode rather than inventing a second read-only concept
        overrides["plan_mode"] = True
    session_id = str(args.get("session") or "") or None
    seen: dict[str, str] = {}
    agent = Agent(cfg=AgentConfig.load(overrides))
    traj = agent.run(task, session_id=session_id, on_session_id=lambda sid: seen.update(session=sid))
    sid = seen.get("session", session_id or "?")
    status = f"[session {sid} · {len(traj.steps)} steps · stop={traj.stop_reason}]"
    if traj.final_answer:
        return True, f"{traj.final_answer}\n\n{status}"
    return False, f"no answer produced\n\n{status}"


def _list_sessions(args: dict[str, Any]) -> tuple[bool, str]:
    from saturday.sessions import SessionStore

    limit = int(args.get("limit") or 20)
    rows = SessionStore().list_sessions(limit=limit)
    if not rows:
        return True, "no sessions yet"
    return True, "\n".join(f"{r['id']}  {r.get('task', '')}" for r in rows)


def agent_tools(cfg_overrides: dict[str, Any] | None = None, read_only: bool = False) -> list[ExposedTool]:
    """Delegation surface: hand Saturday a task, get the answer back.

    The agent is built inside the handler, not here, so `initialize` and
    `tools/list` still work on a machine with no provider key configured.
    A client that cannot even list tools looks broken; one that fails on
    call with a config error is self explanatory.
    """
    return [
        ExposedTool(
            name="saturday_run",
            description=(
                "Delegate a task to the Saturday agent harness and return its final answer. "
                "Runs a full tool-using agent loop (shell, files, web, computer use) in Saturday's workspace."
                + (" Read-only: plan mode, no world mutation." if read_only else "")
            ),
            input_schema=RUN_SCHEMA,
            handler=lambda args: _run_task(args, cfg_overrides, read_only),
        ),
        ExposedTool(
            name="saturday_sessions",
            description="List recent Saturday sessions (id and task), newest first.",
            input_schema=SESSIONS_SCHEMA,
            handler=_list_sessions,
        ),
    ]


def registry_tools(cfg_overrides: dict[str, Any] | None = None, read_only: bool = False) -> list[ExposedTool]:
    """Raw passthrough of Saturday's own tools.

    `read_only` filters to `ToolRegistry.READ_ONLY_TOOLS`, the allowlist plan
    mode already uses, so there is one vetted list instead of two that drift.
    """
    from saturday.config import AgentConfig
    from saturday.tools import default_registry
    from saturday.tools.base import ToolRegistry

    try:
        cfg = AgentConfig.load(cfg_overrides or {})
    except ValueError:
        # tool listing must survive an unconfigured provider; the tools
        # themselves do not need one
        cfg = None
    reg = default_registry(cfg)
    if read_only:
        reg = reg.filtered(ToolRegistry.READ_ONLY_TOOLS)
    # ask_user has no surface to ask through here, so it would always answer
    # "no user surface available" - a tool slot spent on a reply that can read
    # to a model as though the human was consulted. The calling client has its
    # own user; let it ask them.
    reg = reg.excluding({"ask_user"})

    def make(name: str):
        def handler(args: dict[str, Any]) -> tuple[bool, str]:
            result = reg.execute(f"mcp-{name}", name, args)
            return result.ok, (result.output if result.ok else (result.error or result.output or "tool failed"))

        return handler

    out: list[ExposedTool] = []
    for spec in reg.specs():
        name = str(spec.get("name") or "")
        if not name:
            continue
        out.append(ExposedTool(
            name=name,
            description=str(spec.get("description") or ""),
            input_schema=spec.get("parameters") or {"type": "object", "properties": {}},
            handler=make(name),
        ))
    return out


def build_server(expose: str = "agent", read_only: bool = False, cfg_overrides: dict[str, Any] | None = None) -> McpServer:
    tools: list[ExposedTool] = []
    if expose in ("agent", "all"):
        tools.extend(agent_tools(cfg_overrides, read_only))
    if expose in ("tools", "all"):
        tools.extend(registry_tools(cfg_overrides, read_only))
    if not tools:
        raise ValueError(f"unknown expose mode '{expose}' (want agent, tools or all)")
    return McpServer(tools)
