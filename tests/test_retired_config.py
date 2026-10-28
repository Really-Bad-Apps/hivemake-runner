import yaml

from runner.config import Config


def test_old_socket_setting_is_ignored_without_touching_native_socket(tmp_path, caplog):
    native_socket = tmp_path / "native.sock"
    native_socket.write_text("untouched")
    path = tmp_path / "runner.yaml"
    path.write_text(yaml.safe_dump({
        "roots": [str(tmp_path)], "socket": str(tmp_path / "runner.sock"),
        "state_dir": str(tmp_path / "state"), "codex_socket": str(native_socket),
    }))
    config = Config.load(path)
    assert not hasattr(config, "codex_socket")
    assert "retired and ignored" in caplog.text
    assert native_socket.read_text() == "untouched"
