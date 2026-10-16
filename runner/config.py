"""Explicit local configuration; no credentials or infrastructure defaults."""

import math
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Config:
    roots: tuple[Path, ...]
    socket: Path
    state_dir: Path
    commands: dict[str, tuple[str, ...]]
    startup_timeout: float = 60
    run_timeout: float = 1800
    cancel_grace: float = 5
    max_sessions: int = 32

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Runner configuration must be a mapping")
        allowed = {
            "roots",
            "socket",
            "state_dir",
            "commands",
            "startup_timeout",
            "run_timeout",
            "cancel_grace",
            "max_sessions",
        }
        if set(data) - allowed:
            raise ValueError("Unknown runner configuration fields")

        def absolute(value):
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("Configuration paths must be absolute strings")
            return Path(value)

        roots = data.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError("roots must be a nonempty list")
        commands = data.get("commands", {"codex": ["codex-acp"], "claude": ["claude-agent-acp"]})
        if not isinstance(commands, dict) or set(commands) - {"codex", "claude"}:
            raise ValueError("commands supports only codex and claude")
        for args in commands.values():
            if (
                not isinstance(args, list)
                or not args
                or not all(isinstance(x, str) and x for x in args)
            ):
                raise ValueError("Each adapter command must be a nonempty argv list")
        options = {}
        for name in ("startup_timeout", "run_timeout", "cancel_grace", "max_sessions"):
            if name in data:
                v = data[name]
                if (
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or v <= 0
                ):
                    raise ValueError(f"{name} must be positive and finite")
                if name == "max_sessions" and not isinstance(v, int):
                    raise ValueError("max_sessions must be an integer")
                options[name] = v
        return cls(
            tuple(absolute(r) for r in roots),
            absolute(data.get("socket")),
            absolute(data.get("state_dir")),
            {k: tuple(v) for k, v in commands.items()},
            **options,
        )
