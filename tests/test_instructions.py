import hashlib
import json
from dataclasses import replace

import pytest
import yaml
from tests_helpers import wait_finished

from runner.config import Config
from runner.instructions import MAX_INSTRUCTION_BYTES
from runner.manager import RunnerError


def configure(manager, config, tmp_path, harness="codex"):
    path = tmp_path / "shared.md"
    path.write_text("Answer questions only. Do not change files.\n")
    manager.config = replace(config, instruction_profiles={"read-only": path})
    (config.roots[0] / "agent.yaml").write_text(
        f"version: 1\nid: demo\nharness: {harness}\n"
        "runner:\n  instruction_profile: read-only\n"
    )
    manager.refresh()
    return path


@pytest.mark.parametrize("harness", ["codex", "claude"])
async def test_profile_crosses_real_acp_transport_and_preserves_project_files(
    manager, config, tmp_path, harness
):
    path = configure(manager, config, tmp_path, harness)
    project_files = [config.roots[0] / name for name in ("CLAUDE.md", "AGENTS.md", "agent.yaml")]
    for file in project_files[:2]:
        file.write_text("Existing project rules.\n")
    before = {p: p.read_bytes() for p in project_files}
    original = path.read_text()
    session = manager.new_session("demo")
    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assert session["instructions"]["sha256"] == expected_hash
    # Even lazy-started sessions retain the snapshot from their creation.
    path.write_text("A different revision.\n")
    run = manager.submit(session["id"], "inspect_profile")
    await wait_finished(manager, run["id"])
    result = manager.get_run(run["id"])
    assert result["status"] == "completed"
    seen = json.loads(result["response"].split(": ", 1)[1])
    if harness == "codex":
        assert len(seen["blocks"]) == 2
        assert original in seen["blocks"][0]
        assert "systemPrompt" not in seen["options"]
    else:
        assert seen["options"]["systemPrompt"] == {
            "type": "preset", "preset": "claude_code", "append": original,
        }
        assert seen["blocks"] == ["inspect_profile"]
    assert result["instructions"] == session["instructions"]
    assert result["prompt"] == "inspect_profile"
    # Follow-ups use the same conversation, without injecting duplicate context.
    followup = manager.submit(session["id"], "inspect_profile")
    await wait_finished(manager, followup["id"])
    next_result = manager.get_run(followup["id"])
    seen = json.loads(next_result["response"].split(": ", 1)[1])
    assert seen["blocks"] == ["inspect_profile"]
    newer = manager.new_session("demo")
    assert newer["instructions"]["sha256"] != expected_hash
    assert {p: p.read_bytes() for p in project_files} == before
    await manager.close_session(session["id"])
    assert manager.get_run(run["id"])["instructions"]["sha256"] == expected_hash


@pytest.mark.parametrize("failure", ["unknown", "missing", "directory", "empty", "binary", "large"])
async def test_invalid_profile_prevents_session(manager, config, tmp_path, failure):
    path = configure(manager, config, tmp_path)
    if failure == "unknown":
        manager.config = replace(manager.config, instruction_profiles={})
    elif failure == "missing":
        path.unlink()
    elif failure == "directory":
        path.unlink()
        path.mkdir()
    elif failure == "empty":
        path.write_text(" \n")
    elif failure == "binary":
        path.write_bytes(b"\xff")
    else:
        path.write_bytes(b"a" * (MAX_INSTRUCTION_BYTES + 1))
    manager.refresh()
    assert manager.list_agents()["agents"][0]["ready"] is False
    with pytest.raises(RunnerError, match="instruction_profile"):
        manager.new_session("demo")
    assert manager.sessions == {}


def test_config_profile_paths(tmp_path):
    config = tmp_path / "runner.yaml"
    data = {"roots": ["/tmp/projects"], "socket": "/tmp/s", "state_dir": "/tmp/d",
            "instruction_profiles": {"shared": {"file": str(tmp_path / "shared.md")}}}
    config.write_text(yaml.safe_dump(data))
    assert Config.load(config).instruction_profiles["shared"] == tmp_path / "shared.md"
    for profiles in (["bad"], {"shared": "inline text"}, {"shared": {"file": "relative"}},
                     {"../bad": {"file": "/tmp/shared.md"}}):
        data["instruction_profiles"] = profiles
        config.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError):
            Config.load(config)


@pytest.mark.parametrize("runner", [None, "shared", {"instruction_profile": None},
                                   {"instruction_profile": "../bad"}, {"instructions": "inline"}])
async def test_invalid_project_profile_reference(manager, config, runner):
    (config.roots[0] / "agent.yaml").write_text(yaml.safe_dump(
        {"version": 1, "id": "demo", "harness": "codex", "runner": runner}
    ))
    manager.refresh()
    assert "demo" not in manager.agents


async def test_missing_file_after_discovery_still_prevents_session(manager, config, tmp_path):
    path = configure(manager, config, tmp_path)
    assert manager.list_agents()["agents"][0]["ready"]
    path.unlink()
    with pytest.raises(RunnerError, match="instruction_profile_unreadable"):
        manager.new_session("demo")
