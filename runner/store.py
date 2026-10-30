"""Durable run results; sessions deliberately expire when the runner restarts."""

import json
import sqlite3
from datetime import UTC, datetime


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        # Status is indexed separately so restart recovery does not read or
        # deserialize completed transcripts. Upgrade the original two-column
        # prototype in one transaction; SQLite handles the backfill on disk.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS runs "
                "(id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL)"
            )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
            if "status" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN status TEXT")
                self.db.execute("UPDATE runs SET status=json_extract(body, '$.status')")
            self.db.execute("CREATE INDEX IF NOT EXISTS runs_status ON runs(status)")
        # The daemon's process lock prevents concurrent writers during recovery.
        # Read one unfinished transcript at a time, never the full history.
        while row := self.db.execute(
            "SELECT body FROM runs WHERE status IN ('running', 'cancelling') "
            "AND COALESCE(json_extract(body, '$.upgrade_pending'), 0)=0 LIMIT 1"
        ).fetchone():
            run = json.loads(row[0])
            run.update(status="interrupted", finished_at=now(), error="runner_restarted")
            self.save(run)

    def save(self, run):
        with self.db:
            self.db.execute(
                "INSERT INTO runs (id, body, status) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body, status=excluded.status",
                (run["id"], json.dumps(run), run["status"]),
            )

    def get(self, run_id):
        row = self.db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
