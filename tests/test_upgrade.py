"""Offline upgrade tests: no real systemd, provider, HiveMake or Loki calls."""

import json
import os
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import yaml

from runner import upgrade, upgrade_control
from runner.config import Config
from runner.daemon import create_control_app
from runner.manager import Manager, RunnerError
from runner.store import Store
from runner.upgrade import Upgrade, staged_environment
from runner.upgrade_state import UpgradePlan, UpgradePolicy, read_plan, write_plan


def plan() -> UpgradePlan:
    return UpgradePlan(str(uuid4()), "a" * 40, "/tmp/prepared-release", True)


def run_record(intent: UpgradePlan) -> dict:
    return {"id": intent.run_id, "status": "completed", "response_truncated": False,
            "agent_id": "demo", "session_id": "session", "prompt": "upgrade",
            "response": '{"outcome":"runner_upgrade","message":"prepared"}'}


def test_logging_staging_preserves_credentials_and_replaces_duplicates() -> None:
    original = "# Private\nHIVEMAKE_OWNER_API_KEY=private\nCLAUDE_CODE_OAUTH_TOKEN=provider\nDEBUG_LOCAL=false\nLOKI_USER=old\nLOKI_USER=duplicate\nLOKI_PASSWORD=secret\n"
    result = staged_environment(original)
    assert "HIVEMAKE_OWNER_API_KEY=private\n" in result
    assert "CLAUDE_CODE_OAUTH_TOKEN=provider\n" in result
    assert "DEBUG_LOCAL=true\n" in result
    assert result.count("LOKI_USER=") == 1
    assert "LOKI_PASSWORD=\n" in result
    assert "secret" not in result


@pytest.mark.parametrize("original", ["TOKEN='multi\nline'\n", "TOKEN=value\\\n", "export TOKEN=x\n"])
def test_unsupported_environment_rejected(original: str) -> None:
    with pytest.raises(ValueError):
        staged_environment(original)


async def test_handoff_survives_store_reopen_and_completes_only_after_verification(manager, config, monkeypatch) -> None:
    intent = plan()
    write_plan(config.state_dir, intent)
    manager.config_path = config.state_dir / "config.yaml"
    run = run_record(intent)
    launch = AsyncMock(return_value="")
    monkeypatch.setattr(upgrade_control, "command", launch)
    assert await upgrade_control.handoff(manager, run)
    assert run["status"] == "running" and run["upgrade_pending"]
    assert launch.call_args.args[0][0] == "systemd-run"
    assert read_plan(config.state_dir).phase == "ready"
    manager.store.close()
    manager.store = Store(config.state_dir / "runs.sqlite3")
    # The fixture closes its original connection; sqlite close is idempotent.
    assert manager.get_run(run["id"])["status"] == "running"
    updated = read_plan(config.state_dir)
    updated.phase = "verified"
    write_plan(config.state_dir, updated)
    result = manager.get_run(run["id"])
    assert result["status"] == "completed"
    assert json.loads(result["response"])["outcome"] == "resolve"
    assert (await manager.dispatch(run["id"], "demo", "upgrade"))["id"] == run["id"]
    assert not manager.tasks
    manager.store.close()


async def test_ambiguous_launch_remains_pending(manager, config, monkeypatch) -> None:
    intent = plan()
    write_plan(config.state_dir, intent)
    manager.config_path = Path("/tmp/config.yaml")
    run = run_record(intent)
    monkeypatch.setattr(upgrade_control, "command", AsyncMock(side_effect=RuntimeError))
    assert await upgrade_control.handoff(manager, run)
    assert run["status"] == "running" and run["upgrade_pending"]
    assert read_plan(config.state_dir).phase == "ready"


@pytest.mark.parametrize("response", ['{"outcome":"resolve","message":"done"}', '', '{"outcome":"runner_upgrade","message":""}'])
async def test_no_restart_without_explicit_final_ack(manager, config, monkeypatch, response) -> None:
    intent = plan()
    write_plan(config.state_dir, intent)
    run = {**run_record(intent), "response": response}
    launch = AsyncMock()
    monkeypatch.setattr(upgrade_control, "command", launch)
    assert not await upgrade_control.handoff(manager, run)
    launch.assert_not_called()
    assert read_plan(config.state_dir).phase == "failed"


