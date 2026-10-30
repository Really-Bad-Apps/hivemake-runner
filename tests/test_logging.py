"""Offline logging checks: no production endpoint or provider calls."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from byteforge_loki_logging import logging_config

from runner import logging_setup
from tests.test_runner import finished


@pytest.fixture
def isolated_logging(monkeypatch):
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers = []
    for name in ("DEBUG_LOCAL", "LOG_LEVEL", "LOKI_ENDPOINT", "LOKI_USER",
                 "LOKI_PASSWORD", "LOKI_CA_BUNDLE_PATH"):
        monkeypatch.delenv(name, raising=False)
    probe = Mock(side_effect=AssertionError("No network probe permitted"))
    monkeypatch.setattr(logging_config, "_test_loki_connection", probe)
    yield probe
    for handler in root.handlers:
        handler.close()
    root.handlers, root.level = handlers, level


@pytest.mark.parametrize("hostname", ["vulcan", "apollo", "zeus"])
def test_hostname_label_and_local_journal_copy(monkeypatch, isolated_logging, capsys, hostname):
    remote = logging.handlers.BufferingHandler(100)
    monkeypatch.setenv("DEBUG_LOCAL", "false")
    monkeypatch.setenv("LOKI_ENDPOINT", "https://logs.example.test/loki/api/v1/push")
    monkeypatch.setenv("LOKI_USER", "test")
    monkeypatch.setenv("LOKI_PASSWORD", "private_password")
    monkeypatch.setenv("LOKI_CA_BUNDLE_PATH", "/test/ca.pem")
    monkeypatch.setattr(logging_config, "_test_loki_connection", Mock(return_value=(True, "")))
    create = Mock(return_value=remote)
    monkeypatch.setattr(logging_config, "_create_loki_handler", create)
    monkeypatch.setattr(logging_config, "_register_atexit_flush", Mock())
    monkeypatch.setattr(logging_setup.socket, "gethostname", Mock(return_value=hostname))
    logging_setup.setup_logging()
    assert create.call_args.args[0] == f"hivemake-runner-{hostname}"
    logging.getLogger("runner.manager").info("run_finished", extra={"run_id": "test-run"})
    logging.getLogger("acp.connection").warning("private_protocol_payload")
    assert remote.buffer[-1].run_id == "test-run"
    assert remote.buffer[-1].hostname == hostname
    assert remote.buffer[-1].os_user
    assert all("private_protocol_payload" not in r.getMessage() for r in remote.buffer)
    assert '"run_id": "test-run"' in capsys.readouterr().err


def test_default_console_mode_has_no_network_probe(isolated_logging, capsys):
    logging_setup.setup_logging()
    logging.getLogger("runner.daemon").info("local_ready")
    assert "local_ready" in capsys.readouterr().out
    isolated_logging.assert_not_called()


def test_missing_remote_settings_fail_before_network(monkeypatch, isolated_logging):
    monkeypatch.setenv("DEBUG_LOCAL", "false")
    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        logging_setup.setup_logging()
    isolated_logging.assert_not_called()


def test_unreachable_loki_keeps_console_logs(monkeypatch, isolated_logging, capsys):
    monkeypatch.setenv("DEBUG_LOCAL", "false")
    for name in ("LOKI_ENDPOINT", "LOKI_USER", "LOKI_PASSWORD", "LOKI_CA_BUNDLE_PATH"):
        monkeypatch.setenv(name, "test-value")
    monkeypatch.setattr(logging_config, "_test_loki_connection", Mock(return_value=(False, "offline")))
    logging_setup.setup_logging()
    logging.getLogger("runner.daemon").warning("still_running")
    output = capsys.readouterr()
    assert "still_running" in output.out
    assert "Falling back to stdout" in output.err


def test_real_queue_serializes_host_context_and_filters_protocol(monkeypatch, isolated_logging):
    monkeypatch.setenv("DEBUG_LOCAL", "false")
    monkeypatch.setenv("LOKI_ENDPOINT", "https://logs.example.test/loki/api/v1/push")
    monkeypatch.setenv("LOKI_USER", "test")
    monkeypatch.setenv("LOKI_PASSWORD", "private_password")
    monkeypatch.setenv("LOKI_CA_BUNDLE_PATH", "/test/ca.pem")
    monkeypatch.setattr(logging_setup.socket, "gethostname", Mock(return_value="test-host"))
    monkeypatch.setattr(logging_setup.os, "geteuid", Mock(return_value=1234))
    lookup_user = Mock(return_value=SimpleNamespace(pw_name="service-user"))
    monkeypatch.setattr(logging_setup.pwd, "getpwuid", lookup_user)
    monkeypatch.setattr(logging_config, "_test_loki_connection", Mock(return_value=(True, "")))
    post = Mock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr(logging_config.requests.Session, "post", post)
    try:
        logging_setup.setup_logging()
        logging.getLogger("runner.manager").info("run_finished", extra={"run_id": "test-run"})
        logging.getLogger("acp.connection").warning("private_protocol_payload")
        assert logging_config.flush_logging(timeout=2)
        lookup_user.assert_called_once_with(1234)
        assert post.called
        payload = json.dumps([call.kwargs["json"] for call in post.call_args_list])
        for expected in ("hivemake-runner-test-host", "service-user", "test-run"):
            assert expected in payload
        assert "private_protocol_payload" not in payload
        assert "private_password" not in payload
    finally:
        logging_config.flush_logging(timeout=2)


async def test_run_logs_have_ids_but_not_prompt_or_response(manager, caplog):
    caplog.set_level(logging.INFO, logger="runner.manager")
    session = manager.new_session("demo")
    run = manager.submit(session["id"], "private_prompt_marker")
    result = await finished(manager, run["id"])
    assert result["status"] == "completed"
    records = [r for r in caplog.records if getattr(r, "run_id", None) == run["id"]]
    assert records[0].getMessage() == "run_started"
    assert records[-1].status == "completed"
    assert records[-1].agent_id == "demo"
    assert "private_prompt_marker" not in caplog.text
