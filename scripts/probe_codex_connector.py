"""Opt-in live compatibility probe. Only target a disposable native CLI session.

This intentionally does NOT register an attachment or expose a delivery API.
Reads Codex's local state database only as test evidence, never as routing logic.
"""

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import sys
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

from acp import PROTOCOL_VERSION, connect_to_agent, text_block
from acp.schema import ClientCapabilities, RequestPermissionResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner.terminal_session import CodexTerminalSession, TerminalCallbacks  # noqa: E402


class Observer(TerminalCallbacks):
    def __init__(self, human_approval=False):
        super().__init__(lambda update: None)
        self.text = []
        self.permission_requests = 0
        self.human_approval = human_approval

    async def session_update(self, session_id, update, **kwargs):
        if getattr(update, "session_update", None) == "agent_message_chunk":
            self.text.append(getattr(update.content, "text", ""))

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        self.permission_requests += 1
        if self.human_approval:
            # Do not race the native terminal's human decision with a denial
            # or an automatic grant from the diagnostic ACP client.
            print("ACP_PERMISSION_PENDING", flush=True)
            return await super().request_permission(session_id, tool_call, options, **kwargs)
        return RequestPermissionResponse(outcome={"outcome": "cancelled"})


def snapshot(args):
    with sqlite3.connect(args.state_db.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT cwd, sandbox_policy, approval_mode, model, reasoning_effort "
            "FROM threads WHERE id = ?", (args.thread,),
        ).fetchone()
    if row is None or Path(row[0]).resolve() != args.cwd.resolve():
        raise ValueError("test thread must exist and match the supplied workspace")
    return dict(zip(["cwd", "sandbox_policy", "approval_mode", "model", "effort"], row))


def workspace_policy_applied(snapshot):
    """Diagnostic for the pinned Codex managed filesystem-policy representation."""
    policy = json.loads(snapshot.get("sandbox_policy", "{}"))
    fs = policy.get("file_system", {})
    entries = fs.get("entries", [])
    return (
        snapshot.get("approval_mode") == "on-request"
        and policy.get("type") == "managed"
        and policy.get("network") == "restricted"
        and fs.get("type") == "restricted"
        and any(entry.get("access") == "write" and entry.get("path") == {
            "type": "path", "path": snapshot.get("cwd"),
        } for entry in entries)
        and all(entry.get("path") in (
            {"type": "path", "path": snapshot.get("cwd")},
            {"type": "special", "value": {"kind": "slash_tmp"}},
            {"type": "special", "value": {"kind": "tmpdir"}},
        ) for entry in entries if entry.get("access") == "write")
    )


def write_report(path, report):
    """Publish private diagnostic evidence atomically, including when replacing."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(json.dumps(report, indent=2) + "\n")
            out.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


async def probe(args):
    report = {"thread": args.thread, "before": snapshot(args)}
    observer = Observer(args.human_approval or args.workspace_access)
    env = {k: v for k, v in os.environ.items() if k != "HIVEMAKE_OWNER_API_KEY"}
    env.update(
        CODEX_PATH=str(Path(__file__).with_name("codex-connector").resolve()),
        HIVEMAKE_CODEX_SOCKET=str(args.socket.resolve()),
        PATH=str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
    )
    # Test documented configuration overrides as well as default behavior.
    if args.preserve_read_only:
        env["INITIAL_AGENT_MODE"] = "read-only"
        env["CODEX_CONFIG"] = json.dumps({"sandbox_mode": "read-only", "approval_policy": "never"})
    terminal = process = connection = None
    try:
        if args.workspace_access:
            terminal = CodexTerminalSession(
                session_id=args.thread, workspace=args.cwd.resolve(),
                command=[str(args.adapter.resolve())], socket_path=args.socket.resolve(),
                emit=lambda update: None,
            )
            terminal.callbacks = observer
            await terminal.start()
            connection = terminal.connection
        else:
            process = await asyncio.create_subprocess_exec(
                str(args.adapter.resolve()), cwd=args.cwd, env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=8 * 1024 * 1024,
            )
            connection = connect_to_agent(observer, process.stdin, process.stdout)
        async with asyncio.timeout(90):
            if terminal:
                report["adapter"] = terminal.agent_info.model_dump(mode="json")
            else:
                initialized = await connection.initialize(
                    protocol_version=PROTOCOL_VERSION, client_capabilities=ClientCapabilities(),
                )
                report["adapter"] = initialized.agent_info.model_dump(mode="json")
                await connection.load_session(cwd=str(args.cwd.resolve()), session_id=args.thread)
            report["after_load"] = snapshot(args)
            observer.text.clear()
            marker = "CONNECTOR_RECEIVED_" + uuid.uuid4().hex[:12]
            prompt = (
                args.prompt.read_text() if args.prompt else
                "Reply exactly " + marker + ". Do not use tools or change files."
            )
            if terminal:
                stop_reason = await terminal.prompt(prompt)
            else:
                result = await connection.prompt(session_id=args.thread, prompt=[text_block(prompt)])
                stop_reason = result.stop_reason
            report.update(
                marker=marker, stop_reason=stop_reason,
                received="".join(observer.text).strip() == marker,
                permission_requests=observer.permission_requests,
                response="".join(observer.text),
                after_prompt=snapshot(args),
            )
    finally:
        # EOF/disconnect only: no session/close, cancel, or server shutdown RPC.
        if terminal:
            await terminal.close()
        elif process is not None:
            process.stdin.close()
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 3)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, sig)
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 2)
            if connection is not None:
                with suppress(Exception):
                    await asyncio.wait_for(connection.close(), 2)
        report["after_detach"] = snapshot(args)
        report["settings_preserved"] = all(
            report.get(stage) == report["before"]
            for stage in ("after_load", "after_prompt", "after_detach")
        )
        report["workspace_policy_applied"] = all(
            workspace_policy_applied(report.get(stage, {}))
            for stage in ("after_prompt", "after_detach")
        )
        report["other_recorded_settings_preserved"] = all(
            all(report.get(stage, {}).get(key) == report["before"][key]
                for key in ("cwd", "model", "effort"))
            for stage in ("after_load", "after_prompt", "after_detach")
        )
        write_report(args.report, report)
    print(json.dumps({key: report.get(key) for key in (
        "adapter", "marker", "received", "stop_reason", "settings_preserved",
        "workspace_policy_applied", "other_recorded_settings_preserved",
    )}))
    policy_ok = (
        report["workspace_policy_applied"] and report["other_recorded_settings_preserved"]
        if args.workspace_access else report["settings_preserved"]
    )
    return 0 if report.get("received") and policy_ok else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True)
    for flag in ("cwd", "adapter", "socket", "state-db", "report"):
        parser.add_argument("--" + flag, type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preserve-read-only", action="store_true")
    modes.add_argument("--workspace-access", action="store_true")
    parser.add_argument("--human-approval", action="store_true")
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--ack-disposable-session", action="store_true", required=True)
    return asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
