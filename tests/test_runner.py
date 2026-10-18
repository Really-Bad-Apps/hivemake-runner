import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from runner.discovery import discover
from runner.manager import Manager, RunnerError
from runner.store import Store


async def finished(manager, run_id):
    async with asyncio.timeout(10):
        while run_id in manager.tasks:
            await asyncio.sleep(0.02)
    return manager.get_run(run_id)


async def test_real_acp_roundtrip_followup_permission_and_persistence(manager, config):
    session = manager.new_session("demo")
    run = manager.submit(session["id"], "permission")
    result = await finished(manager, run["id"])
    assert result["status"] == "completed"
    assert result["response"] == "turn 1: permission"
    assert (config.roots[0] / "approved.txt").read_text() == "approved"
    assert any(e.get("approved") for e in result["events"])
    second = manager.submit(session["id"], "follow up")
    assert (await finished(manager, second["id"]))["response"] == "turn 2: follow up"
    await manager.close_session(session["id"])
    assert manager.get_run(run["id"])["response"] == result["response"]


async def test_busy_is_per_agent_across_sessions_and_cancel_stops_process(manager):
    a, b = manager.new_session("demo"), manager.new_session("demo")
    run = manager.submit(a["id"], "hang")
    with pytest.raises(RunnerError, match="agent_busy") as err:
        manager.submit(b["id"], "must not run")
    assert err.value.status == 409
    await asyncio.sleep(0.4)
    runtime = manager.sessions[a["id"]]["runtime"]
    manager.cancel(run["id"])
    result = await finished(manager, run["id"])
    assert result["status"] == "cancelled"
    assert runtime.process.returncode is not None
    assert manager.get_session(a["id"])["status"] == "expired"
    followup = manager.submit(b["id"], "available again")
    assert (await finished(manager, followup["id"]))["status"] == "completed"


async def test_cancel_stops_child_process(manager, config):
    session = manager.new_session("demo")
    run = manager.submit(session["id"], "child")
    pidfile = config.roots[0] / "child.pid"
    async with asyncio.timeout(5):
        while not pidfile.exists():
            await asyncio.sleep(0.02)
    pid = int(pidfile.read_text())
    manager.cancel(run["id"])
    assert (await finished(manager, run["id"]))["status"] == "cancelled"
    proc = Path(f"/proc/{pid}/stat")
    assert not proc.exists() or proc.read_text().split()[2] == "Z"


async def test_timeout_invalidates_session_and_releases_agent(manager):
    manager.config = replace(manager.config, run_timeout=0.5)
    s = manager.new_session("demo")
    run = manager.submit(s["id"], "hang")
    result = await finished(manager, run["id"])
    assert result["status"] == "failed"
    assert result["error"] == "run_timeout"
    assert "demo" not in manager.busy
    with pytest.raises(RunnerError, match="session_expired"):
        manager.submit(s["id"], "again")


async def test_adapter_crash_releases_agent(manager):
    s = manager.new_session("demo")
    run = manager.submit(s["id"], "crash")
    result = await finished(manager, run["id"])
    assert result["status"] == "failed"
    assert result["error"] == "adapter_error"
    assert not manager.busy


async def test_restart_results_survive_but_sessions_do_not(manager, config):
    s = manager.new_session("demo")
    completed = manager.submit(s["id"], "hello")
    await finished(manager, completed["id"])
    active = manager.submit(s["id"], "hang")
    await asyncio.sleep(0.1)
    await manager.shutdown()
    assert manager.get_run(active["id"])["status"] == "interrupted"
    # Simulate a crash between submission and final state persistence.
    crashed = dict(active, id="crashed", status="running")
    manager.store.save(crashed)
    store = Store(config.state_dir / "runs.sqlite3")
    restarted = Manager(config, store)
    assert restarted.get_run("crashed")["status"] == "interrupted"
    assert restarted.get_run(completed["id"])["response"] == "turn 1: hello"
    with pytest.raises(RunnerError, match="session_not_found"):
        restarted.get_session(s["id"])
    await restarted.shutdown()
    store.close()


async def test_refresh_invalidates_changed_definition(manager, config):
    s = manager.new_session("demo")
    (config.roots[0] / "agent.yaml").write_text("version: 1\nid: demo\nharness: claude\n")
    manager.refresh()
    with pytest.raises(RunnerError, match="agent_configuration_changed"):
        manager.submit(s["id"], "hello")


def test_discovery_nested_duplicates_symlinks_invalid_and_overlap(config):
    root = config.roots[0]

    def marker(name, body):
        p = root / name
        p.mkdir(parents=True)
        (p / "agent.yaml").write_text(body)

    marker("child", "version: 1\nid: child\nharness: claude\n")
    marker("node_modules/hidden", "version: 1\nid: hidden\nharness: codex\n")
    marker("bad", "version: 1\nid: BAD\nharness: codex\n")
    (root / "linked").symlink_to(root / "child", target_is_directory=True)
    agents, result = discover((root, root / "child"), config.commands)
    assert set(agents) == {"demo", "child"}
    assert len(result["errors"]) == 1
    marker("duplicate", "version: 1\nid: demo\nharness: codex\n")
    agents, result = discover((root,), config.commands)
    assert "demo" not in agents
    assert any(e["error"] == "duplicate_agent_id" for e in result["errors"])


async def test_cancel_immediately_does_not_launch(manager):
    s = manager.new_session("demo")
    run = manager.submit(s["id"], "permission")
    manager.cancel(run["id"])
    assert (await finished(manager, run["id"]))["status"] == "cancelled"
    assert manager.sessions[s["id"]]["runtime"] is None


async def test_missing_adapter_and_session_limit(manager):
    manager.config = replace(manager.config, max_sessions=1)
    s = manager.new_session("demo")
    with pytest.raises(RunnerError, match="session_limit"):
        manager.new_session("demo")
    await manager.close_session(s["id"])
    manager.config = replace(manager.config, commands={"codex": ("/missing/adapter",)})
    with pytest.raises(RunnerError, match="adapter_not_installed"):
        manager.new_session("demo")


def test_config_validation(tmp_path):
    from runner.config import Config

    path = tmp_path / "config.yaml"
    path.write_text("roots: [relative]\nsocket: /tmp/s\nstate_dir: /tmp/d\n")
    with pytest.raises(ValueError):
        Config.load(path)
    path.write_text(
        "roots: [/tmp/projects]\nsocket: /tmp/s\nstate_dir: /tmp/d\nrun_timeout: .nan\n"
    )
    with pytest.raises(ValueError):
        Config.load(path)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "192.168.1.2"])
def test_reject_public_or_wildcard_control_listener(tmp_path, host):
    from runner.config import Config

    path = tmp_path / "config.yaml"
    path.write_text(
        f'roots: [/tmp/projects]\nsocket: /tmp/s\nstate_dir: /tmp/d\nlisten_host: "{host}"\n'
    )
    with pytest.raises(ValueError):
        Config.load(path)


async def test_output_and_event_truncation_are_distinct(manager, monkeypatch):
    from runner import manager as module

    monkeypatch.setattr(module, "MAX_EVENTS", 0)
    session = manager.new_session("demo")
    run = manager.submit(session["id"], "hello")
    from tests_helpers import wait_finished

    await wait_finished(manager, run["id"])
    result = manager.get_run(run["id"])
    assert result["events_truncated"] is True
    assert result["response_truncated"] is False
    monkeypatch.setattr(module, "MAX_OUTPUT", 2)
    run = manager.submit(session["id"], "hello")
    await wait_finished(manager, run["id"])
    assert manager.get_run(run["id"])["response_truncated"] is True
