import asyncio

import httpx
import pytest
from aiohttp import web

from runner.daemon import create_control_app


@pytest.fixture
async def api(manager, config):
    config.socket.parent.mkdir()
    server = web.AppRunner(create_control_app(manager))
    await server.setup()
    await web.UnixSite(server, str(config.socket)).start()
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(config.socket)),
        base_url="http://runner",
        trust_env=False,
    ) as client:

        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            return response.status_code, response.json() if response.content else None

        yield request
    await server.cleanup()


async def test_api_to_unix_socket_to_acp(api):
    assert (await api("GET", "/health"))[0] == 200
    status, agents = await api("GET", "/agents")
    assert agents["agents"][0]["id"] == "demo"
    status, session = await api("POST", "/sessions", json={"agent_id": "demo"})
    assert status == 201
    path = f"/sessions/{session['id']}/runs"
    status, run = await api("POST", path, json={"prompt": "hello"})
    assert status == 202
    async with asyncio.timeout(5):
        while True:
            status, result = await api("GET", f"/runs/{run['id']}")
            if result["status"] != "running":
                break
            await asyncio.sleep(0.03)
    assert result["response"] == "turn 1: hello"
    assert (await api("DELETE", f"/sessions/{session['id']}"))[0] == 204


async def test_busy_and_validation_statuses(api):
    _, session = await api("POST", "/sessions", json={"agent_id": "demo"})
    path = f"/sessions/{session['id']}/runs"
    for payload in ({}, {"prompt": " "}, {"prompt": 1}, {"prompt": "x", "extra": 1}):
        assert (await api("POST", path, json=payload))[0] == 400
    assert (await api("POST", "/sessions", json={"agent_id": "missing"}))[0] == 404
    _, run = await api("POST", path, json={"prompt": "hang"})
    status, error = await api("POST", path, json={"prompt": "again"})
    assert status == 409 and error["error"] == "agent_busy"
    assert (await api("DELETE", f"/sessions/{session['id']}"))[0] == 409
    assert (await api("POST", f"/runs/{run['id']}/cancel"))[0] == 202


async def test_idempotent_execution_api(api, manager):
    from uuid import uuid4

    eid = str(uuid4())
    body = {"agent_id": "demo", "prompt": "hello"}
    first = (await api("PUT", f"/executions/{eid}", json=body))[1]
    second = (await api("PUT", f"/executions/{eid}", json=body))[1]
    assert first["id"] == second["id"] == eid
    assert first["session_id"] == second["session_id"]
    assert len(manager.sessions) == 1
    from tests_helpers import wait_finished

    await wait_finished(manager, eid)
    third = (await api("PUT", f"/executions/{eid}", json=body))[1]
    assert third["response"] == "turn 1: hello"
    assert (await api("PUT", f"/executions/{eid}", json={**body, "prompt": "changed"}))[0] == 409
    assert (await api("PUT", "/executions/not-a-uuid", json=body))[0] == 400
