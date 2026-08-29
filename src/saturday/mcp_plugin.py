from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from saturday.mcp_client import McpError, McpHttpClient, McpStdioClient, McpToolDef
from saturday.plugins import Plugin
from saturday.tools.base import Tool, ToolRegistry


class McpToolProxy(Tool):
    """Bridges a remote MCP tool into the native Saturday tool registry."""

    def __init__(self, client, tool: McpToolDef) -> None:
        self._client = client
        self.name = tool.name
        self.description = tool.description or f"MCP tool {tool.name}"
        params = tool.input_schema or {"type": "object", "properties": {}}
        self.parameters = params
        # all proxies on one client share a single restart gate: two threads
        # must not both see a dead transport and respawn duplicate servers
        lock = getattr(client, "_proxy_restart_lock", None)
        if lock is None:
            lock = threading.Lock()
            try:
                client._proxy_restart_lock = lock
            except AttributeError:
                pass
        self._restart_lock = lock

    def run(self, args: dict) -> tuple[bool, str]:
        try:
            if getattr(self._client, "_dead", False):
                with self._restart_lock:
                    if getattr(self._client, "_dead", False):
                        self._client.start()
            return self._client.call_tool(self.name, args)
        except Exception as exc:
            return False, f"mcp transport error: {type(exc).__name__}: {exc}"


def load_mcp_config(
    path: str | Path | None = None, warnings: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Load MCP server specs. An explicit ``path`` is user-directed and always
    trusted; the implicit CWD ``.saturday/mcp.json`` is honored only after the
    project has been trusted (it names local commands Saturday would spawn)."""
    from saturday.utils.trust import ensure_trusted

    explicit = path is not None
    candidates = [Path(path)] if explicit else [Path(".saturday/mcp.json")]
    for candidate in candidates:
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                if warnings is not None:
                    warnings.append(f"mcp config unreadable ({candidate}): {type(exc).__name__}: {exc}")
                return {}
            if not isinstance(data, dict):
                if warnings is not None:
                    warnings.append(f"mcp config ignored ({candidate}): top level must be an object")
                return {}
            raw = data.get("servers") if isinstance(data.get("servers"), dict) else data
            out: dict[str, dict[str, Any]] = {}
            for key, spec in raw.items():
                if isinstance(spec, dict) and (spec.get("command") or spec.get("url")):
                    out[key] = spec
                elif warnings is not None:
                    warnings.append(f"mcp config: skipping invalid server entry {key!r} (need 'command' or 'url')")
            if not explicit and out:
                root = candidate.parent.parent
                detail = []
                for alias, spec in out.items():
                    # URL-only servers have no 'command'; formatting it
                    # unconditionally crashed the prompt and silently
                    # disabled ALL of MCP via config.py's broad except
                    if "command" in spec:
                        entry = (
                            f"{alias}: {spec['command']} "
                            + " ".join(str(a) for a in (spec.get("args") or []))
                        ).strip()
                    elif "url" in spec:
                        entry = f"{alias}: {spec['url']}"
                    else:
                        entry = f"{alias}: (invalid entry)"
                    detail.append(entry)
                if not ensure_trusted(root, what=f"MCP servers from {candidate}", detail=detail):
                    if warnings is not None:
                        warnings.append(
                            f"mcp config ignored ({candidate}): project not trusted "
                            "(approve interactively once, or set SATURDAY_TRUST_ALL_PROJECTS=1)"
                        )
                    return {}
            return out
    return {}


def build_mcp_plugin(
    servers: dict[str, dict[str, Any]],
    *,
    on_warning=None,
    call_timeout: float = 60.0,
) -> Plugin:
    tools: list[Tool] = []
    started_clients: list[Any] = []
    connected: list[str] = []
    failed: list[str] = []

    for alias, spec in (servers or {}).items():
        if spec.get("url"):
            client = McpHttpClient(
                url=str(spec["url"]),
                headers=spec.get("headers"),
                call_timeout=call_timeout,
            )
        else:
            command = [spec["command"]] + [str(a) for a in (spec.get("args") or [])]
            client = McpStdioClient(command=command, env=spec.get("env"), call_timeout=call_timeout)
        try:
            client.start()
            defs = client.list_tools()
        except McpError as exc:
            client.close()
            failed.append(f"{alias}: {exc}")
            if on_warning:
                on_warning(f"MCP server '{alias}' unavailable: {exc}")
            continue

        # the plugin owns successfully-started transports; dropping them here
        # used to orphan the spawned server processes until reboot
        started_clients.append(client)
        for tool_def in defs:
            tools.append(McpToolProxy(client, tool_def))
        connected.append(f"{alias} ({len(defs)} tools)")

    def register(registry: ToolRegistry) -> None:
        existing = set(registry.names())
        for proxy in tools:
            final_name = proxy.name
            if final_name in existing:
                candidate = f"mcp_{final_name}"
                while candidate in existing:
                    candidate = f"mcp_{candidate}"
                proxy.description = f"{proxy.description} (aliased from '{final_name}')"
                final_name = candidate
            proxy.name = final_name
            existing.add(final_name)
            registry.register(proxy)

    persona: list[str] = [
        f"# MCP servers\nConnected: {', '.join(connected)}." if connected else "# MCP servers\nNone connected.",
        "# MCP tools\nRemote MCP tools behave like local tools; on name collisions they are aliased with an 'mcp_' prefix.",
    ]

    plugin = Plugin(
        name="mcp",
        description="Model Context Protocol servers bridged as local tools",
        tools=tools,
        register_fn=register,
        persona_sections=persona,
        clients=started_clients,
    )
    plugin.warnings = failed
    return plugin


__all__ = ["McpToolProxy", "build_mcp_plugin", "load_mcp_config"]