async def test_maintenance_blocks_new_work_but_preserves_existing_results(manager, config) -> None:
    intent = plan()
    write_plan(config.state_dir, intent)
    assert not manager.list_agents()["agents"][0]["ready"]
    with pytest.raises(RunnerError, match="runner_stopping"):
        manager.new_session("demo")
    run = {**run_record(intent), "status": "running", "upgrade_pending": True}
    manager.store.save(run)
    assert manager.get_run(run["id"])["status"] == "running"
    intent.phase = "failed"
    write_plan(config.state_dir, intent)
    assert json.loads(manager.get_run(run["id"])["response"])["outcome"] == "needs_human"
    assert manager.list_agents()["agents"][0]["ready"]


async def test_pending_missing_manifest_is_not_false_success(manager) -> None:
    intent = plan()
    run = {**run_record(intent), "status": "running", "upgrade_pending": True}
    manager.store.save(run)
    assert json.loads(manager.get_run(run["id"])["response"])["outcome"] == "needs_human"


async def test_interrupted_request_releases_admission(config) -> None:
    config.state_dir.mkdir()
    write_plan(config.state_dir, plan())
    store = Store(config.state_dir / "runs.sqlite3")
    manager = Manager(config, store)
    assert read_plan(config.state_dir).phase == "failed"
    assert manager.list_agents()["agents"][0]["ready"]
    await manager.shutdown()
    store.close()


def setup_upgrade(tmp_path: Path) -> Upgrade:
    releases = tmp_path / "releases"
    release = releases / "new"
    release.mkdir(parents=True)
    for binary in ("claude-agent-acp", "codex-acp"):
        path = release / "adapters/node_modules/.bin" / binary
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("adapter")
    environment = tmp_path / "runner.env"
    environment.write_text("HIVEMAKE_OWNER_API_KEY=keep\nDEBUG_LOCAL=false\n")
    state = tmp_path / "state"
    state.mkdir()
    path = tmp_path / "runner.yaml"
    path.write_text(yaml.safe_dump({"roots": [str(tmp_path / "projects")],
        "socket": str(tmp_path / "runner.sock"), "state_dir": str(state),
        "self_upgrade": {"service": "runner.service", "releases": str(releases),
                         "environment": str(environment), "agents": ["demo"]}}))
    intent = replace(plan(), release=str(release), phase="ready")
    job = Upgrade(path, intent)
    job.override = tmp_path / "unit.d/90-hivemake-upgrade.conf"
    job.files["override"] = job.override
    return job


async def test_install_and_rollback_preserve_private_configuration(tmp_path, monkeypatch) -> None:
    job = setup_upgrade(tmp_path)
    before = {name: path.read_bytes() for name, path in job.files.items() if path.exists()}
    job.prepare()
    job.install()
    assert "DEBUG_LOCAL=true" in job.policy.environment.read_text()
    assert "HIVEMAKE_OWNER_API_KEY=keep" in job.policy.environment.read_text()
    assert job.plan.release in job.override.read_text()
    assert Config.load(job.path).self_upgrade == job.policy
    assert job.policy.environment.stat().st_mode & 0o777 == 0o600
    systemctl = AsyncMock(return_value="")
    monkeypatch.setattr(job, "systemctl", systemctl)
    await job.rollback()
    for name, contents in before.items():
        assert job.files[name].read_bytes() == contents
    assert not job.override.exists()
    assert systemctl.call_args_list[-1].args == ("start", "runner.service")


