"""Stateless Flask gateway; agent processes always belong to the host runner."""

import os

import httpx
from flask import Flask
from flask_smorest import Api

from blueprints.agents import blp


class RunnerClient:
    def __init__(self, socket):
        self.socket = socket

    def request(self, method, path, data=None):
        try:
            # Short requests only: prompts run asynchronously on the host.
            with httpx.Client(transport=httpx.HTTPTransport(uds=self.socket), timeout=15) as client:
                response = client.request(method, "http://runner" + path, json=data)
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
        os.environ.get("RUNNER_SOCKET", "/run/hivemake-runner/runner.sock")
    )
    Api(app).register_blueprint(blp)
    return app
