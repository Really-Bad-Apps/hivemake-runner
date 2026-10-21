"""Diagnostic only: instruction/MCP preservation and native busy-turn behavior.

Use a disposable terminal with the fixture in docs/TERMINAL_COMPATIBILITY.md.
The optional busy probe deliberately submits during native work; never use it
against a real human task. No public attachment or delivery API is enabled.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner.terminal_session import CodexTerminalSession  # noqa: E402
from scripts.probe_codex_connector import write_report  # noqa: E402


def audit_events(path):
    if not path.exists():
        return []
    # Ignore an incomplete final append while the fixture is writing.
    lines = path.read_text().splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith("\n")]


async def probe(args):
    events = []
    report = {"thread": args.thread}

    def emit(update):
        events.append({"time": time.time(), "update": update})

    session = CodexTerminalSession(
        session_id=args.thread, workspace=args.cwd.resolve(),
        command=[str(args.adapter.resolve())], socket_path=args.socket.resolve(), emit=emit,
    )
    try:
        await session.start()
        report["after_load_events"] = list(events)
        events.clear()
        report["context_stop"] = await asyncio.wait_for(session.prompt(
            'PROBE_CONTEXT acp: call connector_context with stage="acp" and delay=0. '
            'Report instruction fingerprints and the tool result. '
            'Do not read files or use other tools.'
        ), 90)
        report["context_events"] = list(events)
        events.clear()
        if args.busy_audit:
            baseline = len(audit_events(args.busy_audit))
            print('Now ask the disposable terminal to call connector_context with '
                  'stage="busy", delay=15, then say NATIVE_BUSY_COMPLETE.', flush=True)
            async with asyncio.timeout(90):
                while not any(
                    event.get("stage") == "busy" and event.get("event") == "start"
                    for event in audit_events(args.busy_audit)[baseline:]
                ):
                    await asyncio.sleep(0.05)
            report["busy_observed_events"] = list(events)
            events.clear()
            report["submitted_at"] = time.time()
            report["busy_stop"] = await asyncio.wait_for(session.prompt(
                'Reply exactly ACP_BUSY_PROBE. Do not call tools or alter files.'
            ), 45)
            report["finished_at"] = time.time()
            report["busy_events"] = list(events)
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        await session.close()
        report["last_events"] = list(events)
        write_report(args.report, report)
    print("Diagnostic recorded. Inspect the terminal and MCP audit; this is not a release-gate verdict.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True)
    for name in ("cwd", "adapter", "socket", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--busy-audit", type=Path)
    parser.add_argument("--ack-disposable-session", required=True, action="store_true")
    asyncio.run(probe(parser.parse_args()))


if __name__ == "__main__":
    main()
