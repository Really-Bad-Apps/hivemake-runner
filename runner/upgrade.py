"""Independent user-systemd upgrade job; never run inside the worker's cgroup."""

import argparse
import asyncio
import fcntl
import json
import os
import re
import time
from pathlib import Path

import aiohttp
import yaml

from .config import Config
from .upgrade_control import command
from .upgrade_state import UPGRADE_PROTOCOL, UpgradePlan, atomic_write, read_plan, write_plan


async def local(config: Config, payload: dict | None = None) -> dict:
    connector = aiohttp.UnixConnector(path=str(config.socket))
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=600 if payload is not None else 10)) as client:
        method = "POST" if payload is not None else "GET"
        async with client.request(method, "http://localhost/maintenance", json=payload) as response:
            response.raise_for_status()
            return await response.json()


def staged_environment(original: str) -> str:
    values = {"DEBUG_LOCAL": "true", "LOG_LEVEL": "INFO", "LOKI_ENDPOINT": "",
              "LOKI_USER": "", "LOKI_PASSWORD": "", "LOKI_CA_BUNDLE_PATH": ""}
    # The documented runner.env uses one unquoted assignment per line. Refuse
    # continuations/multiline values rather than accidentally corrupt a secret.
    lines = original.splitlines()
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\r\n]*", line) or line.endswith("\\"):
                raise ValueError("unsupported_environment_format")
            value = line.split("=", 1)[1]
            if value.startswith(("'", '"')) and (len(value) < 2 or value[-1] != value[0]):
                raise ValueError("unsupported_environment_format")
    kept = [line for line in lines if line.split("=", 1)[0] not in values]
    return "\n".join(kept + [f"{key}={value}" for key, value in values.items()]) + "\n"


