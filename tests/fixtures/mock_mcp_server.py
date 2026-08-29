"""A tiny MCP server for offline tests: newline-delimited JSON-RPC over stdio."""
from __future__ import annotations

import json
import sys


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "1.0"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo back the given text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "add",
                        "description": "Add two integers",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                            "required": ["a", "b"],
                        },
                    },
                ]
            },
        }
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "echo":
            text = str(args.get("text", ""))
            out = {"content": [{"type": "text", "text": f"echo:{text}"}], "isError": False}
        elif name == "add":
            try:
                total = int(args["a"]) + int(args["b"])
                out = {"content": [{"type": "text", "text": str(total)}], "isError": False}
            except (KeyError, TypeError, ValueError):
                out = {"content": [{"type": "text", "text": "bad args"}], "isError": True}
        elif name == "boom":
            out = {"content": [{"type": "text", "text": "intentional failure"}], "isError": True}
        else:
            out = {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        return {"jsonrpc": "2.0", "id": rid, "result": out}
    return None


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
