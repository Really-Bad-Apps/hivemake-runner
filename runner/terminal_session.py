"""Experimental ACP connection to an explicitly selected native Codex session.

Not an autonomous ACPSession: never creates a session, injects ticket profiles,
approves tools, or sends cancellation/close RPCs to the shared native server.
The caller must validate ownership and liveness before using this transport.
No public attachment or delivery endpoint is enabled by this module.
"""

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from acp import PROTOCOL_VERSION, Client, connect_to_agent, text_block
from acp.schema import ClientCapabilities


def terminal_environment(socket_path):
    socket_path = Path(socket_path)
    if not socket_path.is_absolute():
        raise ValueError("An absolute existing Codex socket is required")
    env = {k: v for k, v in os.environ.items() if k != "HIVEMAKE_OWNER_API_KEY"}
    env.update(
        CODEX_PATH=str(Path(__file__).resolve().parent.parent / "scripts/codex-connector"),
        HIVEMAKE_CODEX_SOCKET=str(socket_path),
        PATH=str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        INITIAL_AGENT_MODE="workspace-write",
        CODEX_CONFIG=json.dumps({
            "sandbox_mode": "workspace-write", "approval_policy": "on-request",
        }),
    )
    return env


class TerminalCallbacks(Client):
    def __init__(self, emit):
        self.emit = emit
        self.pending_permissions = set()

    async def session_update(self, session_id, update, **kwargs):
        self.emit(update.model_dump(mode="json", by_alias=True, exclude_none=True))

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        # The shared server also presents this request in the native terminal.
        # Any response here would race (and possibly override) the human there.
        pending = asyncio.get_running_loop().create_future()
        self.pending_permissions.add(pending)
        try:
            self.emit({"sessionUpdate": "terminal_permission_pending"})
            await pending
        finally:
            self.pending_permissions.discard(pending)

    def retire_permissions(self):
        # Cancel local callback tasks after the native turn has finished. Do not
        # return ACP's cancelled outcome: that would be a permission decision.
        for pending in tuple(self.pending_permissions):
            pending.cancel()


class CodexTerminalSession:
    def __init__(self, *, session_id, workspace, command, socket_path, emit,
                 startup_timeout=60):
        self.session_id = str(UUID(session_id))
        self.workspace = Path(workspace)
        if not self.workspace.is_absolute() or not self.workspace.is_dir():
            raise ValueError("An existing absolute workspace is required")
        self.command = tuple(command)
        if not self.command:
            raise ValueError("An adapter command is required")
        self.env = terminal_environment(socket_path)
        self.callbacks = TerminalCallbacks(emit)
        self.startup_timeout = startup_timeout
        self.process = self.connection = None
        self.closed = False
        self.ready = False
        self.agent_info = None
        self._busy = False
        self._started = False
        self._spawn_task = None
        self._close_task = None

    async def start(self):
        if self.closed or self._started:
            raise RuntimeError("Terminal connection has already been started or closed")
        self._started = True
        try:
            self._spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                *self.command, cwd=self.workspace, env=self.env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
                limit=8 * 1024 * 1024,
            ))
            # Keep ownership of a child even if startup is cancelled while the
            # subprocess factory is yielding. close() joins and reaps it.
            self.process = await asyncio.shield(self._spawn_task)
            if self.closed:
                # Detach may have completed while subprocess creation yielded.
                # Reap the newly returned child before any session RPC is sent.
                raise RuntimeError("Terminal connection was detached during startup")
            self.connection = connect_to_agent(
                self.callbacks, self.process.stdin, self.process.stdout,
            )
            async with asyncio.timeout(self.startup_timeout):
                initialized = await self.connection.initialize(
                    protocol_version=PROTOCOL_VERSION, client_capabilities=ClientCapabilities(),
                )
                if initialized.protocol_version != PROTOCOL_VERSION:
                    raise RuntimeError("Unsupported ACP protocol version")
                self.agent_info = initialized.agent_info
                if not initialized.agent_capabilities.load_session:
                    raise RuntimeError("Adapter does not support loading sessions")
                await self.connection.load_session(
                    cwd=str(self.workspace), session_id=self.session_id,
                )
                await self.connection.set_session_mode(
                    session_id=self.session_id, mode_id="workspace-write",
                )
                if self.closed:
                    raise RuntimeError("Terminal connection was detached during startup")
                self.ready = True
        except BaseException:
            await self.close()
            raise

    async def prompt(self, text):
        if not self.ready or self.closed:
            raise RuntimeError("Terminal connection is not ready")
        if self._busy:
            raise RuntimeError("Terminal connection already has a pending prompt")
        # This excludes concurrent calls through THIS connection, not native
        # human turns. Public delivery must additionally prove native idle state.
        self._busy = True
        try:
            result = await self.connection.prompt(
                session_id=self.session_id, prompt=[text_block(text)],
            )
            return result.stop_reason
        except BaseException:
            # Submission may have reached Codex. Never auto-retry or cancel its
            # turn. Detach this transport; caller must record uncertain delivery.
            await self.close()
            raise
        finally:
            self._busy = False
            self.callbacks.retire_permissions()

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self.ready = False
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self._spawn_task is not None:
            try:
                self.process = await asyncio.shield(self._spawn_task)
            except Exception:
                pass  # Spawn failed; there is no child to reap.
        if self.process is not None:
            self.process.stdin.close()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 3)
            # Only our new process group, never the existing Codex server.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, sig)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.process.wait(), 2)
        self.callbacks.retire_permissions()
        if self.connection is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.connection.close(), 2)
