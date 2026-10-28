"""Durable admission/recovery extracted from the retired attachment manager.

This module never opens a session or sends a prompt. Legacy table/field names
are retained so existing delivery evidence remains readable. A future transport
must authorize the destination separately before admission.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from typing import Literal
from uuid import UUID

DeliveryStatus = Literal["admitted", "submitted", "completed", "failed", "uncertain"]


@dataclass(frozen=True)
class DeliveryRequest:
    id: str
    attachment_id: str
    message: str


@dataclass(frozen=True)
class DeliveryRecord:
    id: str
    attachment_id: str
    fingerprint: str
    status: DeliveryStatus
    error: str | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class Admission:
    record: DeliveryRecord
    is_new: bool


def recover_retired_deliveries(db: sqlite3.Connection) -> None:
    """Retire old leases and preserve ambiguous submissions without replay.

    Called under the daemon's exclusive process lock on startup. Do not create
    legacy tables for fresh installations, delete records or rewrite outcomes.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    with db:
        if "terminal_attachments" in tables:
            db.execute(
                "UPDATE terminal_attachments SET body=json_set(body, '$.status', 'expired') "
                "WHERE json_extract(body,'$.status') IN ('pending','active')"
            )
        if "terminal_deliveries" in tables:
            db.execute(
                "UPDATE terminal_deliveries SET body=json_set(body,'$.status','uncertain', "
                "'$.error','runner_restarted') WHERE json_extract(body,'$.status')='submitted'"
            )
            db.execute(
                "UPDATE terminal_deliveries SET body=json_set(body,'$.status','failed', "
                "'$.error','runner_restarted_before_submission') "
                "WHERE json_extract(body,'$.status')='admitted'"
            )


class DeliveryJournal:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS terminal_deliveries "
                       "(id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def get(self, ident: str) -> DeliveryRecord | None:
        row = self.db.execute("SELECT body FROM terminal_deliveries WHERE id=?",
                              (str(UUID(ident)),)).fetchone()
        return DeliveryRecord(**json.loads(row[0])) if row else None

    def admit(self, request: DeliveryRequest) -> Admission:
        ident = str(UUID(request.id))
        destination = str(UUID(request.attachment_id))
        if not isinstance(request.message, str) or not request.message.strip() or len(request.message) > 65536:
            raise ValueError("invalid_reply")
        fingerprint = hashlib.sha256(json.dumps([destination, request.message]).encode()).hexdigest()
        record = DeliveryRecord(ident, destination, fingerprint, "admitted")
        with self.db:
            inserted = self.db.execute(
                "INSERT INTO terminal_deliveries VALUES (?,?) ON CONFLICT(id) DO NOTHING",
                (ident, json.dumps(asdict(record))),
            ).rowcount == 1
            existing = self.get(ident)
            if existing is None or existing.fingerprint != fingerprint:
                raise ValueError("delivery_conflict")
        return Admission(existing, inserted)

    def update(self, record: DeliveryRecord) -> None:
        """Never regress a terminal outcome into another submission."""
        record = replace(record, id=str(UUID(record.id)),
                         attachment_id=str(UUID(record.attachment_id)))
        transitions = {
            "admitted": {"submitted", "failed"},
            "submitted": {"completed", "failed", "uncertain"},
        }
        with self.db:
            existing = self.get(record.id)
            if existing is None:
                raise ValueError("delivery_not_found")
            if (record.attachment_id != existing.attachment_id
                    or record.fingerprint != existing.fingerprint):
                raise ValueError("delivery_conflict")
            if record == existing:
                return
            if record.status not in transitions.get(existing.status, set()):
                raise ValueError("invalid_delivery_transition")
            self.db.execute("UPDATE terminal_deliveries SET body=? WHERE id=?",
                            (json.dumps(asdict(record)), record.id))
