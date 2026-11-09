"""Minimal MCP server over stdio (JSON-RPC 2.0, one message per line).

Standard library only, so it runs anywhere Python 3.10+ runs. Works with any
MCP client (opencode, Claude Code, Claude Desktop, Cline, Continue, ...).
"""
from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from .tools import TOOLS, call

SUPPORTED = ["2025-06-18", "2025-03-26", "2024-11-05"]


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion")
            result = {
                "protocolVersion": requested if requested in SUPPORTED else SUPPORTED[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "goae-lookup", "version": __version__},
                "instructions": "GOÄ lookup. Workflow: goae_search -> goae_get -> goae_check. "
                                "Answers are short; ask again rather than for more results.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text = call(p.get("name"), p.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except KeyError as e:
                result = {"content": [{"type": "text", "text": f"unknown tool or missing argument: {e}"}],
                          "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # pragma: no cover - defensive
        traceback.print_exc(file=sys.stderr)
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
