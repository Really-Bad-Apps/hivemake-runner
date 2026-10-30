"""Exercise the actual daemon process, locking, SIGTERM, and restart."""

import asyncio
import json
import os
import sys

import httpx
import yaml


async def wait_ready(client, proc):
    async with asyncio.timeout(8):
        while True:
            assert proc.returncode is None
            try:
                if (await client.get("/health")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.03)


async def test_process_lock_signal_cleanup_and_restart(config, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "roots": [str(p) for p in config.roots],
                "socket": str(config.socket),
                "state_dir": str(config.state_dir),
                "commands": {key: list(value) for key, value in config.commands.items()},
            }
        )
    )

    async def launch():
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "runner.daemon",
            "--config",
            str(config_path),
            # Never inherit a host's production Loki mode into this fixture.
            env={**os.environ, "DEBUG_LOCAL": "true"},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    proc = await launch()
    transport = httpx.AsyncHTTPTransport(uds=str(config.socket))
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        try:
            await wait_ready(client, proc)
            assert config.socket.stat().st_mode & 0o777 == 0o600
            duplicate = await launch()
            assert await asyncio.wait_for(duplicate.wait(), 5) != 0
            assert (await client.get("/health")).status_code == 200
            sid = (await client.post("/sessions", json={"agent_id": "demo"})).json()["id"]
            # The daemon validates input independently of the HiveMake gateway.
            for payload in ([], {"prompt": 1}, {"prompt": " "}, {"prompt": "x", "extra": 1}):
                assert (await client.post(f"/sessions/{sid}/runs", json=payload)).status_code == 400
            run = (await client.post(f"/sessions/{sid}/runs", json={"prompt": "hang"})).json()
            await asyncio.sleep(0.3)
            proc.terminate()
            assert await asyncio.wait_for(proc.wait(), 10) == 0
            assert not config.socket.exists()
            proc = await launch()
            await wait_ready(client, proc)
            result = (await client.get(f"/runs/{run['id']}")).json()
            assert result["status"] == "interrupted", json.dumps(result)
            assert (await client.get(f"/sessions/{sid}")).status_code == 404
        finally:
            if proc.returncode is None:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 10)
