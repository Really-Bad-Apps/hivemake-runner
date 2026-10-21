import json
import sqlite3
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.probe_codex_connector import probe, write_report


def test_report_replaces_public_file_with_private_evidence(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("old")
    report.chmod(0o644)
    write_report(report, {"response": "private diagnostic"})
    assert stat.S_IMODE(report.stat().st_mode) == 0o600
    assert json.loads(report.read_text()) == {"response": "private diagnostic"}


@pytest.mark.parametrize("workspace_access", [False, True])
async def test_failed_adapter_start_still_saves_report(tmp_path, workspace_access):
    args = SimpleNamespace(
        thread=str(uuid4()), cwd=tmp_path, state_db=tmp_path / "state.sqlite",
        adapter=tmp_path / "missing-adapter", socket=tmp_path / "server.sock",
        report=tmp_path / "report.json", workspace_access=workspace_access,
        preserve_read_only=not workspace_access, human_approval=False,
    )
    with sqlite3.connect(args.state_db) as db:
        db.execute("CREATE TABLE threads (id, cwd, sandbox_policy, approval_mode, model, reasoning_effort)")
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", (
            args.thread, str(tmp_path), "{}", "never", "test-model", "medium",
        ))
    with pytest.raises(FileNotFoundError):
        await probe(args)
    report = json.loads(args.report.read_text())
    assert report["before"] == report["after_detach"]
    assert report["settings_preserved"] is False
    assert report["workspace_policy_applied"] is False
    assert stat.S_IMODE(args.report.stat().st_mode) == 0o600