async def validate_release(plan: UpgradePlan, config: Config) -> None:
    release = Path(plan.release)
    policy = config.self_upgrade
    if (policy is None or release.is_symlink() or not release.is_absolute()
            or not release.resolve().is_relative_to(policy.releases.resolve())
            or release.resolve() == Path(__file__).resolve().parents[1]
            or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", plan.release)):
        raise ValueError("invalid_release")
    revision = await command(["git", "-C", str(release), "rev-parse", "HEAD"])
    dirty = await command(["git", "-C", str(release), "status", "--porcelain"])
    origin = await command(["git", "-C", str(release), "remote", "get-url", "origin"])
    if revision != plan.revision or dirty or origin.rstrip("/").removesuffix(".git") != "https://github.com/Really-Bad-Apps/hivemake-runner":
        raise ValueError("release_changed")
    if not (release / "runner/upgrade.py").is_file():
        raise ValueError("release_missing_upgrade_protocol")


class Upgrade:
    def __init__(self, path: Path, plan: UpgradePlan) -> None:
        self.path, self.plan = path, plan
        self.config = Config.load(path)
        self.policy = self.config.self_upgrade
        if self.policy is None:
            raise ValueError("self_upgrade_not_configured")
        self.backups = self.config.state_dir / "upgrades" / plan.run_id
        self.override = Path.home() / ".config/systemd/user" / (self.policy.service + ".d") / "90-hivemake-upgrade.conf"
        self.files = {"config": path, "environment": self.policy.environment, "override": self.override}

    async def systemctl(self, *arguments: str) -> str:
        return await command(["systemctl", "--user", *arguments])

    def prepare(self) -> None:
        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", str(self.path)):
            raise ValueError("config_path_not_systemd_safe")
        self.backups.mkdir(parents=True, mode=0o700, exist_ok=False)
        for name, path in self.files.items():
            if path.is_symlink():
                raise ValueError("refusing_symlink_configuration")
            if path.exists():
                atomic_write(self.backups / name, path.read_text())
            elif name != "override":
                raise ValueError("missing_configuration")
        data = yaml.safe_load(self.path.read_text())
        binaries = {"claude": "claude-agent-acp", "codex": "codex-acp"}
        commands = data.get("commands", {key: [value] for key, value in binaries.items()})
        for harness, args in commands.items():
            binary = binaries[harness]
            # Only the standard adapter installation is supported. Do not
            # silently replace host-specific wrappers or adapter flags.
            old = Path(__file__).resolve().parents[1] / "adapters/node_modules/.bin" / binary
            if args not in ([binary], [str(old)]):
                raise ValueError("custom_adapter_requires_manual_upgrade")
            executable = Path(self.plan.release) / "adapters/node_modules/.bin" / binary
            if not executable.is_file():
                raise ValueError("adapter_not_staged")
            commands[harness] = [str(executable)]
        data["commands"] = commands
        atomic_write(self.backups / "new-config", yaml.safe_dump(data, sort_keys=False))
        Config.load(self.backups / "new-config")
        environment = self.policy.environment.read_text()
        if self.plan.stage_logging:
            environment = staged_environment(environment)
        atomic_write(self.backups / "new-environment", environment)
        # This marker is last: incomplete preparation never qualifies for rollback.
        atomic_write(self.backups / "prepared", "1\n")

    def install(self) -> None:
        for name in ("config", "environment"):
            atomic_write(self.files[name], (self.backups / ("new-" + name)).read_text())
        self.override.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        release = self.plan.release
        atomic_write(self.override, "[Service]\nExecStart=\n"
                     f"ExecStart={release}/.venv/bin/python -m runner.daemon --config {self.path}\n"
                     f"WorkingDirectory={release}\n")

    async def rollback(self) -> None:
        await self.systemctl("stop", self.policy.service)
        for name, path in self.files.items():
            backup = self.backups / name
            if backup.exists():
                atomic_write(path, backup.read_text())
            elif name == "override":
                path.unlink(missing_ok=True)
        await self.systemctl("daemon-reload")
        await self.systemctl("start", self.policy.service)

    async def verify(self, before: dict) -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                current = await local(self.config)
                if (current["boot_id"] != before["boot_id"]
                        and current["pid"] != before["pid"]
                        and current["revision"] == self.plan.revision
                        and current["upgrade_protocol"] == UPGRADE_PROTOCOL
                        and current["runner_id"] == before["runner_id"]
                        and current["registered"]
                        and (not self.plan.stage_logging or current["debug_local"])
                        and current["agents"] == before["agents"]):
                    return
            except (aiohttp.ClientError, TimeoutError, KeyError):
                pass
            await asyncio.sleep(2)
        raise RuntimeError("upgrade_readiness_failed")

    async def run(self) -> None:
        if self.plan.phase != "ready":
            raise ValueError("upgrade_not_ready")
        changed = False
        try:
            before = await local(self.config)
            while not before["idle"]:
                await asyncio.sleep(2)
                before = await local(self.config)
            if (not before["runner_id"] or not before["registered"] or before["agents"]["errors"]
                    or not before["agents"]["agents"]
                    or not all(agent["ready"] for agent in before["agents"]["agents"])):
                raise ValueError("existing_runner_unhealthy")
            pid = await self.systemctl("show", self.policy.service, "-p", "MainPID", "--value")
            if pid != str(before["pid"]):
                raise ValueError("service_identity_mismatch")
            environment_files = await self.systemctl("show", self.policy.service, "-p", "EnvironmentFiles", "--value")
            if environment_files != f"{self.policy.environment} (ignore_errors=no)":
                raise ValueError("requires_single_required_environment_file")
            await validate_release(self.plan, self.config)
            self.prepare()
            self.plan.phase = "applying"
            write_plan(self.config.state_dir, self.plan)
            changed = True
            await self.systemctl("stop", self.policy.service)
            self.install()
            await self.systemctl("daemon-reload")
            await self.systemctl("start", self.policy.service)
            await self.verify(before)
        except Exception as exc:
            self.plan.error = type(exc).__name__
            if changed:
                try:
                    await self.rollback()
                except Exception:
                    self.plan.error = "rollback_failed"
            self.plan.phase = "failed"
            write_plan(self.config.state_dir, self.plan)
            raise RuntimeError("upgrade_failed_see_private_status") from None
        # Publishing a verified result can partially succeed (archive vs active
        # manifest). Never roll back a healthy runner after success is visible.
        self.plan.phase = "verified"
        write_plan(self.config.state_dir, self.plan)


async def execute(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    if args.action == "request":
        run_id = os.environ.get("HIVEMAKE_RUN_ID")
        if not run_id or os.environ.get("HIVEMAKE_RUNNER_SOCKET") != str(config.socket):
            raise ValueError("request_requires_worker_context")
        await local(config, {"run_id": run_id, "revision": args.revision,
                             "release": str(args.release), "stage_logging": args.stage_logging})
        print(json.dumps({"outcome": "runner_upgrade", "message": "Prepared release handed to independent restart job."}))
        return
    plan = read_plan(config.state_dir)
    if plan is None or plan.run_id != args.run_id:
        raise ValueError("upgrade_plan_mismatch")
    with (config.state_dir / "upgrade.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await Upgrade(args.config, plan).run()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("request", "apply"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--revision")
    parser.add_argument("--release", type=Path)
    parser.add_argument("--stage-logging", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    os.umask(0o077)
    asyncio.run(execute(args))


if __name__ == "__main__":
    main()
