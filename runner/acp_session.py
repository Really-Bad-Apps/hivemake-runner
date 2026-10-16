"""ACP transport with explicit process-group ownership and bounded cleanup."""

import asyncio
import os
import signal
from contextlib import suppress

from acp import PROTOCOL_VERSION, Client, connect_to_agent, text_block
from acp.schema import ClientCapabilities, RequestPermissionResponse


class Callbacks(Client):
    def __init__(self, emit):
        self.emit = emit

    async def session_update(self, session_id, update, **kwargs):
        self.emit(update.model_dump(mode="json", by_alias=True, exclude_none=True))

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        # Prototype policy: approve a single action, never persist broad grants.
        option = next((o for o in options if o.kind == "allow_once"), None)
        option = option or next((o for o in options if o.kind == "allow_always"), None)
        self.emit(
            {
                "sessionUpdate": "permission_decision",
                "approved": option is not None,
                "toolCallId": tool_call.tool_call_id,
            }
        )
        if option is None:
            return RequestPermissionResponse(outcome={"outcome": "cancelled"})
        return RequestPermissionResponse(
            outcome={"outcome": "selected", "optionId": option.option_id}
        )


class ACPSession:
    def __init__(self, agent, command, emit, startup_timeout):
        self.agent, self.command = agent, command
        self.callbacks = Callbacks(emit)
        self.startup_timeout = startup_timeout
        self.process = self.connection = self.session_id = None
        self.stderr_task = None
        self.closed = False
        self._close_task = None

    async def start(self):
        # Inherit the host service user's login/config environment. Never copy it
        # into the API image, REST response, or log.
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.agent.workspace,
            env={k: v for k, v in os.environ.items() if k != "HIVEMAKE_OWNER_API_KEY"},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=4 * 1024 * 1024,
        )
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self.connection = connect_to_agent(self.callbacks, self.process.stdin, self.process.stdout)
        try:
            async with asyncio.timeout(self.startup_timeout):
                initialized = await self.connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                )
                if initialized.protocol_version != PROTOCOL_VERSION:
                    raise RuntimeError("Unsupported ACP protocol version")
                session = await self.connection.new_session(
                    cwd=self.agent.workspace, mcp_servers=[]
                )
                self.session_id = session.session_id
        except BaseException:
            await self.close()
            raise

    async def _drain_stderr(self):
        # Agent diagnostics can contain prompts, credentials, and source. Drain
        # without logging; preserve structured ACP updates in the private store.
        while await self.process.stderr.read(65536):
            pass

    async def prompt(self, text):
        if self.connection is None:
            await self.start()
        result = await self.connection.prompt(session_id=self.session_id, prompt=[text_block(text)])
        return result.stop_reason

    async def cancel(self):
        if self.connection is not None and self.session_id:
            await self.connection.cancel(session_id=self.session_id)

    async def close(self):
        if self.closed:
            return
        if self._close_task is None or (
            self._close_task.done()
            and (self._close_task.cancelled() or self._close_task.exception())
        ):
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self.process is not None:
            # Include children even when the adapter parent already exited.
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 2)
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 2)
        if self.connection is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.connection.close(), 2)
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.stderr_task

        self.closed = True
