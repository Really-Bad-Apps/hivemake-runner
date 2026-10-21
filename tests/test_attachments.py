import asyncio
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from runner.attachments import Attachments
from runner.manager import RunnerError


class Terminal:
    instances = []

    def __init__(self, **kwargs):
        self.prompts = []
        self.closed = False
        self.release = asyncio.Event()
        self.instances.append(self)

    async def start(self):
        pass

    async def prompt(self, message):
        self.prompts.append(message)
        await self.release.wait()
        return 'end_turn'

    async def close(self):
        self.closed = True


async def valid(*args):
    pass


async def attached(manager):
    manager.config = replace(manager.config, codex_socket=manager.config.socket)
    manager.attachments = Attachments(manager, factory=Terminal, validator=valid)
    aid = str(uuid4())
    row = manager.attachments.register(aid, 'demo', str(uuid4()), int(time.time()) + 3600)
    await asyncio.gather(*manager.attachments.tasks.values())
    assert manager.attachments.get(aid)['status'] == 'active'
    return manager.attachments, aid, row


async def test_exact_session_idempotency_busy_and_detach(manager):
    a, aid, row = await attached(manager)
    assert a.register(aid, 'demo', row['session_id'], row['expires_at'])['status'] == 'active'
    with pytest.raises(RunnerError, match='terminal_already_attached'):
        a.register(str(uuid4()), 'demo', row['session_id'], row['expires_at'])
    terminal = a.sessions[aid]
    did = str(uuid4())
    assert a.deliver(did, aid, 'Peer ticket result')['status'] == 'admitted'
    await asyncio.sleep(0)
    assert a.deliver(did, aid, 'Peer ticket result')['status'] == 'submitted'
    with pytest.raises(RunnerError, match='delivery_conflict'):
        a.deliver(did, aid, 'different')
    with pytest.raises(RunnerError, match='attachment_busy'):
        a.deliver(str(uuid4()), aid, 'second')
    terminal.release.set()
    await asyncio.gather(*a.tasks.values())
    assert a.delivery(did)['status'] == 'completed'
    assert terminal.prompts == ['Peer ticket result']
    await a.detach(aid.upper())
    assert terminal.closed
    assert a.get(aid)['status'] == 'detached'
    assert a.deliver(did, aid, 'Peer ticket result')['status'] == 'completed'


async def test_failed_live_validation_never_prompts(manager):
    a, aid, _ = await attached(manager)
    terminal = a.sessions[aid]

    async def stale(*args):
        raise ValueError('not_loaded')

    a.validator = stale
    did = str(uuid4())
    a.deliver(did, aid, 'result')
    await asyncio.gather(*a.tasks.values())
    assert a.delivery(did)['status'] == 'failed'
    assert terminal.prompts == []
    assert terminal.closed
    assert a.get(aid)['status'] == 'failed'


async def test_restart_never_replays_submission(manager):
    a, aid, _ = await attached(manager)
    did = str(uuid4())
    a.deliver(did, aid, 'result')
    await asyncio.sleep(0)
    assert a.delivery(did)['status'] == 'submitted'
    await a.shutdown()
    assert a.delivery(did)['status'] == 'uncertain'
    replacement = Attachments(manager, factory=Terminal, validator=valid)
    assert replacement.get(aid)['status'] != 'active'
    assert replacement.deliver(did, aid, 'result')['status'] == 'uncertain'
    assert replacement.sessions == {}


async def test_disabled_and_expired(manager):
    with pytest.raises(RunnerError, match='terminal_attachments_disabled'):
        manager.attachments.register(str(uuid4()), 'demo', str(uuid4()), int(time.time()) + 100)
    a, aid, row = await attached(manager)
    terminal = a.sessions[aid]
    row['expires_at'] = 0
    a._save('terminal_attachments', row)
    with pytest.raises(RunnerError, match='attachment_inactive'):
        a.deliver(str(uuid4()), aid, 'result')
    await a.reap()
    assert terminal.closed


async def test_shutdown_immediately_after_admission(manager):
    a, aid, _ = await attached(manager)
    did = str(uuid4())
    a.deliver(did, aid, 'result')
    await a.shutdown()
    assert a.delivery(did)['status'] in ('failed', 'uncertain')
    assert not a.busy


async def test_two_terminals_route_independently_and_share_capacity(manager):
    a, first, _ = await attached(manager)
    second = str(uuid4())
    a.register(second, 'demo', str(uuid4()), int(time.time()) + 3600)
    await asyncio.gather(*a.tasks.values())
    manager.config = replace(manager.config, max_sessions=2)
    with pytest.raises(RunnerError, match='session_limit'):
        manager.new_session('demo')
    with pytest.raises(RunnerError, match='attachment_capacity'):
        a.register(str(uuid4()), 'demo', str(uuid4()), int(time.time()) + 3600)
    a.deliver(str(uuid4()), first, 'first result')
    a.deliver(str(uuid4()), second, 'second result')
    a.sessions[first].release.set()
    a.sessions[second].release.set()
    await asyncio.gather(*a.tasks.values())
    assert a.sessions[first].prompts == ['first result']
    assert a.sessions[second].prompts == ['second result']


async def test_control_api_roundtrip_and_invalid_identifiers(manager):
    from aiohttp.test_utils import TestClient, TestServer

    from runner.daemon import create_control_app
    a, aid, _ = await attached(manager)
    async with TestClient(TestServer(create_control_app(manager))) as client:
        assert (await client.get(f'/attachments/{aid}')).status == 200
        response = await client.put('/deliveries/' + str(uuid4()), json={'attachment_id': [], 'message': 'result'})
        assert response.status == 400
        did = str(uuid4())
        assert (await client.put(f'/deliveries/{did}', json={'attachment_id': aid, 'message': 'answer'})).status == 202
        a.sessions[aid].release.set()
        await asyncio.gather(*a.tasks.values())
        assert (await (await client.get(f'/deliveries/{did}')).json())['status'] == 'completed'
        assert (await client.delete(f'/attachments/{aid}')).status == 204
        assert (await (await client.get(f'/attachments/{aid}')).json())['status'] == 'detached'
