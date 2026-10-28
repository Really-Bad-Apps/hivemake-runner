import hashlib
import json
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

from runner.delivery_journal import DeliveryJournal, DeliveryRequest, recover_retired_deliveries


def test_duplicate_and_changed_payload_cannot_admit_a_second_delivery(tmp_path):
    with sqlite3.connect(tmp_path / "journal.sqlite3") as db:
        journal = DeliveryJournal(db)
        request = DeliveryRequest(str(uuid4()), str(uuid4()), "Peer answer")
        admission = journal.admit(request)
        assert admission.is_new
        assert not journal.admit(request).is_new
        journal.update(replace(admission.record, status="submitted"))
        completed = replace(admission.record, status="completed", stop_reason="end_turn")
        journal.update(completed)
        assert journal.admit(request).record == completed
        for changed in (replace(request, message="Different"), replace(request, attachment_id=str(uuid4()))):
            with pytest.raises(ValueError, match="delivery_conflict"):
                journal.admit(changed)
        with pytest.raises(ValueError, match="invalid_delivery_transition"):
            journal.update(replace(completed, status="submitted"))
        assert db.execute("SELECT count(*) FROM terminal_deliveries").fetchone()[0] == 1


def test_legacy_records_survive_restart_without_replay(tmp_path):
    path = tmp_path / "journal.sqlite3"
    requests = []
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE terminal_attachments (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("INSERT INTO terminal_attachments VALUES ('lease', ?)",
                   (json.dumps({"status": "active", "session_id": "original"}),))
        db.execute("CREATE TABLE terminal_deliveries (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        for status in ("admitted", "submitted", "completed", "failed", "uncertain"):
            request = DeliveryRequest(str(uuid4()), str(uuid4()), status)
            requests.append(request)
            row = dict(id=request.id, attachment_id=request.attachment_id, status=status,
                       fingerprint=hashlib.sha256(json.dumps([request.attachment_id, status]).encode()).hexdigest())
            db.execute("INSERT INTO terminal_deliveries VALUES (?,?)", (request.id, json.dumps(row)))
    with sqlite3.connect(path) as db:
        recover_retired_deliveries(db)
        journal = DeliveryJournal(db)
        for request, expected in zip(requests, ("failed", "uncertain", "completed", "failed", "uncertain")):
            admission = journal.admit(request)
            assert not admission.is_new
            assert admission.record.status == expected
        assert journal.get(requests[1].id).error == "runner_restarted"
        before = list(db.execute("SELECT * FROM terminal_deliveries"))
        recover_retired_deliveries(db)
        assert list(db.execute("SELECT * FROM terminal_deliveries")) == before
        lease = json.loads(db.execute("SELECT body FROM terminal_attachments").fetchone()[0])
        assert lease == {"status": "expired", "session_id": "original"}


def test_fresh_runner_does_not_create_legacy_tables():
    with sqlite3.connect(":memory:") as db:
        recover_retired_deliveries(db)
        assert list(db.execute("SELECT name FROM sqlite_master WHERE type='table'")) == []


def test_update_normalizes_uuid_spelling(tmp_path):
    with sqlite3.connect(tmp_path / "journal.sqlite3") as db:
        journal = DeliveryJournal(db)
        request = DeliveryRequest(str(uuid4()), str(uuid4()), "Peer answer")
        record = journal.admit(request).record
        journal.update(replace(record, id=record.id.upper(),
                               attachment_id=record.attachment_id.upper(), status="submitted"))
        assert journal.get(record.id) == replace(record, status="submitted")
