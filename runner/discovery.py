"""Opt-in recursive discovery. Ambiguous IDs are never runnable."""

import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

SKIP = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    ".tox",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
}


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    harness: str
    workspace: str

    def to_dict(self):
        return asdict(self)


def discover(roots, commands):
    agents, errors, seen, duplicates = {}, [], set(), set()
    for root in roots:
        if root.is_symlink() or not root.is_dir():
            errors.append({"path": str(root), "error": "root_missing_or_symlink"})
            continue

        def on_error(exc):
            errors.append({"path": exc.filename, "error": "directory_unreadable"})

        for directory, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
            base = Path(directory)
            # Both conventional venvs and root-level virtual environments.
            dirs[:] = sorted(d for d in dirs if d not in SKIP and not (base / d).is_symlink())
            if "pyvenv.cfg" in files:
                dirs[:] = [d for d in dirs if d not in {"bin", "lib", "lib64", "include", "share"}]
            marker = base / "agent.yaml"
            if "agent.yaml" not in files or marker in seen:
                continue
            seen.add(marker)
            try:
                if marker.is_symlink() or marker.stat().st_size > 16384:
                    raise ValueError("marker_symlink_or_too_large")
                data = yaml.safe_load(marker.read_text())
                if not isinstance(data, dict) or set(data) - {
                    "version",
                    "id",
                    "name",
                    "harness",
                }:
                    raise ValueError("invalid_marker_fields")
                if type(data.get("version")) is not int or data["version"] != 1:
                    raise ValueError("unsupported_marker_version")
                agent_id = data.get("id")
                if not isinstance(agent_id, str) or not re.fullmatch(
                    r"[a-z0-9][a-z0-9_-]{0,63}", agent_id
                ):
                    raise ValueError("invalid_agent_id")
                harness = data.get("harness")
                if harness not in ("codex", "claude"):
                    raise ValueError("unsupported_harness")
                name = data.get("name", agent_id)
                if not isinstance(name, str) or not name.strip() or len(name) > 128:
                    raise ValueError("invalid_agent_name")
                agent = Agent(agent_id, name, harness, str(base.resolve()))
                if agent_id in agents or agent_id in duplicates:
                    agents.pop(agent_id, None)
                    duplicates.add(agent_id)
                    errors.append(
                        {
                            "path": str(marker),
                            "error": "duplicate_agent_id",
                            "agent_id": agent_id,
                        }
                    )
                else:
                    agents[agent_id] = agent
            except (OSError, UnicodeError, yaml.YAMLError, ValueError, TypeError):
                errors.append({"path": str(marker), "error": "invalid_marker"})
    rows = []
    for agent in sorted(agents.values(), key=lambda a: a.id):
        command = commands.get(agent.harness)
        ready = bool(command and shutil.which(command[0]))
        rows.append(
            {
                **agent.to_dict(),
                "ready": ready,
                "unavailable_reason": None if ready else "adapter_not_installed",
            }
        )
    return agents, {"agents": rows, "errors": errors}
