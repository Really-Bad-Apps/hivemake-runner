import asyncio

import pytest
from aiohttp import web

from hivemake_runner import RunnerClient, create_app
from runner.daemon import create_control_app


@pytest.fixture
async def api(manager, config):
    config.socket.parent.mkdir()
    server = web.AppRunner(create_control_app(manager))
    await server.setup()
    await web.UnixSite(server, str(config.socket)).start()
    app = create_app(RunnerClient(str(config.socket)))

    def call(method, path, **kwargs):
        with app.test_client() as client:
            response = client.open(path, method=method, **kwargs)
            return response.status_code, response.get_json(silent=True)

    async def request(method, path, **kwargs):
        return await asyncio.to_thread(call, method, path, **kwargs)

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
        assert (await api("POST", path, json=payload))[0] == 422
    assert (await api("POST", "/sessions", json={"agent_id": "missing"}))[0] == 404
    _, run = await api("POST", path, json={"prompt": "hang"})
    status, error = await api("POST", path, json={"prompt": "again"})
    assert status == 409 and error["error"] == "agent_busy"
    assert (await api("DELETE", f"/sessions/{session['id']}"))[0] == 409
    assert (await api("POST", f"/runs/{run['id']}/cancel"))[0] == 202


def test_unavailable_runner_returns_503(tmp_path):
    client = create_app(RunnerClient(str(tmp_path / "absent.sock"))).test_client()
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json == {"error": "runner_unavailable"}


async def test_api_to_tcp_runner(manager, monkeypatch):
    server = web.AppRunner(create_control_app(manager))
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    port = server.addresses[0][1]
    monkeypatch.setenv("RUNNER_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    app = create_app()

    def call():
        with app.test_client() as client:
            assert client.get("/health").status_code == 200
            session = client.post("/sessions", json={"agent_id": "demo"})
            assert session.status_code == 201
            response = client.post(f"/sessions/{session.json['id']}/runs", json={"prompt": "hello"})
            assert response.status_code == 202
            return response.json["id"]

    try:
        rid = await asyncio.to_thread(call)
        async with asyncio.timeout(5):
            while rid in manager.tasks:
                await asyncio.sleep(0.02)
        assert manager.get_run(rid)["response"] == "turn 1: hello"
    finally:
        await server.cleanup()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test",
        "http://user:password@example.test",
        "http://example.test/path",
        "http://example.test?token=secret",
    ],
)
def test_remote_origin_validation(url):
    with pytest.raises(ValueError):
        RunnerClient(url=url)
