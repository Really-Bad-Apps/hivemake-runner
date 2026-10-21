import asyncio
import json

import pytest
from aiohttp import ClientError, web

from runner.codex_connector import ConnectorError, frame, relay


class Output:
    def __init__(self):
        self.data = bytearray()
        self.received = asyncio.Event()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        self.received.set()


@pytest.fixture
async def server(tmp_path):
    messages = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            messages.append(json.loads(message.data))
            await ws.send_str(json.dumps({"id": 1, "result": "hello\nworld"}, indent=2))
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    socket = tmp_path / "server.sock"
    await web.UnixSite(runner, str(socket)).start()
    yield socket, messages
    await runner.cleanup()


async def roundtrip(socket):
    reader = asyncio.StreamReader()
    output = Output()
    task = asyncio.create_task(relay(reader, output, socket))
    reader.feed_data(b'{"id":1,"method":"test","params":{"text":"hello\\nworld"}}\n')
    try:
        await asyncio.wait_for(output.received.wait(), 2)
        reader.feed_eof()
        await asyncio.wait_for(task, 2)
        return output.data
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_framing_and_detach_leave_server_alive(server):
    socket, messages = server
    for _ in range(2):
        data = await roundtrip(socket)
        assert data.count(b"\n") == 1
        assert json.loads(data) == {"id": 1, "result": "hello\nworld"}
    assert [m["method"] for m in messages] == ["test", "test"]
    assert messages[0]["params"]["text"] == "hello\nworld"


async def test_oversized_input_is_not_forwarded(server):
    socket, messages = server
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"secret":"' + b"x" * 80 + b'"}\n')
    with pytest.raises(ConnectorError, match="size limit"):
        await asyncio.wait_for(relay(reader, Output(), socket, max_message_bytes=64), 2)
    assert messages == []


async def test_cancellation_only_disconnects(server):
    socket, _ = server
    task = asyncio.create_task(relay(asyncio.StreamReader(), Output(), socket))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert json.loads(await roundtrip(socket))["id"] == 1


async def test_missing_server_fails_without_creating_socket(tmp_path):
    socket = tmp_path / "absent.sock"
    with pytest.raises(ClientError):
        await relay(asyncio.StreamReader(), Output(), socket)
    assert not socket.exists()


@pytest.mark.parametrize("binary", [False, True])
async def test_server_close_or_bad_frame_ends_waiting_reader(tmp_path, binary):
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if binary:
            await ws.send_bytes(b"not JSON text")
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    server = web.AppRunner(app)
    await server.setup()
    socket = tmp_path / "closing.sock"
    await web.UnixSite(server, str(socket)).start()
    try:
        operation = relay(asyncio.StreamReader(), Output(), socket)
        if binary:
            with pytest.raises(ConnectorError, match="message type"):
                await asyncio.wait_for(operation, 2)
        else:
            await asyncio.wait_for(operation, 2)
    finally:
        await server.cleanup()


@pytest.mark.parametrize("value", [
    b"sensitive-invalid-input", b"[]", b"\xff", b'{"x": NaN}', b'{"x": Infinity}',
])
def test_invalid_frames_do_not_expose_contents(value):
    with pytest.raises(ConnectorError) as caught:
        frame(value)
    assert "sensitive" not in str(caught.value)


def test_framing_preserves_numeric_tokens_and_unicode_string_content():
    source = '{\n"n": 0.12345678901234567890123, "large": 1e400, "s": "a\u0085b"\n}'
    assert frame(source) == source.replace("\n", "")


async def test_stalled_websocket_upgrade_has_deadline(tmp_path):
    release = asyncio.Event()

    async def stalled(reader, writer):
        try:
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()

    socket = tmp_path / "stalled.sock"
    server = await asyncio.start_unix_server(stalled, path=socket)
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(relay(
                asyncio.StreamReader(), Output(), socket, handshake_timeout=0.02,
            ), 1)
    finally:
        release.set()
        server.close()
        await server.wait_closed()
