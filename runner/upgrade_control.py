"""Local-only maintenance admission and deferred execution completion."""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .upgrade_state import ACTIVE, UpgradePlan, read_plan, write_plan


async def command(argv: list[str], *, allow_failure: bool = False) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 120)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode and not allow_failure:
        raise RuntimeError("upgrade_command_failed")
    return stdout.decode().strip()


def job_name(run_id: str) -> str:
    return f"hivemake-upgrade-{run_id}.service"


def active(manager: Any) -> bool:
    plan = read_plan(manager.config.state_dir)
    return plan is not None and plan.phase in ACTIVE


async def request_upgrade(manager: Any, data: dict) -> UpgradePlan:
    policy = manager.config.self_upgrade
    if policy is None or manager.config_path is None:
        raise ValueError("self_upgrade_not_configured")
    if not isinstance(data, dict) or set(data) != {"run_id", "revision", "release", "stage_logging"}:
        raise ValueError("invalid_upgrade_request")
    if not isinstance(data["run_id"], str):
        raise ValueError("invalid_run_id")
    run = manager.runs.get(data["run_id"])
    if not run or run["agent_id"] not in policy.agents:
        raise ValueError("upgrade_requires_active_authorized_agent")
    session = manager.sessions[run["session_id"]]
    if not session.get("auto_close") or run["status"] != "running":
        raise ValueError("upgrade_requires_dispatcher_execution")
    if not isinstance(data["revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", data["revision"]):
        raise ValueError("upgrade_requires_full_commit")
    if not isinstance(data["release"], str) or type(data["stage_logging"]) is not bool:
        raise ValueError("invalid_upgrade_request")
    release = Path(data["release"])
    if (not release.is_absolute() or release.is_symlink()
            or not release.resolve().is_relative_to(policy.releases.resolve())
            or release.resolve() == Path(__file__).resolve().parents[1]
            or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", str(release))):
        raise ValueError("upgrade_requires_separate_staged_release")
    if not (release / ".venv/bin/python").is_file() or not (release / "runner/upgrade_control.py").is_file():
        raise ValueError("upgrade_release_not_prepared")
    revision = await command(["git", "-C", str(release), "rev-parse", "HEAD"])
    dirty = await command(["git", "-C", str(release), "status", "--porcelain"])
    origin = await command(["git", "-C", str(release), "remote", "get-url", "origin"])
    if revision != data["revision"] or dirty or origin.rstrip("/").removesuffix(".git") != "https://github.com/Really-Bad-Apps/hivemake-runner":
        raise ValueError("upgrade_release_revision_or_origin_mismatch")
    main_pid = await command(["systemctl", "--user", "show", policy.service, "-p", "MainPID", "--value"])
    if main_pid != str(os.getpid()):
        raise ValueError("upgrade_service_does_not_own_this_daemon")
    # Recheck after subprocess awaits; a worker may have finished or another
    # request may have reserved maintenance while we validated the candidate.
    if manager.runs.get(data["run_id"]) is not run or run["status"] != "running":
        raise ValueError("upgrade_execution_no_longer_running")
    previous = read_plan(manager.config.state_dir)
    proposed = UpgradePlan(**data)
    if previous and previous.phase in ACTIVE:
        if all(getattr(previous, key) == value for key, value in data.items()):
            return previous
        raise ValueError("upgrade_already_pending")
    write_plan(manager.config.state_dir, proposed)
    manager.inventory_changed.set()
    return proposed


def acknowledged(response: str) -> bool:
    text = response.strip().removesuffix("```").rstrip()
    decoder = json.JSONDecoder()
    for position, char in enumerate(text):
        if char != "{":
            continue
        try:
            answer, end = decoder.raw_decode(text, position)
        except ValueError:
            continue
        if not text[end:].strip():
            return (isinstance(answer, dict) and set(answer) == {"outcome", "message"}
                    and answer["outcome"] == "runner_upgrade"
                    and isinstance(answer["message"], str) and bool(answer["message"].strip()))
    return False


async def handoff(manager: Any, run: dict) -> bool:
    plan = read_plan(manager.config.state_dir)
    if plan is None or plan.run_id != run["id"] or plan.phase != "requested":
        return False
    if run["status"] != "completed" or run.get("response_truncated") or not acknowledged(run["response"]):
        plan.phase, plan.error = "failed", "worker_did_not_acknowledge_upgrade"
        write_plan(manager.config.state_dir, plan)
        return False
    run.update(status="running", upgrade_pending=True, finished_at=None)
    # A durable pending result must precede any out-of-process job launch.
    manager.store.save(run)
    plan.phase = "ready"
    write_plan(manager.config.state_dir, plan)
    try:
        await command([
            "systemd-run", "--user", "--collect", f"--unit={job_name(run['id'])}",
            "--property=Type=exec", "--property=UMask=0077",
            f"--working-directory={Path(__file__).resolve().parents[1]}",
            sys.executable, "-m", "runner.upgrade", "apply",
            "--config", str(manager.config_path), "--run-id", run["id"],
        ])
    except Exception:
        # A lost systemd-run reply does not mean the job failed to start.
        # Leave admission closed until the watchdog confirms the job is gone.
        logging.getLogger(__name__).warning("upgrade_launch_unconfirmed run_id=%s", run["id"])
    return True


async def reconcile_job(manager: Any) -> None:
    plan = read_plan(manager.config.state_dir)
    if not plan or plan.phase not in {"ready", "applying"} or time.time() - plan.updated_at < 60:
        return
    try:
        state = await command([
            "systemctl", "--user", "show", job_name(plan.run_id),
            "-p", "LoadState", "-p", "ActiveState",
        ], allow_failure=True)
    except (OSError, TimeoutError):
        return
    fields = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
    gone = fields.get("LoadState") == "not-found" or (
        fields.get("LoadState") == "loaded" and fields.get("ActiveState") in {"inactive", "failed"}
    )
    # Empty output / bus errors are not evidence of a lost job.
    if not gone:
        return
    current = read_plan(manager.config.state_dir)
    if current and current.run_id == plan.run_id and current.phase in {"ready", "applying"}:
        current.phase, current.error = "failed", "upgrade_job_lost"
        write_plan(manager.config.state_dir, current)
        manager.inventory_changed.set()


async def watch(manager: Any) -> None:
    while True:
        await asyncio.sleep(5)
        try:
            await reconcile_job(manager)
        except Exception as exc:
            # Keep admission closed and retry; a damaged manifest or transient
            # filesystem failure must not kill the watchdog or daemon cleanup.
            logging.getLogger(__name__).warning("upgrade_watch_failed type=%s", type(exc).__name__)
