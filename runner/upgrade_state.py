"""Durable, secret-free self-upgrade intent shared with an external systemd job."""

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

UPGRADE_PROTOCOL = 1
ACTIVE = {"requested", "ready", "applying"}


@dataclass(frozen=True)
class UpgradePolicy:
    service: str
    releases: Path
    environment: Path
    agents: tuple[str, ...]

    @classmethod
    def parse(cls, data: dict) -> "UpgradePolicy":
        if not isinstance(data, dict) or set(data) != {"service", "releases", "environment", "agents"}:
            raise ValueError("Invalid self_upgrade configuration")
        if not isinstance(data["service"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.service", data["service"]):
            raise ValueError("self_upgrade service must be a plain user service name")
        paths = []
        for field in ("releases", "environment"):
            value = data[field]
            if not isinstance(value, str) or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", value):
                raise ValueError("self_upgrade paths must be absolute and systemd-safe")
            paths.append(Path(value))
        agents = data["agents"]
        if not isinstance(agents, list) or not agents or not all(
            isinstance(agent, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", agent)
            for agent in agents
        ):
            raise ValueError("self_upgrade agents must be explicit local agent IDs")
        return cls(data["service"], paths[0], paths[1], tuple(agents))


@dataclass
class UpgradePlan:
    run_id: str
    revision: str
    release: str
    stage_logging: bool
    phase: str = "requested"
    updated_at: float = 0
    error: str = ""


def plan_path(state: Path) -> Path:
    return state / "upgrade.json"


def read_plan(state: Path, run_id: str | None = None) -> UpgradePlan | None:
    path = plan_path(state)
    if run_id is not None:
        result = state / "upgrade-results" / (str(UUID(run_id)) + ".json")
        if result.exists():
            path = result
    if not path.exists():
        return None
    return UpgradePlan(**json.loads(path.read_text()))


def write_plan(state: Path, plan: UpgradePlan) -> None:
    plan.updated_at = time.time()
    content = json.dumps(asdict(plan)) + "\n"
    if plan.phase in {"verified", "failed"}:
        results = state / "upgrade-results"
        results.mkdir(mode=0o700, exist_ok=True)
        atomic_write(results / (str(UUID(plan.run_id)) + ".json"), content)
    atomic_write(plan_path(state), content)


def atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def finish_pending(run: dict, plan: UpgradePlan | None) -> dict:
    success = plan is not None and plan.run_id == run["id"] and plan.phase == "verified"
    message = (
        f"Runner upgraded to {plan.revision}; new process, identity, registration and inventory verified."
        if success else "Runner upgrade did not complete safely; inspect private upgrade status and journal."
    )
    return {**run, "status": "completed", "upgrade_pending": False,
            "finished_at": time.time(), "error": None, "response_truncated": False,
            "response": json.dumps({"outcome": "resolve" if success else "needs_human", "message": message})}
