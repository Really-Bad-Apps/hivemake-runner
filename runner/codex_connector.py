"""Transport-only connection to an existing Codex server; never starts Codex.

Used as codex-acp's CODEX_PATH executable. The adapter speaks JSONL on stdio;
the existing server speaks WebSocket messages on a local Unix socket.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class ConnectorError(Exception):
    """A connection failure whose details must not expose message contents."""


def frame(text):
    """Normalize framing only, without interpreting or rewriting RPC fields."""
    def invalid_constant(_value):
        raise ValueError("non-JSON number")

    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        value = json.loads(text, parse_float=str, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise ConnectorError("invalid JSON message") from exc
    if not isinstance(value, dict):
        raise ConnectorError("expected a JSON object")
    # Valid JSON cannot contain literal CR/LF inside strings. Remove only these
    # framing characters; parsing and reserializing would round decimal values.
    return text.replace("\r", "").replace("\n", "")


async def relay(reader, writer, socket_path, *, max_message_bytes=MAX_MESSAGE_BYTES,
                handshake_timeout=10):
    """Disconnect on either EOF; never cancel a turn or close a shared session."""
    connector = aiohttp.UnixConnector(path=str(socket_path))
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as client:
        # sock_connect bounds socket establishment, not the HTTP upgrade read.
        async with asyncio.timeout(handshake_timeout):
            ws = await client.ws_connect(
                "http://localhost/", max_msg_size=max_message_bytes, compress=0,
            )
        async with ws:
            async def upstream():
                while True:
                    try:
                        line = await reader.readline()
                    except ValueError as exc:
                        raise ConnectorError("stdio message exceeds size limit") from exc
                    if not line:
                        return
                    if len(line) > max_message_bytes:
                        raise ConnectorError("stdio message exceeds size limit")
                    await ws.send_str(frame(line))

            async def downstream():
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        writer.write((frame(message.data) + "\n").encode())
                        await writer.drain()
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectorError("server WebSocket failed")
                    else:
                        raise ConnectorError("unexpected WebSocket message type")
                if ws.close_code not in (1000, 1001):
                    raise ConnectorError("server connection ended unexpectedly")

            tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def stdio(socket_path):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
    )
    write_transport, protocol = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout.buffer,
    )
    writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
    try:
        await relay(reader, writer, socket_path)
    finally:
        read_transport.close()
        write_transport.close()


def main():
    # Refuse any invocation other than the pinned adapter's server launch.
    if sys.argv[1:] != ["app-server"]:
        print("codex-connector: expected app-server", file=sys.stderr)
        return 64
    configured = os.environ.get("HIVEMAKE_CODEX_SOCKET")
    if not configured or not Path(configured).is_absolute():
        print("codex-connector: absolute HIVEMAKE_CODEX_SOCKET required", file=sys.stderr)
        return 64
    try:
        asyncio.run(stdio(configured))
    except KeyboardInterrupt:
        return 130
    except Exception:
        # No server messages, host paths, prompts or credentials in diagnostics.
        print("codex-connector: connection failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
