"""Single-event-loop owner of sessions, admission, and run state."""

import asyncio
import json
import logging
import shutil
import sqlite3
from contextlib import suppress
from uuid import uuid4

from . import upgrade_control
from .acp_session import ACPSession
from .delivery_journal import recover_retired_deliveries
from .discovery import discover
from .instructions import InstructionError, load_instructions
from .store import now
from .upgrade_state import ACTIVE, finish_pending, read_plan, write_plan

log = logging.getLogger(__name__)
MAX_OUTPUT = 1024 * 1024
MAX_EVENTS = 1000


class RunnerError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class Manager:
    def __init__(self, config, store, session_factory=ACPSession):
        self.config, self.store, self.session_factory = config, store, session_factory
        self.sessions, self.busy, self.tasks, self.cancel_tasks = {}, {}, {}, {}
        self.runs = {}
        self.pending_results = {}
        self.inventory_changed = asyncio.Event()
        self.discovery_task = None
        self.stopping = False
        self.config_path = None
        self.boot_id = str(uuid4())
        self.runner_id = None
        self.registered = False
        self.revision = None
        plan = read_plan(config.state_dir)
        if plan and plan.phase == "requested":
            plan.phase, plan.error = "failed", "worker_interrupted_before_handoff"
            write_plan(config.state_dir, plan)
        self.refresh()
        recover_retired_deliveries(self.store.db)

    def refresh(self):
        self.agents, self.discovery = discover(
            self.config.roots, self.config.commands, self.config.instruction_profiles
        )
        return self.list_agents()

    async def refresh_async(self):
        if self.stopping or upgrade_control.active(self):
            raise RunnerError("runner_stopping", 503)
        # Callers share one scan. Cancelling an HTTP request must not cancel
        # its worker or allow a second scan to overlap the still-running thread.
        if self.discovery_task is None or self.discovery_task.done():
            self.discovery_task = asyncio.create_task(self._refresh_inventory())
            self.discovery_task.add_done_callback(self._discovery_finished)
        return await asyncio.shield(self.discovery_task)

    @staticmethod
    def _discovery_finished(task):
        # Retrieve errors even when all waiting requests have been cancelled.
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("runner_discovery_failed type=%s", type(exc).__name__)

    async def _refresh_inventory(self):
        agents, discovery = await asyncio.to_thread(
            discover,
            self.config.roots,
            self.config.commands,
            self.config.instruction_profiles,
        )
        if not self.stopping:
            self.agents, self.discovery = agents, discovery
            self.inventory_changed.set()
        return self.list_agents()

    async def periodic_discovery(self):
        while True:
            await asyncio.sleep(self.config.discovery_interval)
            try:
                await self.refresh_async()
            except Exception as exc:
                # Keep the last complete inventory and retry on the next cycle.
                log.warning("runner_discovery_failed type=%s", type(exc).__name__)

    def list_agents(self):
        maintaining = upgrade_control.active(self)
        return {
            "agents": [{**a, "busy": a["id"] in self.busy,
                         "ready": a.get("ready", False) and not maintaining} for a in self.discovery["agents"]],
            "errors": self.discovery["errors"],
        }

    def new_session(self, agent_id):
        if self.stopping or upgrade_control.active(self):
            raise RunnerError("runner_stopping", 503)
        if agent_id not in self.agents:
            raise RunnerError("agent_not_found", 404)
        if len(self.sessions) >= self.config.max_sessions:
            raise RunnerError("session_limit", 409)
        agent = self.agents[agent_id]
        command = self.config.commands.get(agent.harness)
        if not command or not shutil.which(command[0]):
            raise RunnerError("adapter_not_installed", 503)
        try:
            instructions = load_instructions(
                agent.instruction_profile, self.config.instruction_profiles
            )
        except InstructionError as exc:
            raise RunnerError(str(exc), 503) from None
        session_id = str(uuid4())
        self.sessions[session_id] = {
            "id": session_id,
            "agent": agent,
            "created_at": now(),
            "runtime": None,
            "active_run": None,
            "valid": True,
            "instructions": instructions,
        }
        return self.get_session(session_id)

    def _session(self, session_id):
        session = self.sessions.get(session_id)
        if session is None:
            raise RunnerError("session_not_found", 404)
        return session

    def get_session(self, session_id):
        s = self._session(session_id)
        return {
            "id": s["id"],
            "agent_id": s["agent"].id,
            "created_at": s["created_at"],
            "active_run": s["active_run"],
            "status": "open" if s["valid"] else "expired",
            "instructions": (
                s["instructions"].metadata(s["agent"].harness) if s["instructions"] else None
            ),
        }

    def submit(self, session_id, prompt, *, run_id=None):
        if self.stopping or upgrade_control.active(self):
            raise RunnerError("runner_stopping", 503)
        if not self._flush_pending():
            raise RunnerError("storage_unavailable", 503)
        s = self._session(session_id)
        agent = s["agent"]
        if not s["valid"]:
            raise RunnerError("session_expired", 409)
        if self.agents.get(agent.id) != agent:
            raise RunnerError("agent_configuration_changed", 409)
        if agent.id in self.busy:
            raise RunnerError("agent_busy", 409)
        # No await between admission and reservation: concurrent requests cannot
        # both start a run. Separate sessions still share the agent reservation.
        run_id = run_id or str(uuid4())
        run = {
            "id": run_id,
            "session_id": session_id,
            "agent_id": agent.id,
            "status": "running",
            "created_at": now(),
            "finished_at": None,
            "prompt": prompt,
            "response": "",
            "events": [],
            "truncated": False,
            "response_truncated": False,
            "events_truncated": False,
            "stop_reason": None,
            "error": None,
            "instructions": self.get_session(session_id)["instructions"],
        }
        try:
            self.store.save(run)
        except sqlite3.Error:
            raise RunnerError("storage_unavailable", 503) from None
        self.busy[agent.id] = run_id
        s["active_run"] = run_id
        self.runs[run_id] = run
        self.tasks[run_id] = asyncio.create_task(self._execute(s, run))
        log.info("run_started", extra={
            "run_id": run_id, "session_id": session_id, "agent_id": agent.id,
            "harness": agent.harness,
        })
        return dict(run)

    async def dispatch(self, execution_id, agent_id, prompt):
        """One durable run per caller execution UUID, including after restart."""
        from uuid import UUID

        execution_id = str(UUID(execution_id))
        existing = self.get_run_or_none(execution_id)
        if existing is not None:
            if existing["agent_id"] != agent_id or existing["prompt"] != prompt:
                raise RunnerError("execution_conflict", 409)
            return existing
        session = self.new_session(agent_id)
        self.sessions[session["id"]]["auto_close"] = True
        try:
            return self.submit(session["id"], prompt, run_id=execution_id)
        except Exception:
            await self.close_session(session["id"])
            raise

    def get_run_or_none(self, run_id):
        try:
            return self.get_run(run_id)
        except RunnerError as exc:
            if exc.code == "run_not_found":
                return None
            raise

    def _emit(self, session, event):
        run = self.runs.get(session["active_run"])
        if run is None:
            return
        if event.get("sessionUpdate") == "agent_message_chunk":
            content = event.get("content", {})
            if content.get("type") == "text":
                text = content.get("text", "")
                available = MAX_OUTPUT - len(run["response"])
                run["response"] += text[:available]
                if len(text) > available:
                    run["truncated"] = True
                    run["response_truncated"] = True
        # Bound snapshots (including unusually large tool results).
        if len(run["events"]) < MAX_EVENTS and len(json.dumps(event)) <= 16384:
            run["events"].append(event)
        else:
            run["truncated"] = True
            run["events_truncated"] = True
        self._persist(run)

    def _persist(self, run):
        try:
            self.store.save(run)
        except sqlite3.Error:
            self.pending_results[run["id"]] = run
            log.error("result_persistence_pending run_id=%s", run["id"])
            return False
        self.pending_results.pop(run["id"], None)
        return True

    def _flush_pending(self):
        for run in list(self.pending_results.values()):
            if not self._persist(run):
                return False
        return True

    async def _execute(self, session, run):
        failed = False
        try:
            if run["status"] == "cancelling":
                raise asyncio.CancelledError
            if session["runtime"] is None:
                agent = session["agent"]
                session["runtime"] = self.session_factory(
                    agent,
                    self.config.commands[agent.harness],
                    lambda event: self._emit(session, event),
                    self.config.startup_timeout,
                    session["instructions"],
                )
            if session.get("auto_close") and self.config.self_upgrade:
                session["runtime"].maintenance_env = {
                    "HIVEMAKE_RUN_ID": run["id"],
                    "HIVEMAKE_RUNNER_SOCKET": str(self.config.socket),
                }
            async with asyncio.timeout(self.config.run_timeout):
                reason = await session["runtime"].prompt(run["prompt"])
            run["stop_reason"] = reason
            run["status"] = (
                "cancelled"
                if run["status"] == "cancelling" or reason == "cancelled"
                else "completed"
            )
        except asyncio.CancelledError:
            run["status"] = "interrupted" if self.stopping else "cancelled"
            run["error"] = "runner_stopped" if self.stopping else None
            failed = True
        except TimeoutError:
            run.update(status="failed", error="run_timeout")
            failed = True
        except Exception as exc:
            # Do not leak provider errors containing local config or tokens.
            run.update(status="failed", error="adapter_error", error_type=type(exc).__name__)
            log.warning("adapter_error run_id=%s type=%s", run["id"], type(exc).__name__)
            failed = True
        finally:
            # Own finalization independently: cancellation may stop prompt work,
            # but must never interrupt process cleanup or admission release.
            finalizer = asyncio.create_task(self._finalize(session, run, failed))
            while not finalizer.done():
                try:
                    await asyncio.shield(finalizer)
                except asyncio.CancelledError:
                    continue
            await finalizer

    async def _finalize(self, session, run, failed):
        if failed or run["status"] in ("cancelled", "interrupted"):
            session["valid"] = False
            if session["runtime"] is not None:
                await session["runtime"].close()
        # Keep an unpersisted result available in memory, and reject further
        # work while storage is unavailable instead of accumulating results.
        try:
            await upgrade_control.handoff(self, run)
        except Exception:
            # Never launch a restart when persistence or handoff failed.
            run.update(finish_pending(run, None))
            # A state I/O failure must not bypass runtime cleanup or retain
            # the busy reservation forever. Admission remains closed if the
            # durable plan could not be repaired.
            with suppress(OSError, ValueError, TypeError):
                plan = read_plan(self.config.state_dir)
                if plan and plan.run_id == run["id"]:
                    plan.phase, plan.error = "failed", "handoff_failed"
                    write_plan(self.config.state_dir, plan)
        if not run.get("upgrade_pending"):
            run["finished_at"] = now()
        self._persist(run)
        self.busy.pop(session["agent"].id, None)
        session["active_run"] = None
        self.runs.pop(run["id"], None)
        if session.get("auto_close"):
            await self.close_session(session["id"])
        self.tasks.pop(run["id"], None)
        log.info("run_finished run_id=%s status=%s", run["id"], run["status"], extra={
            "run_id": run["id"], "session_id": run["session_id"],
            "agent_id": run["agent_id"], "status": run["status"],
            "error_code": run["error"], "error_type": run.get("error_type"),
        })

    def get_run(self, run_id):
        if run_id in self.pending_results:
            run = self.pending_results[run_id]
            persisted = self._persist(run)
            return {**run, "persistence_pending": not persisted}
        try:
            run = self.store.get(run_id)
        except sqlite3.Error:
            raise RunnerError("storage_unavailable", 503) from None
        if run is None:
            raise RunnerError("run_not_found", 404)
        if run.get("upgrade_pending"):
            plan = read_plan(self.config.state_dir, run_id)
            if plan is None or plan.run_id != run_id or plan.phase not in ACTIVE:
                run = finish_pending(run, plan)
                return {**run, "persistence_pending": not self._persist(run)}
        return run

    def cancel(self, run_id):
        # In-memory state is authoritative while a task is finishing. The
        # persisted copy can still say "cancelling" during process cleanup.
        current = self.runs.get(run_id)
        if current is None:
            return self.get_run(run_id)
        if current["status"] not in ("running", "cancelling"):
            return dict(current)
        current["status"] = "cancelling"
        self._persist(current)
        if run_id not in self.cancel_tasks:
            task = asyncio.create_task(self._cancel(run_id))
            self.cancel_tasks[run_id] = task
            task.add_done_callback(lambda _: self.cancel_tasks.pop(run_id, None))
        return dict(current)

    async def _cancel(self, run_id):
        task = self.tasks.get(run_id)
        if task is None:
            return
        session = self.sessions[self.runs[run_id]["session_id"]]
        try:
            async with asyncio.timeout(self.config.cancel_grace):
                if session["runtime"] is not None:
                    with suppress(Exception):
                        await session["runtime"].cancel()
                await asyncio.shield(task)
        except TimeoutError:
            # The adapter did not acknowledge cancellation. Kill its process
            # group and invalidate the conversation, then release admission.
            task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.shield(task)

    async def close_session(self, session_id):
        s = self._session(session_id)
        if s["active_run"] is not None:
            raise RunnerError("session_busy", 409)
        # Reserve before awaiting teardown to prevent a concurrent submit.
        s["valid"] = False
        if s["runtime"] is not None:
            await s["runtime"].close()
        self.sessions.pop(session_id, None)

    async def shutdown(self):
        self.stopping = True
        for task in list(self.cancel_tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.cancel_tasks.values()), return_exceptions=True)
        # Let newly submitted execution tasks enter their try/finally before
        # cancelling them; a task cancelled before its first step never runs it.
        await asyncio.sleep(0)
        active = list(self.tasks.values())
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        for s in self.sessions.values():
            if s["runtime"] is not None:
                await s["runtime"].close()

        self._flush_pending()
        if self.discovery_task is not None:
            await asyncio.gather(self.discovery_task, return_exceptions=True)
