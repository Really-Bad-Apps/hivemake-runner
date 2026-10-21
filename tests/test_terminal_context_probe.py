import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.fixtures.context_mcp import respond
from scripts.probe_terminal_context import audit_events, probe


def test_fixture_produces_fresh_markers_and_matching_audit(tmp_path):
    path = tmp_path / "audit.jsonl"
    request = {"method": "tools/call", "params": {
        "name": "connector_context", "arguments": {"stage": "acp", "delay": 0},
    }}
    first = respond(request, path)
    second = respond(request, path)
    assert first != second
    entries = audit_events(path)
    assert [e["event"] for e in entries] == ["start", "end", "start", "end"]
    assert entries[0]["token"] == entries[1]["token"] == first["content"][0]["text"]
    with path.open("a") as stream:
        stream.write('{"stage":')
    assert audit_events(path) == entries


@pytest.mark.parametrize("delay", [-1, 21, True, "15"])
def test_fixture_refuses_invalid_wait_before_any_work(tmp_path, delay):
    path = tmp_path / "audit.jsonl"
    with pytest.raises(ValueError):
        respond({"method": "tools/call", "params": {
            "name": "connector_context", "arguments": {"stage": "busy", "delay": delay},
        }}, path)
    assert not path.exists()


async def test_failed_context_attach_records_private_evidence(tmp_path):
    args = SimpleNamespace(
        thread=str(uuid4()), cwd=tmp_path, adapter=tmp_path / "missing-adapter",
        socket=tmp_path / "absent.sock", report=tmp_path / "report.json", busy_audit=None,
    )
    with pytest.raises(FileNotFoundError):
        await probe(args)
    assert json.loads(args.report.read_text())["error_type"] == "FileNotFoundError"
    assert args.report.stat().st_mode & 0o777 == 0o600