async def test_startup_failure_rolls_back_and_marks_failed(tmp_path, monkeypatch) -> None:
    job = setup_upgrade(tmp_path)
    before = job.path.read_bytes()
    status = {"idle": True, "runner_id": "identity", "registered": True, "pid": 42,
              "agents": {"agents": [{"id": "demo", "ready": True}], "errors": []}}
    monkeypatch.setattr(upgrade, "local", AsyncMock(return_value=status))
    monkeypatch.setattr(upgrade, "validate_release", AsyncMock())
    systemctl = AsyncMock(side_effect=["42", f"{job.policy.environment} (ignore_errors=no)", "", "", "", "", "", ""])
    monkeypatch.setattr(job, "systemctl", systemctl)
    monkeypatch.setattr(job, "verify", AsyncMock(side_effect=RuntimeError))
    with pytest.raises(RuntimeError, match="upgrade_failed"):
        await job.run()
    assert job.path.read_bytes() == before
    assert read_plan(job.config.state_dir).phase == "failed"
    assert not job.override.exists()


def test_self_upgrade_policy_rejects_unsafe_paths_and_wildcards() -> None:
    data = {"service": "runner.service", "releases": "/tmp/releases", "environment": "/tmp/env", "agents": ["demo"]}
    assert UpgradePolicy.parse(data).agents == ("demo",)
    for field, value in (("service", "../runner.service"), ("agents", ["*"]), ("environment", "/tmp/%h"), ("releases", "relative")):
        with pytest.raises(ValueError):
            UpgradePolicy.parse({**data, field: value})


async def test_later_upgrade_cannot_erase_verified_result(manager, config) -> None:
    first = plan()
    manager.store.save({**run_record(first), "status": "running", "upgrade_pending": True})
    first.phase = "verified"
    write_plan(config.state_dir, first)
    write_plan(config.state_dir, plan())
    assert json.loads(manager.get_run(first.run_id)["response"])["outcome"] == "resolve"


async def test_valid_request_requires_authorized_dispatcher_and_exact_release(manager, config, tmp_path, monkeypatch) -> None:
    release = tmp_path / "releases" / "candidate"
    (release / ".venv/bin").mkdir(parents=True)
    (release / ".venv/bin/python").write_text("prepared")
    (release / "runner").mkdir()
    (release / "runner/upgrade_control.py").write_text("protocol")
    manager.config = replace(config, self_upgrade=UpgradePolicy("runner.service", release.parent, tmp_path / "env", ("demo",)))
    manager.config_path = tmp_path / "config.yaml"
    intent = replace(plan(), release=str(release))
    run = {**run_record(intent), "status": "running"}
    manager.runs[run["id"]] = run
    manager.sessions["session"] = {"auto_close": False}
    data = {"run_id": intent.run_id, "revision": intent.revision, "release": intent.release, "stage_logging": True}
    launch = AsyncMock()
    monkeypatch.setattr(upgrade_control, "command", launch)
    with pytest.raises(ValueError, match="dispatcher"):
        await upgrade_control.request_upgrade(manager, data)
    launch.assert_not_called()
    manager.sessions["session"]["auto_close"] = True
    launch.side_effect = [intent.revision, "", "https://github.com/Really-Bad-Apps/hivemake-runner.git", str(os.getpid())]
    accepted = await upgrade_control.request_upgrade(manager, data)
    assert accepted.phase == "requested"
    assert read_plan(config.state_dir).run_id == intent.run_id
    # Remove synthetic session before fixture teardown.
    manager.sessions.clear()
    manager.runs.clear()


async def test_verification_rejects_old_process_before_accepting_new(tmp_path, monkeypatch) -> None:
    job = setup_upgrade(tmp_path)
    before = {"boot_id": "old", "pid": 42, "runner_id": "same", "agents": {"agents": [], "errors": []}}
    old = {**before, "revision": job.plan.revision, "registered": True, "debug_local": True, "upgrade_protocol": 1}
    new = {**old, "boot_id": "new", "pid": 43}
    fetch = AsyncMock(side_effect=[old, new])
    monkeypatch.setattr(upgrade, "local", fetch)
    monkeypatch.setattr(upgrade.asyncio, "sleep", AsyncMock())
    await job.verify(before)
    assert fetch.await_count == 2


