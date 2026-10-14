"""
Jarvis's tools as a stdio MCP server, so the Claude Code brain can call them.

Hand-rolled JSON-RPC 2.0, one message per line, no extra dependency. stdout is the
protocol channel, so anything a tool prints is redirected to stderr while it runs.

    python -m jarvis_v2.mcp_server       (started by Claude Code from data/mcp.json)
"""

import contextlib
import json
import sys

from . import tools

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "jarvis", "version": "2.0"}


def handle(msg):
    """One JSON-RPC message in, the response dict out (None for notifications)."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notifications (e.g. notifications/initialized) get no reply
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION,
                  "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in tools.specs()]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        with contextlib.redirect_stdout(sys.stderr):
            text = tools.call(name, params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": name not in tools.TOOLS}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                reply = handle(msg)
            except Exception as error:  # never let one bad call kill the server
                reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(error)}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    main()
