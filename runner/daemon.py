"""Host daemon with a Unix socket and optional private-network listener."""

import argparse
import asyncio
import fcntl
import logging
import os
import signal
import stat
from contextlib import suppress
from pathlib import Path

from aiohttp import web

from .config import Config
from .manager import Manager, RunnerError
from .registration import OWNER_KEY_ENV, heartbeat, identity
from .store import Store


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except RunnerError as exc:
        return web.json_response({"error": exc.code}, status=exc.status)
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid_request"}, status=400)
    except web.HTTPException:
        raise
    except Exception as exc:
        logging.getLogger(__name__).error("request_failed type=%s", type(exc).__name__)
        return web.json_response({"error": "internal_error"}, status=500)


async def body(request, field, max_length):
    if request.content_type != "application/json":
        raise RunnerError("json_required", 415)
    data = await request.json()
    if not isinstance(data, dict) or set(data) != {field}:
        raise RunnerError("invalid_request")
    value = data[field]
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise RunnerError("invalid_request")
    return value


def create_control_app(manager):
    app = web.Application(middlewares=[errors], client_max_size=256 * 1024)

    async def health(request):
        return web.json_response({"status": "ok"})

    async def agents(request):
        return web.json_response(manager.list_agents())

    async def refresh(request):
        return web.json_response(await manager.refresh_async())

    async def sessions(request):
        return web.json_response(
            manager.new_session(await body(request, "agent_id", 64)), status=201
        )

    async def session(request):
        sid = request.match_info["session_id"]
        if request.method == "DELETE":
            await manager.close_session(sid)
            return web.Response(status=204)
        return web.json_response(manager.get_session(sid))

    async def submit(request):
        result = manager.submit(
            request.match_info["session_id"], await body(request, "prompt", 65536)
        )
        return web.json_response(result, status=202)

    async def execution(request):
        if request.content_type != "application/json":
            raise RunnerError("json_required", 415)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"agent_id", "prompt"}:
            raise RunnerError("invalid_request")
        for field, limit in (("agent_id", 64), ("prompt", 65536)):
            if (
                not isinstance(data[field], str)
                or not data[field].strip()
                or len(data[field]) > limit
            ):
                raise RunnerError("invalid_request")
        result = await manager.dispatch(request.match_info["execution_id"], **data)
        return web.json_response(result, status=202)

    async def run(request):
        return web.json_response(manager.get_run(request.match_info["run_id"]))

    async def cancel(request):
        return web.json_response(manager.cancel(request.match_info["run_id"]), status=202)

    async def attachment(request):
        ident = request.match_info["attachment_id"]
        if request.method == "DELETE":
            await manager.attachments.detach(ident)
            return web.Response(status=204)
        if request.method == "GET":
            return web.json_response(manager.attachments.get(ident))
        data = await object_body(request, {"agent_id", "session_id", "expires_at"})
        return web.json_response(manager.attachments.register(ident, **data), status=202)

    async def delivery(request):
        ident = request.match_info["delivery_id"]
        if request.method == "GET":
            return web.json_response(manager.attachments.delivery(ident))
        data = await object_body(request, {"attachment_id", "message"})
        return web.json_response(manager.attachments.deliver(ident, **data), status=202)

    async def object_body(request, keys):
        if request.content_type != "application/json":
            raise RunnerError("json_required", 415)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != keys:
            raise RunnerError("invalid_request")
        return data

    app.add_routes(
        [
            web.get("/health", health),
            web.put("/attachments/{attachment_id}", attachment),
            web.get("/attachments/{attachment_id}", attachment),
            web.delete("/attachments/{attachment_id}", attachment),
            web.put("/deliveries/{delivery_id}", delivery),
            web.get("/deliveries/{delivery_id}", delivery),
            web.put("/executions/{execution_id}", execution),
            web.get("/agents", agents),
            web.post("/agents/refresh", refresh),
            web.post("/sessions", sessions),
            web.get("/sessions/{session_id}", session),
            web.delete("/sessions/{session_id}", session),
            web.post("/sessions/{session_id}/runs", submit),
            web.get("/runs/{run_id}", run),
            web.post("/runs/{run_id}/cancel", cancel),
        ]
    )
    return app


async def serve(config):
    # Remove the registration credential before creating any ACP subprocesses.
    owner_key = os.environ.pop(OWNER_KEY_ENV, "")
    if config.hivemake_url and not owner_key.startswith("hm_owner_"):
        raise ValueError("HIVEMAKE_OWNER_API_KEY is required for registration")
    os.umask(0o077)
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A second daemon must not interrupt existing runs or unlink a live socket.
    with (
        (config.state_dir / "runner.lock").open("w") as lock,
        (config.socket.parent / "socket.lock").open("w") as socket_lock,
    ):
        for handle in (lock, socket_lock):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if config.socket.exists() or config.socket.is_symlink():
            if not stat.S_ISSOCK(config.socket.lstat().st_mode):
                raise ValueError("Refusing to replace a non-socket path")
            config.socket.unlink()
        runner_id, display_name = identity(config.state_dir, config.display_name)
        registration_task = None
        discovery_task = None
        store = Store(config.state_dir / "runs.sqlite3")
        manager = Manager(config, store)
        server = web.AppRunner(create_control_app(manager), access_log=None)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        try:
            await server.setup()
            await web.UnixSite(server, str(config.socket)).start()
            os.chmod(config.socket, 0o600)
            if config.listen_host is not None:
                await web.TCPSite(server, config.listen_host, config.listen_port).start()
            logging.getLogger(__name__).info(
                "runner_ready runner_id=%s display_name=%s agents=%d",
                runner_id,
                display_name,
                len(manager.agents),
            )
            discovery_task = asyncio.create_task(manager.periodic_discovery())
            if config.hivemake_url:
                registration_task = asyncio.create_task(
                    heartbeat(config, manager, runner_id, display_name, owner_key)
                )
            await stop.wait()
        finally:
            for task in (discovery_task, registration_task):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            manager.stopping = True
            await server.cleanup()
            await manager.shutdown()
            store.close()
            config.socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="HiveMake Runner host daemon")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(Config.load(args.config)))


if __name__ == "__main__":
    main()
