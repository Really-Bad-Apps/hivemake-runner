"""Persist host identity and renew its owner-scoped registration lease."""

import asyncio
import json
import logging
import os
import socket
from uuid import UUID, uuid4

import aiohttp

logger = logging.getLogger(__name__)
OWNER_KEY_ENV = "HIVEMAKE_OWNER_API_KEY"


def identity(state_dir, display_name=None):
    path = state_dir / "identity.json"
    if path.exists():
        runner_id = str(UUID(json.loads(path.read_text())["id"]))
    else:
        runner_id = str(uuid4())
        temp = state_dir / "identity.tmp"
        with temp.open("w") as handle:
            os.chmod(temp, 0o600)
            json.dump({"id": runner_id}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
        fd = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return runner_id, display_name or socket.gethostname()


async def register_once(client, config, manager, runner_id, display_name, key):
    payload = {
        "id": runner_id,
        "display_name": display_name,
        "url": f"http://{config.listen_host}:{config.listen_port}",
        # Keep the registry inventory compatible with existing HiveMake servers.
        # Detailed runner-only configuration is available through proxy endpoints.
        "agents": [
            {key: value for key, value in agent.items() if key != "instruction_profile"}
            for agent in manager.list_agents()["agents"]
        ],
    }
    async with client.post(
        config.hivemake_url + "/api/runners/register",
        json=payload,
        headers={"Authorization": "Bearer " + key},
        allow_redirects=False,
    ) as response:
        # Never log response bodies: reverse proxies can echo Authorization.
        return response.status


async def wait_for_inventory(manager, timeout):
    try:
        await asyncio.wait_for(manager.inventory_changed.wait(), timeout)
    except TimeoutError:
        pass


async def heartbeat(config, manager, runner_id, display_name, key):
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as client:
        while True:
            manager.inventory_changed.clear()
            try:
                status = await register_once(client, config, manager, runner_id, display_name, key)
                manager.registered = status == 200
                if status != 200:
                    logger.warning(
                        "runner_registration_failed runner_id=%s display_name=%s status=%d",
                        runner_id,
                        display_name,
                        status,
                    )
            except (aiohttp.ClientError, TimeoutError) as exc:
                manager.registered = False
                logger.warning(
                    "runner_registration_failed runner_id=%s display_name=%s type=%s",
                    runner_id,
                    display_name,
                    type(exc).__name__,
                )
            await wait_for_inventory(manager, 30)
