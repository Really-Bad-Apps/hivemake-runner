from flask import current_app
from flask.views import MethodView
from flask_smorest import Blueprint
from marshmallow import Schema, fields, validate

blp = Blueprint("runner", __name__, description="Host agent sessions and runs")


class SessionInput(Schema):
    agent_id = fields.String(required=True, validate=validate.Regexp(r"^[a-z0-9][a-z0-9_-]{0,63}$"))


class RunInput(Schema):
    prompt = fields.String(
        required=True,
        validate=[validate.Length(min=1, max=65536), validate.Regexp(r"(?s).*\S")],
    )


def forward(method, path, data=None):
    return current_app.extensions["runner_client"].request(method, path, data)


@blp.route("/health")
class Health(MethodView):
    def get(self):
        """Readiness: both API and host runner must be reachable."""
        return forward("GET", "/health")


@blp.route("/agents")
class Agents(MethodView):
    def get(self):
        """List discovered agents, adapter availability, and discovery errors."""
        return forward("GET", "/agents")


@blp.route("/agents/refresh")
class Refresh(MethodView):
    def post(self):
        """Rescan configured roots."""
        return forward("POST", "/agents/refresh")


@blp.route("/sessions")
class Sessions(MethodView):
    @blp.arguments(SessionInput)
    def post(self, data):
        """Create a fresh conversation; adapter starts on the first run."""
        return forward("POST", "/sessions", data)


@blp.route("/sessions/<uuid:session_id>")
class Session(MethodView):
    def get(self, session_id):
        return forward("GET", f"/sessions/{session_id}")

    def delete(self, session_id):
        """Close an idle session and release its adapter process."""
        return forward("DELETE", f"/sessions/{session_id}")


@blp.route("/sessions/<uuid:session_id>/runs")
class Submit(MethodView):
    @blp.arguments(RunInput)
    def post(self, data, session_id):
        """Start a run, or return 409 agent_busy without queueing."""
        return forward("POST", f"/sessions/{session_id}/runs", data)


@blp.route("/runs/<uuid:run_id>")
class Run(MethodView):
    def get(self, run_id):
        return forward("GET", f"/runs/{run_id}")


@blp.route("/runs/<uuid:run_id>/cancel")
class Cancel(MethodView):
    def post(self, run_id):
        return forward("POST", f"/runs/{run_id}/cancel")
