import asyncio
from contextlib import suppress
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import yaml

from runner import registration
from runner.config import Config


@pytest.mark.parametrize("value", [0, -1, True, "60", float("inf"), float("nan")])
def test_invalid_discovery_interval(config, tmp_path, value):
    path = tmp_path / "runner.yaml"
    path.write_text(
        yaml.safe_dump(
            dict(
                roots=[str(config.roots[0])],
                socket=str(config.socket),
                state_dir=str(config.state_dir),
                discovery_interval=value,
            )
        )
    )
    with pytest.raises(ValueError, match="discovery_interval"):
        Config.load(path)


async def test_periodic_scan_publishes_changes_without_mutating_sessions(manager, config):
    old = manager.new_session("demo")["id"]
    manager.config = replace(config, discovery_interval=0.02)
    marker = config.roots[0] / "agent.yaml"
    marker.write_text("version: 1\nid: demo\nname: Renamed\nharness: claude\n")
    nested = config.roots[0] / "nested"
    nested.mkdir()
    (nested / "agent.yaml").write_text("version: 1\nid: added\nharness: codex\n")
    task = asyncio.create_task(manager.periodic_discovery())
    try:
        await asyncio.wait_for(manager.inventory_changed.wait(), 2)
        assert manager.agents["demo"].harness == "claude"
        assert "added" in manager.agents
        assert manager.sessions[old]["agent"].harness == "codex"
        new = manager.new_session("demo")["id"]
        assert manager.sessions[new]["agent"].harness == "claude"
        manager.inventory_changed.clear()
        (nested / "agent.yaml").unlink()
        await asyncio.wait_for(manager.inventory_changed.wait(), 2)
        assert "added" not in manager.agents
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_inventory_change_wakes_registration_before_heartbeat(manager, config, monkeypatch):
    posted = asyncio.Event()
    seen = []

    async def register(*args):
        seen.append(set(manager.agents))
        posted.set()
        return 200

    monkeypatch.setattr(registration, "register_once", register)
    task = asyncio.create_task(registration.heartbeat(config, manager, "id", "host", "test-key"))
    try:
        await asyncio.wait_for(posted.wait(), 1)
        posted.clear()
        folder = config.roots[0] / "new"
        folder.mkdir()
        (folder / "agent.yaml").write_text("version: 1\nid: new\nharness: codex\n")
        await manager.refresh_async()
        await asyncio.wait_for(posted.wait(), 1)  # well before the 30-second heartbeat
        assert seen[-1] == {"demo", "new"}
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_scan_failure_keeps_inventory_and_retries(manager, config, monkeypatch):
    manager.config = replace(config, discovery_interval=0.01)
    original = manager.refresh_async
    calls = AsyncMock(side_effect=[OSError("scan failed"), None])
    recovered = asyncio.Event()

    async def refresh():
        await calls()
        assert "demo" in manager.agents
        await original()
        recovered.set()

    monkeypatch.setattr(manager, "refresh_async", refresh)
    task = asyncio.create_task(manager.periodic_discovery())
    try:
        await asyncio.wait_for(recovered.wait(), 2)
        assert calls.await_count >= 2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
