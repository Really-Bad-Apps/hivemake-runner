import asyncio
import json
import sys
from uuid import uuid4

import pytest

from runner.terminal_session import (
    CodexTerminalSession,
    TerminalCallbacks,
    terminal_environment,
)


@pytest.fixture
def adapter(tmp_path):
    script = tmp_path / "adapter.py"
    script.write_text('''
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    with open("calls.jsonl", "a") as out:
        out.write(json.dumps(request) + "\\n")
    if "id" not in request:
        continue
    method = request["method"]
    result = {}
    error = None
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                  "agentInfo": {"name": "test", "version": "1"}}
    elif method == "session/load" and "fail" in sys.argv:
        error = {"code": -32602, "message": "missing session"}
    elif method == "session/prompt":
        if request["params"]["prompt"][0]["text"] == "wait":
            time.sleep(0.3)
        result = {"stopReason": "end_turn"}
    response = {"jsonrpc": "2.0", "id": request["id"]}
    response.update({"error": error} if error else {"result": result})
    print(json.dumps(response), flush=True)
''')
    return script


def session(tmp_path, adapter, *extra):
    return CodexTerminalSession(
        session_id=str(uuid4()), workspace=tmp_path,
        command=[sys.executable, str(adapter), *extra],
        socket_path=tmp_path / "existing.sock", emit=lambda event: None,
        startup_timeout=2,
    )


def calls(tmp_path):
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


async def test_retained_connection_uses_exact_session_and_never_cancels(tmp_path, adapter):
    attached = session(tmp_path, adapter)
    await attached.start()
    pid = attached.process.pid
    try:
        assert await attached.prompt("first reply") == "end_turn"
        assert await attached.prompt("second reply") == "end_turn"
        assert attached.process.pid == pid
    finally:
        await asyncio.gather(attached.close(), attached.close())
    requests = calls(tmp_path)
    assert [r["method"] for r in requests] == [
        "initialize", "session/load", "session/set_mode", "session/prompt", "session/prompt",
    ]
    assert all(r["params"]["sessionId"] == attached.session_id for r in requests[1:])
    assert requests[2]["params"]["modeId"] == "workspace-write"
    assert attached.process.returncode is not None
    with pytest.raises(RuntimeError):
        await attached.prompt("late reply")


async def test_load_failure_has_no_fallback(tmp_path, adapter):
    attached = session(tmp_path, adapter, "fail")
    with pytest.raises(Exception, match="missing session"):
        await attached.start()
    assert attached.closed
    assert attached.process.returncode is not None
    assert [r["method"] for r in calls(tmp_path)] == ["initialize", "session/load"]


@pytest.mark.parametrize("cancel_start", [False, True])
async def test_detach_during_process_start_reaps_child_without_loading_session(
    tmp_path, adapter, monkeypatch, cancel_start,
):
    real_spawn = asyncio.create_subprocess_exec
    spawning = asyncio.Event()
    release = asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        spawning.set()
        await release.wait()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    attached = session(tmp_path, adapter)
    starting = asyncio.create_task(attached.start())
    await spawning.wait()
    if cancel_start:
        starting.cancel()
    closing = asyncio.create_task(attached.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    expected = asyncio.CancelledError if cancel_start else RuntimeError
    with pytest.raises(expected):
        await starting
    await closing
    assert attached.process.returncode is not None
    assert not (tmp_path / "calls.jsonl").exists()


async def test_concurrent_prompt_is_rejected_and_cancel_detaches_only(tmp_path, adapter):
    attached = session(tmp_path, adapter)
    await attached.start()
    pending = asyncio.create_task(attached.prompt("wait"))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="pending prompt"):
        await attached.prompt("duplicate")
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert attached.closed
    methods = [r["method"] for r in calls(tmp_path)]
    assert "session/cancel" not in methods
    assert methods.count("session/prompt") <= 1


async def test_permission_callback_neither_approves_nor_denies():
    events = []
    callback = TerminalCallbacks(events.append)
    pending = asyncio.create_task(callback.request_permission("session", None, []))
    await asyncio.sleep(0)
    assert not pending.done()
    assert len(callback.pending_permissions) == 1
    assert events == [{"sessionUpdate": "terminal_permission_pending"}]
    callback.retire_permissions()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not callback.pending_permissions


def test_terminal_policy_does_not_inherit_autonomous_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVEMAKE_OWNER_API_KEY", "private-owner-key")
    monkeypatch.setenv("INITIAL_AGENT_MODE", "agent-full-access")
    monkeypatch.setenv("CODEX_CONFIG", '{"approval_policy":"never"}')
    env = terminal_environment(tmp_path / "server.sock")
    assert "HIVEMAKE_OWNER_API_KEY" not in env
    assert env["INITIAL_AGENT_MODE"] == "workspace-write"
    assert json.loads(env["CODEX_CONFIG"]) == {
        "sandbox_mode": "workspace-write", "approval_policy": "on-request",
    }
