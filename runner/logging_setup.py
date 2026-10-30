"""Host-labelled daemon logs, with private ACP content kept out of Loki."""

import logging
import os
import pwd
import socket
import sys

from byteforge_loki_logging import LokiJsonFormatter, configure_logging


class RunnerLogContext(logging.Filter):
    def __init__(self, hostname: str, os_user: str) -> None:
        super().__init__()
        self.hostname = hostname
        self.os_user = os_user

    def filter(self, record: logging.LogRecord) -> bool:
        record.hostname = self.hostname
        record.os_user = self.os_user
        return True


class RunnerOnly(logging.Filter):
    """Third-party protocol diagnostics may contain prompts and credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "__main__" or record.name.startswith("runner.")


def setup_logging() -> None:
    hostname = socket.gethostname()
    application = f"hivemake-runner-{hostname}"
    local = os.environ.get("DEBUG_LOCAL", "true").lower()
    if local not in {"true", "false"}:
        raise ValueError("DEBUG_LOCAL must be true or false")
    handler = configure_logging(
        application_tag=application,
        debug_local=local == "true",
        local_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    root = logging.getLogger()
    if handler is not None:
        # Keep systemd's journal useful even when remote shipping is healthy.
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(LokiJsonFormatter())
        root.addHandler(console)
        handler.addFilter(RunnerOnly())
    context = RunnerLogContext(hostname, pwd.getpwuid(os.geteuid()).pw_name)
    for output in root.handlers:
        output.addFilter(context)
    logging.getLogger(__name__).info(
        "runner_logging_configured",
        extra={"application": application, "loki_enabled": handler is not None},
    )
