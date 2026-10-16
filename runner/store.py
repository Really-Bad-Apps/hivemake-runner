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
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        # The process lock is acquired before opening this store.
        for (body,) in self.db.execute("SELECT body FROM runs").fetchall():
            run = json.loads(body)
            if run["status"] in ("running", "cancelling"):
                run.update(status="interrupted", finished_at=now(), error="runner_restarted")
                self.save(run)

    def save(self, run):
        with self.db:
            self.db.execute(
                "INSERT INTO runs VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (run["id"], json.dumps(run)),
            )

    def get(self, run_id):
        row = self.db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
