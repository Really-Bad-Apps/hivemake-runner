"""Codex connector's read-only identity check; never resumes or starts threads.

ACP list/session includes saved history. Native thread/read is used only here
to validate the local connection target; all message delivery remains ACP.
"""

import asyncio
from pathlib import Path

import aiohttp


async def validate_live_thread(socket_path, session_id, workspace):
    async with asyncio.timeout(10):
        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=str(socket_path)),
        ) as client:
            async with client.ws_connect("http://localhost/", max_msg_size=1024 * 1024) as ws:
                result = None
                for request_id, method, params in (
                    (1, "initialize", {
                        "clientInfo": {"name": "hivemake-connector", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    }),
                    (2, "thread/read", {"threadId": session_id, "includeTurns": False}),
                ):
                    await ws.send_json({"jsonrpc": "2.0", "id": request_id,
                                        "method": method, "params": params})
                    while True:
                        message = await ws.receive_json()
                        if message.get("id") == request_id:
                            break
                    if "error" in message:
                        raise ValueError("terminal_not_live")
                    result = message.get("result", {})
                thread = result.get("thread", {})
                # A real CLI attached to the shared server can report vscode.
                # Source is provenance, not a reliable label for its current UI.
                # Require a loaded root thread accepting direct input as well.
                if (
                    thread.get("id") != session_id
                    or thread.get("status", {}).get("type") not in ("idle", "active")
                    or thread.get("source") not in ("cli", "vscode", "appServer")
                    or thread.get("parentThreadId") is not None
                    or thread.get("canAcceptDirectInput") is not True
                ):
                    raise ValueError("terminal_not_live")
                if Path(thread.get("cwd", "")).resolve() != Path(workspace).resolve():
                    raise ValueError("terminal_workspace_mismatch")
