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


def test_config_preserves_existing_positional_timeouts(config):
    positional = Config(
        config.roots, config.socket, config.state_dir, config.commands, 12, 34, 5, 6
    )
    assert positional.startup_timeout == 12
    assert positional.run_timeout == 34
    assert positional.max_sessions == 6
    assert positional.discovery_interval == 60


async def test_cancelled_refresh_does_not_overlap_scan(manager, monkeypatch):
    import threading

    from runner import manager as module

    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = module.discover
    calls = 0

    def blocked(*args):
        nonlocal calls
        calls += 1
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return original(*args)

    monkeypatch.setattr(module, "discover", blocked)
    first = asyncio.create_task(manager.refresh_async())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        worker = manager.discovery_task
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(manager.refresh_async())
        await asyncio.sleep(0)
        assert manager.discovery_task is worker
        assert not worker.cancelled()
        release.set()
        assert (await asyncio.wait_for(second, 1))["agents"][0]["id"] == "demo"
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


async def test_inventory_change_during_registration_is_not_lost(manager, config, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    republished = asyncio.Event()
    seen = []

    async def register(*args):
        seen.append(set(manager.agents))
        if len(seen) == 1:
            entered.set()
            await release.wait()
        else:
            republished.set()
        return 200

    monkeypatch.setattr(registration, "register_once", register)
    task = asyncio.create_task(registration.heartbeat(config, manager, "id", "host", "test-key"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        folder = config.roots[0] / "during-post"
        folder.mkdir()
        (folder / "agent.yaml").write_text("version: 1\nid: during-post\nharness: codex\n")
        await manager.refresh_async()
        release.set()
        await asyncio.wait_for(republished.wait(), 1)
        assert seen == [{"demo"}, {"demo", "during-post"}]
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
