"""Language Server Protocol client + tools (zero-dep, stdio transport).

Config: AgentConfig.lsp_servers maps language -> launch command, e.g.
    {"python": ["pylsp"], "typescript": ["typescript-language-server", "--stdio"]}
Servers start lazily per language and are shared process-wide. When no server
is configured/available the tools return actionable messages instead of
failing - LSP is an enhancement, never a requirement.

Framing: LSP JSON-RPC over stdio with Content-Length headers. The client
transport is injectable (read_all/write callables) so tests run offline.
"""
from __future__ import annotations

import atexit
import json
import subprocess
import threading
import time
from pathlib import Path


class LspError(RuntimeError):
    pass


class ProcTransport:
    """Subprocess-based byte transport."""

    def __init__(self, cmd: list[str], cwd: str | None = None) -> None:
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd or None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def write(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def read(self, n: int) -> bytes:
        assert self.proc.stdout is not None
        return self.proc.stdout.read(n)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass


class LspClient:
    def __init__(self, transport, root_uri: str, timeout_s: float = 10.0) -> None:
        self.transport = transport
        self.root_uri = root_uri
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._pending_diag_uris: set[str] = set()
        self._diagnostics_by_uri: dict[str, list] = {}
        self._initialized = False

    # -- framing -----------------------------------------------------------------
    def _send(self, method: str, params: dict | None = None, notify: bool = False) -> int | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        rid = None
        if not notify:
            rid = next(self._ids)
            msg["id"] = rid
        if params is not None:
            msg["params"] = params
        body = json.dumps(msg).encode("utf-8")
        self.transport.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        return rid

    _ids = iter(range(1, 1_000_000))

    def _read_message(self) -> dict:
        headers: dict[str, str] = {}
        while True:
            line = b""
            while not line.endswith(b"\r\n"):
                chunk = self.transport.read(1)
                if not chunk:
                    raise LspError("LSP server closed the stream")
                line += chunk
                if len(line) > 4096:
                    raise LspError("LSP header line too long")
            sline = line.decode("ascii", errors="replace").strip()
            if not sline:
                break
            if ":" in sline:
                k, v = sline.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0 or length > 32 * 1024 * 1024:
            raise LspError(f"bad LSP content-length {length}")
        body = b""
        while len(body) < length:
            chunk = self.transport.read(length - len(body))
            if not chunk:
                raise LspError("LSP stream ended mid-body")
            body += chunk
        return json.loads(body.decode("utf-8"))

    def _request(self, method: str, params: dict | None = None) -> dict:
        rid = self._send(method, params)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            msg = self._read_message()
            if msg.get("id") == rid:
                if "error" in msg:
                    raise LspError(f"LSP error: {msg['error']}")
                return msg.get("result") or {}
            if msg.get("method") == "textDocument/publishDiagnostics":
                params_d = msg.get("params") or {}
                uri = str(params_d.get("uri") or "")
                self._diagnostics_by_uri[uri] = list(params_d.get("diagnostics") or [])
            elif msg.get("method") == "window/logMessage":
                pass  # ignore server chatter
        raise LspError(f"LSP request timed out: {method}")

    # -- lifecycle ---------------------------------------------------------------
    def initialize(self) -> None:
        if self._initialized:
            return
        self._request("initialize", {
            "processId": None,
            "rootUri": self.root_uri,
            "capabilities": {},
        })
        self._send("initialized", {}, notify=True)
        self._initialized = True

    @staticmethod
    def _to_uri(path: str) -> str:
        p = str(path).replace("\\", "/")
        if not p.startswith("file:"):
            p = "file:///" + p.lstrip("/")
        return p

    def did_open(self, path: str, text: str, language_id: str = "python") -> str:
        uri = self._to_uri(path)
        self._send("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": language_id, "version": 1, "text": text},
        }, notify=True)
        self._pending_diag_uris.add(uri)
        return uri

    def diagnostics_for(self, path: str, wait_s: float = 3.0) -> list[dict]:
        """Diagnostics last published for this uri (publishes arrive async)."""
        uri = self._to_uri(path)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and uri in self._pending_diag_uris:
            try:
                msg = self._read_message()
            except LspError:
                break
            if msg.get("method") == "textDocument/publishDiagnostics":
                p = msg.get("params") or {}
                u = str(p.get("uri") or "")
                self._diagnostics_by_uri[u] = list(p.get("diagnostics") or [])
                self._pending_diag_uris.discard(u)
        return self._diagnostics_by_uri.get(uri, [])

    def definition(self, path: str, line: int, column: int) -> list[dict]:
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": self._to_uri(path)},
            "position": {"line": max(0, int(line)), "character": max(0, int(column))},
        })
        if isinstance(result, dict):
            result = [result]
        out = []
        for loc in result or []:
            luri = str((loc.get("uri") or "").replace("file:///", "").replace("file://", ""))
            rng = loc.get("range") or {}
            start = rng.get("start") or {}
            out.append({"path": luri, "line": int(start.get("line", 0)) + 1,
                        "column": int(start.get("character", 0))})
        return out

    def close(self) -> None:
        self.transport.close()


