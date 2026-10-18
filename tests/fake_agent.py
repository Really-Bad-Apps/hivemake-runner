"""Credential-free ACP peer for real stdio integration tests."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from acp import Agent, InitializeResponse, NewSessionResponse, PromptResponse, run_agent
from acp.schema import (
    AgentMessageChunk,
    PermissionOption,
    TextContentBlock,
    ToolCallUpdate,
)


class Echo(Agent):
    def __init__(self):
        self.counts = {}
        self.cancelled = asyncio.Event()

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version, **kwargs):
        return InitializeResponse(protocolVersion=protocol_version)

    async def new_session(self, cwd, mcp_servers, **kwargs):
        sid = str(uuid4())
        self.counts[sid] = 0
        self.cwd = Path(cwd)
        self.session_options = kwargs
        return NewSessionResponse(sessionId=sid)

    async def prompt(self, session_id, prompt, **kwargs):
        self.counts[session_id] += 1
        text = prompt[-1].text
        if text == "inspect_profile":
            text = json.dumps({
                "options": self.session_options,
                "blocks": [block.text for block in prompt],
            })
        if text == "crash":
            sys.exit(7)
        if text == "hang":
            await asyncio.sleep(100)
        if text == "child":
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time;time.sleep(100)"
            )
            (self.cwd / "child.pid").write_text(str(child.pid))
            await asyncio.sleep(100)
        if text == "permission":
            permission = await self.conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(toolCallId="edit"),
                options=[
                    PermissionOption(optionId="yes", name="Allow once", kind="allow_once"),
                    PermissionOption(optionId="no", name="Reject", kind="reject_once"),
                ],
            )
            assert permission.outcome.option_id == "yes"
            (self.cwd / "approved.txt").write_text("approved")
        # Large stderr output must not deadlock the parent.
        sys.stderr.write("diagnostic\n" * 10000)
        sys.stderr.flush()
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                sessionUpdate="agent_message_chunk",
                content=TextContentBlock(
                    type="text", text=f"turn {self.counts[session_id]}: {text}"
                ),
            ),
        )
        return PromptResponse(stopReason="end_turn")

    async def cancel(self, session_id, **kwargs):
        # Deliberately uncooperative so the runner's fallback is exercised.
        pass


if __name__ == "__main__":
    asyncio.run(run_agent(Echo()))
