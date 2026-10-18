"""Regressions for cancellation finalization and durable-result recovery."""

import asyncio
import json
import sqlite3
import sys

import pytest
from tests_helpers import wait_finished

from runner.acp_session import ACPSession
from runner.discovery import Agent
from runner.manager import RunnerError
from runner.store import Store


class CooperativeSlowClose:
    def __init__(self, *args):
        self.done = asyncio.Event()
        self.closing = asyncio.Event()
        self.release_close = asyncio.Event()
        self.cleaned = False

    async def prompt(self, text):
        await self.done.wait()
        return "cancelled"

    async def cancel(self):
        self.done.set()

    async def close(self):
        self.closing.set()
        await self.release_close.wait()
        self.cleaned = True


async def test_grace_expiry_cannot_interrupt_cooperative_cleanup(manager):
    manager.session_factory = CooperativeSlowClose
    sid = manager.new_session("demo")["id"]
    run = manager.submit(sid, "hello")
    await asyncio.sleep(0)
    runtime = manager.sessions[sid]["runtime"]
    task = manager.tasks[run["id"]]
    manager.cancel(run["id"])
    await asyncio.wait_for(runtime.closing.wait(), 1)
    await asyncio.sleep(manager.config.cancel_grace * 2)
    assert not task.done()
    assert "demo" in manager.busy
    manager.cancel(run["id"])  # A repeated request must not undo terminal state.
    # Repeated cancellation must also leave the separately owned finalizer alive.
    task.cancel()
    await asyncio.sleep(0)
    runtime.release_close.set()
    await asyncio.wait_for(task, 2)
    assert runtime.cleaned
    assert manager.get_run(run["id"])["status"] == "cancelled"
    assert not manager.busy
    assert manager.get_session(sid)["active_run"] is None


async def test_shutdown_during_cleanup_waits_for_finalizer(manager):
    manager.session_factory = CooperativeSlowClose
    sid = manager.new_session("demo")["id"]
    run = manager.submit(sid, "hello")
    await asyncio.sleep(0)
    runtime = manager.sessions[sid]["runtime"]
    manager.cancel(run["id"])
    await asyncio.wait_for(runtime.closing.wait(), 1)
    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0.05)
    assert not shutdown.done()
    runtime.release_close.set()
    await asyncio.wait_for(shutdown, 2)
    assert runtime.cleaned
    assert not manager.busy
    assert manager.get_run(run["id"])["finished_at"] is not None


async def test_shutdown_before_execution_starts_finalizes_submission(manager):
    sid = manager.new_session("demo")["id"]
    run = manager.submit(sid, "hang")
    await manager.shutdown()
    assert manager.get_run(run["id"])["status"] == "interrupted"
    assert not manager.busy


async def test_close_survives_cancelled_waiter_and_can_be_awaited_again(tmp_path):
    # Exercise ACPSession.close itself, with a child that requires SIGKILL.
    session = ACPSession(Agent("demo", "Demo", "codex", str(tmp_path)), (), lambda e: None, 1)
    session.process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(30)",
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
    )
    await session.process.stdout.readline()
    first = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not session.closed
    await asyncio.wait_for(session.close(), 5)
    assert session.closed
    assert session.process.returncode is not None


async def test_final_write_failure_is_visible_and_recovers_without_restart(manager, monkeypatch):
    real_save = manager.store.save
    unavailable = True

    def save(run):
        if unavailable and run.get("finished_at"):
            raise sqlite3.OperationalError("disk full")
        real_save(run)

    monkeypatch.setattr(manager.store, "save", save)
    sid = manager.new_session("demo")["id"]
    run = manager.submit(sid, "hello")
    await wait_finished(manager, run["id"])
    result = manager.get_run(run["id"])
    assert result["status"] == "completed"
    assert result["response"] == "turn 1: hello"
    assert result["persistence_pending"] is True
    assert not manager.busy
    with pytest.raises(RunnerError, match="storage_unavailable") as exc:
        manager.submit(sid, "blocked while storage is unavailable")
    assert exc.value.status == 503
    unavailable = False
    # A new submission retries the pending result before accepting more work.
    followup = manager.submit(sid, "followup")
    assert not manager.pending_results
    assert manager.store.get(run["id"])["status"] == "completed"
    await wait_finished(manager, followup["id"])
    assert manager.get_run(followup["id"])["response"] == "turn 2: followup"


async def test_initial_write_failure_never_reserves_agent(manager, monkeypatch):
    def fail(run):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(manager.store, "save", fail)
    sid = manager.new_session("demo")["id"]
    with pytest.raises(RunnerError, match="storage_unavailable"):
        manager.submit(sid, "hello")
    assert not manager.busy
    assert not manager.tasks


@pytest.mark.parametrize("legacy", [False, True])
def test_recovery_only_deserializes_unfinished_runs(tmp_path, monkeypatch, legacy):
    path = tmp_path / "runs.db"
    if legacy:
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    else:
        store = Store(path)
        store.close()
        db = sqlite3.connect(path)
    for n in range(50):
        run = {"id": str(n), "status": "completed", "response": "x" * 20000}
        if legacy:
            db.execute("INSERT INTO runs VALUES (?, ?)", (run["id"], json.dumps(run)))
        else:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?)", (run["id"], json.dumps(run), run["status"])
            )
    active = {"id": "active", "status": "running"}
    if legacy:
        db.execute("INSERT INTO runs VALUES (?, ?)", (active["id"], json.dumps(active)))
    else:
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?)",
            (active["id"], json.dumps(active), active["status"]),
        )
    db.commit()
    db.close()
    real_loads = json.loads
    loaded = []

    def loads(body):
        result = real_loads(body)
        loaded.append(result["id"])
        return result

    monkeypatch.setattr(json, "loads", loads)
    store = Store(path)
    assert loaded == ["active"]
    assert store.get("active")["status"] == "interrupted"
    assert store.get("0")["response"] == "x" * 20000
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT body FROM runs WHERE status IN ('running', 'cancelling') LIMIT 1"
    ).fetchall()
    assert "runs_status" in str(plan)
    store.close()
    loaded.clear()
    store = Store(path)
    assert loaded == []
    store.close()


async def test_execution_id_survives_restart(config, tmp_path):
    from uuid import uuid4

    from tests_helpers import wait_finished

    from runner.manager import Manager
    from runner.store import Store

    path = tmp_path / "dedup.sqlite3"
    store = Store(path)
    first = Manager(config, store)
    eid = str(uuid4())
    run = await first.dispatch(eid, "demo", "hello")
    await wait_finished(first, eid)
    assert run["session_id"] not in first.sessions
    await first.shutdown()
    store.close()
    store = Store(path)
    second = Manager(config, store)
    try:
        replay = await second.dispatch(eid, "demo", "hello")
        assert replay["id"] == eid
        assert replay["response"] == "turn 1: hello"
        assert second.sessions == {}
    finally:
        await second.shutdown()
        store.close()
