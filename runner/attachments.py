"""Explicit terminal leases and durable, non-replaying reply admission."""

import asyncio
import hashlib
import json
import logging
import time
from uuid import UUID

from .codex_live_state import validate_live_thread
from .manager import RunnerError
from .terminal_session import CodexTerminalSession


def canonical_id(value):
    if not isinstance(value, str):
        raise RunnerError("invalid_identifier")
    return str(UUID(value))


class Attachments:
    def __init__(self, manager, factory=CodexTerminalSession, validator=validate_live_thread):
        self.manager, self.db = manager, manager.store.db
        self.factory, self.validator = factory, validator
        self.sessions, self.tasks, self.busy = {}, {}, set()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS terminal_attachments "
                            "(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS terminal_deliveries "
                            "(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            self.db.execute("UPDATE terminal_attachments SET body=json_set(body, '$.status', 'expired') "
                            "WHERE json_extract(body,'$.status') IN ('pending','active')")
            self.db.execute("UPDATE terminal_deliveries SET body=json_set(body,'$.status','uncertain', "
                            "'$.error','runner_restarted') WHERE json_extract(body,'$.status')='submitted'")
            self.db.execute("UPDATE terminal_deliveries SET body=json_set(body,'$.status','failed', "
                            "'$.error','runner_restarted_before_submission') "
                            "WHERE json_extract(body,'$.status')='admitted'")

    def _save(self, table, value):
        with self.db:
            self.db.execute(f"INSERT INTO {table} VALUES (?,?) ON CONFLICT(id) "
                            "DO UPDATE SET body=excluded.body", (value["id"], json.dumps(value)))

    def _get(self, table, ident):
        ident = canonical_id(ident)
        row = self.db.execute(f"SELECT body FROM {table} WHERE id=?", (ident,)).fetchone()
        if not row:
            raise RunnerError("attachment_not_found" if table == "terminal_attachments"
                              else "delivery_not_found", 404)
        return json.loads(row[0])

    def get(self, ident):
        row = self._get("terminal_attachments", ident)
        if row["status"] in ("pending", "active") and row["expires_at"] <= time.time():
            row["status"] = "expired"
            self._save("terminal_attachments", row)
        return row

    def count(self):
        return len(self.sessions) + sum(
            kind == "attach" and not task.done() for (kind, _), task in self.tasks.items()
        )

    def register(self, ident, agent_id, session_id, expires_at):
        ident, session_id = canonical_id(ident), canonical_id(session_id)
        if self.manager.stopping:
            raise RunnerError("runner_stopping", 503)
        if self.manager.config.codex_socket is None:
            raise RunnerError("terminal_attachments_disabled", 409)
        if type(expires_at) is not int or not time.time() < expires_at <= time.time() + 86400:
            raise RunnerError("invalid_attachment_expiry")
        if not isinstance(agent_id, str):
            raise RunnerError("invalid_agent_id")
        agent = self.manager.agents.get(agent_id)
        if agent is None or agent.harness != "codex":
            raise RunnerError("terminal_project_unavailable", 409)
        body = dict(id=ident, agent_id=agent_id, session_id=session_id, expires_at=expires_at,
                    workspace=agent.workspace, status="pending")
        try:
            existing = self.get(ident)
        except RunnerError as exc:
            if exc.status != 404:
                raise
        else:
            if any(existing[k] != body[k] for k in ("agent_id", "session_id", "expires_at", "workspace")):
                raise RunnerError("attachment_conflict", 409)
            return existing
        # One attachment generation per exact native session. Do not silently
        # redirect old ticket return addresses to a replacement attachment.
        for (raw,) in self.db.execute("SELECT body FROM terminal_attachments"):
            old = json.loads(raw)
            if old["session_id"] == session_id and self.get(old["id"])["status"] in ("pending", "active"):
                raise RunnerError("terminal_already_attached", 409)
        if self.count() + len(self.manager.sessions) >= self.manager.config.max_sessions:
            raise RunnerError("attachment_capacity", 409)
        self._save("terminal_attachments", body)
        self.tasks[("attach", ident)] = asyncio.create_task(self._start(body))
        return body

    async def _start(self, row):
        session = None
        try:
            await self.validator(self.manager.config.codex_socket, row["session_id"], row["workspace"])
            session = self.factory(
                session_id=row["session_id"], workspace=row["workspace"],
                command=self.manager.config.commands["codex"],
                socket_path=self.manager.config.codex_socket, emit=lambda event: None,
                startup_timeout=self.manager.config.startup_timeout,
            )
            await session.start()
            if self.get(row["id"])["status"] != "pending":
                return
            row["status"] = "active"
            self.sessions[row["id"]] = session
            self._save("terminal_attachments", row)
            session = None
        except (Exception, asyncio.CancelledError):
            current = self.get(row["id"])
            if current["status"] == "pending":
                current.update(status="failed", error="terminal_attach_failed")
                self._save("terminal_attachments", current)
        finally:
            if session:
                await session.close()

    async def detach(self, ident):
        ident = canonical_id(ident)
        row = self.get(ident)
        row["status"] = "detached"
        self._save("terminal_attachments", row)
        if ident in self.sessions:
            await self.sessions.pop(ident).close()

    def delivery(self, ident):
        value = self._get("terminal_deliveries", ident)
        return {k: v for k, v in value.items() if k != "fingerprint"}

    def deliver(self, ident, attachment_id, message):
        ident, attachment_id = canonical_id(ident), canonical_id(attachment_id)
        if not isinstance(message, str) or not message.strip() or len(message) > 65536:
            raise RunnerError("invalid_reply")
        fingerprint = hashlib.sha256(json.dumps([attachment_id, message]).encode()).hexdigest()
        try:
            old = self._get("terminal_deliveries", ident)
        except RunnerError as exc:
            if exc.status != 404:
                raise
        else:
            if old["fingerprint"] != fingerprint:
                raise RunnerError("delivery_conflict", 409)
            return self.delivery(ident)
        if self.manager.stopping:
            raise RunnerError("runner_stopping", 503)
        row = self.get(attachment_id)
        if row["status"] != "active" or attachment_id not in self.sessions:
            raise RunnerError("attachment_inactive", 410)
        if attachment_id in self.busy:
            raise RunnerError("attachment_busy", 409)
        value = dict(id=ident, attachment_id=attachment_id, fingerprint=fingerprint, status="admitted")
        self._save("terminal_deliveries", value)
        self.busy.add(attachment_id)
        self.tasks[("deliver", ident)] = asyncio.create_task(self._deliver(value, row, message))
        return self.delivery(ident)

    async def _deliver(self, value, row, message):
        try:
            agent = self.manager.agents.get(row["agent_id"])
            if agent is None or agent.harness != "codex" or agent.workspace != row["workspace"]:
                raise ValueError("terminal_project_changed")
            await self.validator(self.manager.config.codex_socket, row["session_id"], row["workspace"])
            if self.get(row["id"])["status"] != "active":
                raise ValueError("attachment_inactive")
            value["status"] = "submitted"
            self._save("terminal_deliveries", value)
            # Native busy turns are intentionally steered. No autonomous profile,
            # automatic permission approval or native cancel RPC is used here.
            async with asyncio.timeout(self.manager.config.run_timeout):
                reason = await self.sessions[row["id"]].prompt(message)
            value.update(status="completed", stop_reason=reason)
        except (Exception, asyncio.CancelledError):
            value.update(status="uncertain" if value["status"] == "submitted" else "failed",
                         error="terminal_delivery_failed")
            current = self.get(row["id"])
            if current["status"] == "active":
                current.update(status="failed", error="terminal_delivery_failed")
                self._save("terminal_attachments", current)
            session = self.sessions.pop(row["id"], None)
            if session:
                await session.close()
        finally:
            try:
                self._save("terminal_deliveries", value)
            finally:
                self.busy.discard(row["id"])

    async def reap(self):
        for ident in list(self.sessions):
            if self.get(ident)["status"] != "active":
                await self.sessions.pop(ident).close()
        for task in self.tasks.values():
            if task.done() and not task.cancelled() and task.exception() is not None:
                logging.getLogger(__name__).error("terminal_task_failed")
        self.tasks = {ident: task for ident, task in self.tasks.items() if not task.done()}

    async def shutdown(self):
        await asyncio.sleep(0)
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for ident, session in list(self.sessions.items()):
            row = self.get(ident)
            row["status"] = "expired"
            self._save("terminal_attachments", row)
            await session.close()
        self.sessions.clear()