async def test_readiness_requires_logging_mode_registration_and_inventory(tmp_path, monkeypatch) -> None:
    job = setup_upgrade(tmp_path)
    before = {"boot_id": "old", "pid": 42, "runner_id": "same", "agents": {"agents": [{"id": "demo"}], "errors": []}}
    good = {**before, "boot_id": "new", "pid": 43, "revision": job.plan.revision,
            "registered": True, "debug_local": True, "upgrade_protocol": 1}
    replies = [{**good, "registered": False}, {**good, "debug_local": False},
               {**good, "agents": {}}, {**good, "runner_id": "changed"}, good]
    fetch = AsyncMock(side_effect=replies)
    monkeypatch.setattr(upgrade, "local", fetch)
    monkeypatch.setattr(upgrade.asyncio, "sleep", AsyncMock())
    await job.verify(before)
    assert fetch.await_count == 5


@pytest.mark.parametrize("reply,failed", [
    ("", False),
    ("LoadState=loaded\nActiveState=active", False),
    ("LoadState=loaded\nActiveState=activating", False),
    ("LoadState=not-found\nActiveState=inactive", True),
    ("LoadState=loaded\nActiveState=failed", True),
])
async def test_watchdog_requires_positive_evidence_job_is_gone(manager, config, monkeypatch, reply, failed) -> None:
    intent = replace(plan(), phase="ready")
    write_plan(config.state_dir, intent)
    monkeypatch.setattr(upgrade_control.time, "time", Mock(return_value=intent.updated_at + 61))
    monkeypatch.setattr(upgrade_control, "command", AsyncMock(return_value=reply))
    await upgrade_control.reconcile_job(manager)
    assert (read_plan(config.state_dir).phase == "failed") is failed


async def test_verification_publication_failure_must_not_rollback_success(tmp_path, monkeypatch) -> None:
    job = setup_upgrade(tmp_path)
    status = {"idle": True, "runner_id": "identity", "registered": True, "pid": 42,
              "agents": {"agents": [{"id": "demo", "ready": True}], "errors": []}}
    monkeypatch.setattr(upgrade, "local", AsyncMock(return_value=status))
    monkeypatch.setattr(upgrade, "validate_release", AsyncMock())
    monkeypatch.setattr(job, "systemctl", AsyncMock(side_effect=[
        "42", f"{job.policy.environment} (ignore_errors=no)", "", "", "",
    ]))
    monkeypatch.setattr(job, "verify", AsyncMock())
    rollback = AsyncMock()
    monkeypatch.setattr(job, "rollback", rollback)
    real_write = upgrade.write_plan

    def failed_publication(state: Path, intent: UpgradePlan) -> None:
        real_write(state, intent)
        if intent.phase == "verified":
            raise OSError("simulated post-publication failure")

    monkeypatch.setattr(upgrade, "write_plan", failed_publication)
    with pytest.raises(OSError):
        await job.run()
    rollback.assert_not_called()
    assert read_plan(job.config.state_dir).phase == "verified"
    assert job.override.exists()


def test_upgrade_preserves_disabled_harness(tmp_path) -> None:
    job = setup_upgrade(tmp_path)
    data = yaml.safe_load(job.path.read_text())
    data["commands"] = {"claude": ["claude-agent-acp"]}
    job.path.write_text(yaml.safe_dump(data))
    job.prepare()
    staged = yaml.safe_load((job.backups / "new-config").read_text())
    assert set(staged["commands"]) == {"claude"}


async def test_maintenance_api_denies_tcp(manager) -> None:
    app = create_control_app(manager)
    route = next(route for route in app.router.routes()
                 if route.resource.canonical == "/maintenance" and route.method == "GET")
    transport = Mock()
    transport.get_extra_info.return_value = SimpleNamespace(family=socket.AF_INET)
    request = SimpleNamespace(transport=transport, method="GET")
    with pytest.raises(RunnerError, match="local_socket_required"):
        await route.handler(request)
    transport.get_extra_info.return_value = SimpleNamespace(family=socket.AF_UNIX)
    response = await route.handler(request)
    assert response.status == 200
    assert json.loads(response.text)["boot_id"] == manager.boot_id