# -- registry of shared clients ----------------------------------------------

_clients: dict[str, LspClient] = {}
_lock = threading.Lock()


def close_all_clients() -> None:
    """Best-effort shutdown of every live language server at exit.

    Servers normally die on stdin EOF when the harness exits; this makes the
    cleanup explicit and immediate (same contract as mcp_client's atexit net)
    instead of relying on pipe-EOF behavior differences across platforms."""
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(close_all_clients)


def _resolve_in_root(raw: str, root: str | None) -> tuple[Path | None, str]:
    """Resolve a model-supplied path against the workspace root, or refuse it.

    Returns (path, "") on success and (None, error) when the target escapes
    the workspace - handlers must not read arbitrary files outside it."""
    base = Path(root or ".").resolve()
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    try:
        p = p.resolve()
    except (OSError, ValueError) as exc:
        return None, f"bad path {raw!r}: {exc}"
    if p != base and base not in p.parents:
        return None, f"path escapes workspace root: {raw}"
    return p, ""


def get_client(language: str, cmd: list[str], workspace_root: str) -> LspClient | None:
    key = f"{language}:{workspace_root}"
    with _lock:
        existing = _clients.get(key)
        if existing is not None and existing.transport.alive():
            return existing
    from pathlib import Path

    root_uri = Path(workspace_root).resolve().as_uri()
    client = LspClient(ProcTransport(cmd, cwd=workspace_root), root_uri)
    try:
        client.initialize()
    except Exception:
        # WHY: a failed/hung initialize must not leak a server process per
        # attempt - closing the transport kills the child
        client.close()
        return None
    with _lock:
        # WHY: double-check under the lock so two threads that raced past the
        # first check don't spawn two servers for the same key
        existing = _clients.get(key)
        if existing is not None and existing.transport.alive():
            client.close()  # lost the race; drop our duplicate
            return existing
        _clients[key] = client
    return client


def make_lsp_tools(lsp_servers_cfg: dict[str, list[str]], workspace_root_fn):
    """Build lsp_diagnostics / lsp_definition tools when servers configured."""
    tools = []

    def resolve(language: str):
        cmd = lsp_servers_cfg.get(language)
        if not cmd:
            return None, (
                f"no LSP server configured for '{language}'. Add to config: "
                'lsp_servers = {"' + language + '": ["<server>", "--stdio"]}'
            )
        client = get_client(language, cmd, workspace_root_fn())
        if client is None:
            return None, f"could not start LSP server for {language}: {cmd} (installed?)"
        return client, None

    class LspDiagnosticsTool:
        name = "lsp_diagnostics"
        description = "Language-server diagnostics (errors/warnings) for a file. language defaults to python."
        parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "language": {"type": "string"},
            },
            "required": ["path"],
        }

        def run(self, args: dict) -> tuple[bool, str]:
            language = str(args.get("language") or "python")
            client, err = resolve(language)
            if client is None:
                return False, err
            path, perr = _resolve_in_root(str(args.get("path") or ""), workspace_root_fn())
            if perr:
                return False, perr
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return False, f"{type(exc).__name__}: {exc}"
            client.did_open(str(path), text, language_id=language)
            diags = client.diagnostics_for(str(path))
            if not diags:
                return True, "(no diagnostics)"
            out = []
            for d in diags[:50]:
                rng = d.get("range", {}).get("start", {})
                out.append(
                    f"{d.get('severity', '?')}:{int(rng.get('line', 0)) + 1}:"
                    f"{d.get('message', '')[:200]}"
                )
            return True, "\n".join(out)

    class LspDefinitionTool:
        name = "lsp_definition"
        description = "Go-to-definition via the language server. args: path, line (0-based), column."
        parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line": {"type": "integer"},
                "column": {"type": "integer"},
                "language": {"type": "string"},
            },
            "required": ["path", "line", "column"],
        }

        def run(self, args: dict) -> tuple[bool, str]:
            language = str(args.get("language") or "python")
            client, err = resolve(language)
            if client is None:
                return False, err
            path, perr = _resolve_in_root(str(args.get("path") or ""), workspace_root_fn())
            if perr:
                return False, perr
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                client.did_open(str(path), text, language_id=language)
                locs = client.definition(str(path), int(args.get("line") or 0), int(args.get("column") or 0))
            except Exception as exc:
                return False, f"{type(exc).__name__}: {exc}"
            if not locs:
                return True, "(no definition found)"
            return True, "\n".join(f"{loc['path']}:{loc['line']}:{loc['column']}" for loc in locs)

    tools.extend([LspDiagnosticsTool(), LspDefinitionTool()])
    return tools
