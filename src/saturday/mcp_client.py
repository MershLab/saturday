from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


def _interpolate_env(value: str) -> str:
    """Expand ${VAR} references in header/URL values from the environment."""
    import re

    def sub(m: "re.Match[str]") -> str:
        return os.environ.get(m.group(1), "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, value)


class McpHttpClient:
    """Minimal MCP client over streamable HTTP (JSON-RPC POST).

    Responses may be a single JSON body OR an SSE stream; both are parsed.
    Auth is header-based (bearer/PAT) with ${VAR} interpolation - covers
    hosted MCP gateways. Full OAuth dance is out of scope by design.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None, call_timeout: float = 60.0) -> None:
        self.url = url
        self.headers = {k: _interpolate_env(v) for k, v in (headers or {}).items()}
        self.call_timeout = call_timeout
        self.server_info: dict[str, Any] = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self.session_headers: dict[str, str] = {}

    # -- transport ---------------------------------------------------------------
    def _post(self, payload: dict, notify: bool = False) -> dict:
        import urllib.error
        import urllib.request

        body = json.dumps(payload).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.session_headers,
            **self.headers,
        }
        req = urllib.request.Request(self.url, data=body, headers=req_headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.call_timeout)
        except urllib.error.HTTPError as exc:
            raise McpError(f"MCP HTTP error {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            mcp_session = resp.headers.get("Mcp-Session-Id")
            if mcp_session:
                self.session_headers["Mcp-Session-Id"] = mcp_session
            raw = resp.read().decode("utf-8", errors="replace")
        if notify and (resp.status == 202 or not raw.strip()):
            return {}
        if not raw.strip():
            # an empty 2xx body for a real request is a wedged gateway, not a
            # successful result; returning {} here faked success upstream
            raise McpError("empty response body")
        if "text/event-stream" in ctype:
            events: list[list[str]] = []
            data_lines: list[str] = []
            for line in raw.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif not line.strip() and data_lines:
                    events.append(data_lines)
                    data_lines = []
            if data_lines:
                events.append(data_lines)
            for chunk in events:
                # SSE joins consecutive data fields of one event with newlines
                candidate = "\n".join(chunk).strip()
                if candidate and candidate != "[DONE]":
                    try:
                        evt = json.loads(candidate)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("id") == payload.get("id"):
                        return evt
            raise McpError("MCP SSE stream carried no response for the request id")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McpError(f"MCP returned non-JSON body: {raw[:200]}") from exc

    def start(self) -> dict[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "saturday", "version": "0.6"},
        })
        self.server_info = result.get("serverInfo", {})
        self._notify("notifications/initialized")
        if not any(c is self for c in _LIVE_CLIENTS):
            _LIVE_CLIENTS.append(self)
        return self.server_info

    def _send(self, obj: dict, notify: bool = False) -> None:
        self._post(obj, notify=notify)

    def _read_response(self, want_id: int) -> dict:  # parity with stdio API shape
        raise McpError("internal: use start/call_tool")

    def _request(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params:
                msg["params"] = params
            resp = self._post(msg)
        if "error" in resp:
            raise McpError(f"MCP error: {resp['error']}")
        return resp.get("result", {})

    def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        with self._lock:
            self._post(msg, notify=True)

    def list_tools(self) -> list[McpToolDef]:
        result = self._request("tools/list", {})
        out: list[McpToolDef] = []
        for t in result.get("tools") or []:
            out.append(McpToolDef(
                name=str(t.get("name") or ""),
                description=str(t.get("description") or ""),
                input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
            ))
        return out

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content") or []
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
        text = "\n".join(parts)
        if result.get("isError"):
            return False, text or "tool reported an error"
        return True, text

    def close(self) -> None:  # stateless transport
        pass

    def __enter__(self) -> "McpHttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class McpStdioClient:
    """Minimal MCP client over stdio (JSON-RPC 2.0, newline-delimited)."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None, call_timeout: float = 60.0) -> None:
        self.command = command
        self.env = env
        self.call_timeout = call_timeout
        self.proc: subprocess.Popen | None = None
        self.server_info: dict[str, Any] = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._dead = False

    def start(self) -> dict[str, Any]:
        # one gate around spawn+handshake: two threads racing a dead transport
        # must not both respawn and leak one server process
        with self._spawn_lock:
            if self.proc is not None and self.proc.poll() is None:
                return self.server_info
            self._dead = False
            command = list(self.command)
            if command and command[0].lower().endswith(".py"):
                command.insert(0, sys.executable)
            elif command:
                # CreateProcess cannot exec .cmd shims (npx/uvx) by bare name;
                # resolve via PATH whenever there is no explicit directory separator
                if os.name == "nt" or not os.path.dirname(command[0]):
                    resolved = shutil.which(command[0])
                    if resolved:
                        command[0] = resolved
            try:
                merged_env = dict(os.environ)
                if self.env:
                    merged_env.update(self.env)
                self.proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    env=merged_env,
                )
            except OSError as exc:
                raise McpError(f"cannot spawn MCP server {command}: {exc}") from exc
            result = self._request_with_timeout("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "saturday", "version": "0.3.0"},
            })
            self.server_info = result.get("serverInfo", {})
            self._notify("notifications/initialized")
            if not any(c is self for c in _LIVE_CLIENTS):
                _LIVE_CLIENTS.append(self)
            return self.server_info

    def _send(self, obj: dict[str, Any]) -> None:
        if self._dead:
            raise McpError("MCP client is dead after a previous timeout; call start() to restart")
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"MCP server pipe broken: {exc}") from exc

    def _read_response(self, want_id: int) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                code = self.proc.poll()
                raise McpError(f"MCP server exited (code={code}) while awaiting response")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                if "error" in msg:
                    err = msg["error"]
                    raise McpError(f"MCP error {err.get('code')}: {err.get('message')}")
                return msg.get("result", {})

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            req: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                req["params"] = params
            self._send(req)
            return self._read_response(rid)

    def _request_with_timeout(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Handshake/list use the same bounded-executor pattern as call_tool:
        a wedged server must fail fast instead of freezing agent boot forever."""
        pool = ThreadPoolExecutor(max_workers=1)

        def invoke():
            return self._request(method, params)

        try:
            return pool.submit(invoke).result(timeout=self.call_timeout)
        except FutTimeout:
            self._kill()
            raise McpError(
                f"timed out waiting for '{method}' response after {self.call_timeout}s; "
                "server killed and will restart on next call"
            ) from None
        finally:
            pool.shutdown(wait=False)

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        note: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            note["params"] = params
        with self._lock:
            self._send(note)

    def list_tools(self) -> list[McpToolDef]:
        result = self._request_with_timeout("tools/list", {})
        tools = []
        for t in result.get("tools") or []:
            tools.append(
                McpToolDef(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
                )
            )
        return [t for t in tools if t.name]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        pool = ThreadPoolExecutor(max_workers=1)

        def invoke():
            return self._request("tools/call", {"name": name, "arguments": arguments})

        try:
            future = pool.submit(invoke)
            result = future.result(timeout=self.call_timeout)
        except FutTimeout:
            self._kill()
            pool.shutdown(wait=False, cancel_futures=True)
            return False, f"MCP tool '{name}' timed out after {self.call_timeout}s; server killed and will restart on next call"
        except McpError as exc:
            pool.shutdown(wait=False)
            return False, str(exc)
        finally:
            pool.shutdown(wait=False)

        parts = []
        for c in result.get("content") or []:
            ctype = c.get("type")
            if ctype == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(f"[{ctype or 'unknown'} content]")
        output = "\n".join(p for p in parts if p) or "(empty result)"
        if result.get("isError"):
            return False, output
        return True, output

    def _kill(self) -> None:
        """Release the protocol after a hang so the lock frees; next start() respawns."""
        self._dead = True
        if self.proc is not None and self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            self.proc.kill()
            self.proc.wait(timeout=5)

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()

    def __enter__(self) -> "McpStdioClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# Exit-time safety net: lifecycle owners (e.g. the MCP plugin) normally close
# their clients, but dropped references used to orphan server processes.
_LIVE_CLIENTS: list = []


def close_all_live_clients() -> None:
    """Idempotent best-effort shutdown of every live MCP client."""
    while _LIVE_CLIENTS:
        try:
            _LIVE_CLIENTS.pop().close()
        except Exception:
            pass


atexit.register(close_all_live_clients)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - debug REPL
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) < 1:
        print("usage: python -m saturday.mcp_client <command...> [args...]")
        return 2
    client = McpStdioClient(argv)
    info = client.start()
    print("server:", json.dumps(info))
    for t in client.list_tools():
        print("-", t.name, "::", t.description[:80])
    client.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
