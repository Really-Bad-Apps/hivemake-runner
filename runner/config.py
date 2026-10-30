"""Explicit local configuration; no credentials or infrastructure defaults."""

import ipaddress
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .instructions import PROFILE_NAME
from .upgrade_state import UpgradePolicy


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
    listen_host: str | None = None
    listen_port: int = 7184
    hivemake_url: str | None = None
    display_name: str | None = None
    instruction_profiles: dict[str, Path] = field(default_factory=dict)
    discovery_interval: float = 60
    self_upgrade: UpgradePolicy | None = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Runner configuration must be a mapping")
        allowed = {
            "roots",
            "codex_socket",
            "socket",
            "state_dir",
            "commands",
            "discovery_interval",
            "startup_timeout",
            "run_timeout",
            "cancel_grace",
            "max_sessions",
            "listen_host",
            "listen_port",
            "hivemake_url",
            "display_name",
            "instruction_profiles",
            "self_upgrade",
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
        if "self_upgrade" in data:
            options["self_upgrade"] = UpgradePolicy.parse(data["self_upgrade"])
        if data.get("codex_socket") is not None:
            logging.getLogger(__name__).warning(
                "codex_socket is retired and ignored; remove it from runner configuration"
            )
        profiles = data.get("instruction_profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("instruction_profiles must be a mapping")
        resolved_profiles = {}
        for name, profile in profiles.items():
            if (
                not isinstance(name, str)
                or not PROFILE_NAME.fullmatch(name)
                or not isinstance(profile, dict)
                or set(profile) != {"file"}
            ):
                raise ValueError("Each instruction profile must have a valid name and file")
            resolved_profiles[name] = absolute(profile["file"])
        options["instruction_profiles"] = resolved_profiles
        for name in (
            "discovery_interval",
            "startup_timeout",
            "run_timeout",
            "cancel_grace",
            "max_sessions",
        ):
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
        host = data.get("listen_host")
        port = data.get("listen_port", 7184)
        if host is not None:
            address = ipaddress.ip_address(host)
            if not (address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")):
                raise ValueError("listen_host must be a loopback or Tailscale IPv4 address")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("listen_port must be an integer from 1 to 65535")
        name = data.get("display_name")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
            raise ValueError("display_name must contain 1–200 characters")
        url = data.get("hivemake_url")
        if url is not None:
            if not isinstance(url, str):
                raise ValueError("hivemake_url must be an origin")
            parsed = urlsplit(url)
            _ = parsed.port  # Validate malformed/out-of-range ports at startup.
            # Send owner credentials only over HTTPS, or encrypted Tailscale.
            private_http = False
            try:
                ip = ipaddress.ip_address(parsed.hostname or "")
                private_http = ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")
            except ValueError:
                pass
            if (
                parsed.scheme not in ("https", "http")
                or not parsed.hostname
                or (parsed.scheme == "http" and not private_http)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("hivemake_url must be an HTTPS or Tailscale HTTP origin")
            if host is None or ipaddress.ip_address(host) not in ipaddress.ip_network(
                "100.64.0.0/10"
            ):
                raise ValueError("Registration requires a Tailscale IPv4 listen_host")
            if port < 1024:
                raise ValueError("Registration requires listen_port >= 1024")
            url = url.rstrip("/")
        options.update(listen_host=host, listen_port=port, hivemake_url=url, display_name=name)
        return cls(
            tuple(absolute(r) for r in roots),
            absolute(data.get("socket")),
            absolute(data.get("state_dir")),
            {k: tuple(v) for k, v in commands.items()},
            **options,
        )
