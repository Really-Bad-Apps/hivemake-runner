from uuid import uuid4

import pytest
from aiohttp import web

from runner.codex_live_state import validate_live_thread


@pytest.mark.parametrize('change,valid', [({}, True), ({'source': 'vscode'}, True), ({'source': 'appServer'}, True), ({'status': {'type': 'active'}}, True),
    ({'status': {'type': 'notLoaded'}}, False), ({'source': 'exec'}, False),
    ({'canAcceptDirectInput': False}, False), ({'parentThreadId': 'parent'}, False),
    ({'cwd': '/different-project'}, False), ({'id': str(uuid4())}, False)])
async def test_only_exact_loaded_cli_thread(tmp_path, change, valid):
    ident = str(uuid4())
    methods = []

    async def endpoint(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            data = msg.json()
            methods.append(data['method'])
            thread = dict(id=ident, cwd=str(tmp_path), source='cli', status={'type': 'idle'},
                          parentThreadId=None, canAcceptDirectInput=True)
            thread.update(change)
            await ws.send_json({'id': data['id'], 'result': {'thread': thread}})
        return ws

    app = web.Application()
    app.router.add_get('/', endpoint)
    server = web.AppRunner(app)
    await server.setup()
    socket = tmp_path / 'native.sock'
    await web.UnixSite(server, str(socket)).start()
    try:
        if valid:
            await validate_live_thread(socket, ident, str(tmp_path))
        else:
            with pytest.raises(ValueError):
                await validate_live_thread(socket, ident, str(tmp_path))
        assert methods == ['initialize', 'thread/read']
    finally:
        await server.cleanup()
