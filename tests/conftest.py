import sys
from pathlib import Path

import pytest

from runner.config import Config
from runner.manager import Manager
from runner.store import Store


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "agent.yaml").write_text("version: 1\nid: demo\nname: Demo\nharness: codex\n")
    fake = str(Path(__file__).with_name("fake_agent.py"))
    return Config(
        (root,),
        tmp_path / "runtime" / "runner.sock",
        tmp_path / "state",
        {"codex": (sys.executable, fake), "claude": (sys.executable, fake)},
        startup_timeout=3,
        run_timeout=8,
        cancel_grace=0.1,
    )


@pytest.fixture
async def manager(config):
    config.state_dir.mkdir()
    store = Store(config.state_dir / "runs.sqlite3")
    manager = Manager(config, store)
    yield manager
    await manager.shutdown()
    store.close()
