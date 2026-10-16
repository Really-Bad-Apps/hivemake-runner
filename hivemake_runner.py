"""Stateless Flask gateway; agent processes always belong to the host runner."""

import os
from urllib.parse import urlsplit

import httpx
from flask import Flask
from flask_smorest import Api

from blueprints.agents import blp


class RunnerClient:
    def __init__(self, socket=None, url=None):
        self.socket = socket if not url else None
        self.base_url = "http://runner"
        if url:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "http"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("RUNNER_URL must be an http origin without credentials or a path")
            self.base_url = url.rstrip("/")
        elif not socket:
            raise ValueError("A runner socket or URL is required")

    def request(self, method, path, data=None):
        try:
            # Short requests only: prompts run asynchronously on the host.
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=self.socket, trust_env=False),
                timeout=15,
                trust_env=False,
            ) as client:
                response = client.request(method, self.base_url + path, json=data)
            if response.status_code == 204:
                return "", 204
            return response.json(), response.status_code
        except httpx.TimeoutException:
            # A timed-out submission may already have been accepted. Never retry
            # automatically: a retry could run a task twice.
            return {"error": "runner_timeout", "outcome_unknown": method == "POST"}, 504
        except (httpx.HTTPError, ValueError):
            return {"error": "runner_unavailable"}, 503


def create_app(client=None):
    app = Flask(__name__)
    app.config.update(
        API_TITLE="HiveMake Runner",
        API_VERSION="v1",
        OPENAPI_VERSION="3.0.3",
        MAX_CONTENT_LENGTH=256 * 1024,
    )
    app.extensions["runner_client"] = client or RunnerClient(
        os.environ.get("RUNNER_SOCKET", "/run/hivemake-runner/runner.sock"),
        url=os.environ.get("RUNNER_URL"),
    )
    Api(app).register_blueprint(blp)
    return app
