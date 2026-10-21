"""Local stdio MCP fixture; never configure this diagnostic in a real project."""

import json
import os
import sys
import time
import uuid
from pathlib import Path


def audit(path, event):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as stream:
        stream.write(json.dumps(event) + "\n")


def respond(request, audit_path):
    method = request["method"]
    if method == "initialize":
        return {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "connector-context-probe", "version": "1"},
        }
    if method == "tools/list":
        return {"tools": [{
            "name": "connector_context",
            "description": "Read-only diagnostic: return a fresh marker after an optional brief wait.",
            "inputSchema": {
                "type": "object", "required": ["stage"],
                "properties": {
                    "stage": {"type": "string"},
                    "delay": {"type": "integer", "minimum": 0, "maximum": 20},
                },
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        }]}
    if method == "tools/call":
        params = request["params"]
        args = params["arguments"]
        delay = args.get("delay", 0)
        if (params["name"] != "connector_context" or not isinstance(args.get("stage"), str)
                or type(delay) is not int or not 0 <= delay <= 20):
            raise ValueError("invalid diagnostic arguments")
        entry = {"stage": args["stage"], "token": "MCP_" + uuid.uuid4().hex[:12]}
        audit(audit_path, dict(entry, event="start", time=time.time()))
        time.sleep(delay)
        audit(audit_path, dict(entry, event="end", time=time.time()))
        return {"content": [{"type": "text", "text": entry["token"]}]}
    if method == "ping":
        return {}
    raise ValueError("unsupported diagnostic method")


def main():
    audit_path = Path(sys.argv[1])
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = respond(request, audit_path)
        except (KeyError, TypeError, ValueError):
            response["error"] = {"code": -32602, "message": "Invalid diagnostic request"}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
