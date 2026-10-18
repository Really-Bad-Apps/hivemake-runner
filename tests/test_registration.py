import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID

import aiohttp
import pytest
import yaml
from aiohttp import web

from runner.config import Config
from runner.registration import identity, register_once


def test_identity_survives_restart_and_rename(tmp_path):
    first, name = identity(tmp_path, "First host")
    assert str(UUID(first)) == first and name == "First host"
    assert identity(tmp_path, "Renamed") == (first, "Renamed")
    assert (tmp_path / "identity.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "identity.json").read_text()) == {"id": first}


def test_corrupt_identity_is_not_silently_replaced(tmp_path):
    (tmp_path / "identity.json").write_text('{"id":"broken"}')
    with pytest.raises(ValueError):
        identity(tmp_path)


async def test_registration_sends_identity_inventory_and_key_without_redirect(manager, config):
    received = []

    async def receive(request):
        received.append((request.headers.get("Authorization"), await request.json()))
        return web.Response(status=302, headers={"Location": "/leak"})

    async def leak(request):
        pytest.fail("Registration must never follow redirects")

    app = web.Application()
    app.router.add_post("/api/runners/register", receive)
    app.router.add_get("/leak", leak)
    server = web.AppRunner(app)
    await server.setup()
    await web.TCPSite(server, "127.0.0.1", 0).start()
    cfg = replace(
        config, hivemake_url=f"http://127.0.0.1:{server.addresses[0][1]}", listen_host="100.64.0.10"
    )
    try:
        async with aiohttp.ClientSession(trust_env=False) as client:
            status = await register_once(
                client, cfg, manager, "runner-id", "Friendly", "hm_owner_test"
            )
        assert status == 302
        token, payload = received[0]
        assert token == "Bearer hm_owner_test"
        assert payload["display_name"] == "Friendly"
        assert payload["url"] == "http://100.64.0.10:7184"
        assert payload["agents"][0]["id"] == "demo"
        assert "instruction_profile" not in payload["agents"][0]
        assert "hm_owner_test" not in json.dumps(payload)
    finally:
        await server.cleanup()


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://user:secret@example.test",
        "https://example.test/path",
        "https://example.test?token=x",
    ],
)
def test_rejects_registration_credential_leak_urls(config, tmp_path, url):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            dict(
                roots=[str(config.roots[0])],
                socket=str(config.socket),
                state_dir=str(config.state_dir),
                hivemake_url=url,
                listen_host="100.64.0.10",
            )
        )
    )
    with pytest.raises(ValueError):
        Config.load(path)


async def test_heartbeat_recovers_and_repeats(monkeypatch, config, manager, caplog):
    from runner import registration

    calls = AsyncMock(side_effect=[aiohttp.ClientConnectionError("secret-do-not-log"), 200])
    monkeypatch.setattr(registration, "register_once", calls)
    original_sleep = registration.asyncio.sleep
    sleeps = 0

    async def sleep(delay):
        nonlocal sleeps
        if delay == 0:
            return await original_sleep(0)
        assert delay == 30
        sleeps += 1
        if sleeps == 2:
            raise KeyboardInterruptForTest()

    monkeypatch.setattr(registration.asyncio, "sleep", sleep)
    with pytest.raises(KeyboardInterruptForTest):
        await registration.heartbeat(config, manager, "id", "host", "hm_owner_test")
    assert calls.await_count == 2
    assert "secret-do-not-log" not in caplog.text
    assert "hm_owner_test" not in caplog.text


class KeyboardInterruptForTest(Exception):
    pass


async def test_owner_key_is_not_in_agent_environment(manager, monkeypatch):
    from runner import acp_session
    from tests.test_runner import finished

    original = acp_session.asyncio.create_subprocess_exec
    observed = []

    async def capture(*args, **kwargs):
        observed.append(kwargs["env"])
        return await original(*args, **kwargs)

    monkeypatch.setenv("HIVEMAKE_OWNER_API_KEY", "hm_owner_do_not_inherit")
    monkeypatch.setattr(acp_session.asyncio, "create_subprocess_exec", capture)
    sid = manager.new_session("demo")["id"]
    result = await finished(manager, manager.submit(sid, "hello")["id"])
    assert result["status"] == "completed"
    assert observed and "HIVEMAKE_OWNER_API_KEY" not in observed[0]
