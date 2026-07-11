import pytest
from pathlib import Path

from jarvis.tools.local_commands import LocalCommandExecutor


def test_create_folder_command_is_scoped_to_desktop(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    monkeypatch.setenv("JARVIS_DESKTOP_PATH", str(desktop))

    result = LocalCommandExecutor().execute("帮我在桌面新建一个强强命名的文件夹")

    assert result is not None
    assert result.operation == "create_folder"
    assert result.executed is True
    assert (desktop / "强强").is_dir()


def test_existing_folder_is_not_overwritten(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "强强").mkdir()
    monkeypatch.setenv("JARVIS_DESKTOP_PATH", str(desktop))

    result = LocalCommandExecutor().execute("在桌面创建一个强强文件夹")

    assert result is not None
    assert result.executed is False
    assert "已经存在" in result.message


def test_permission_error_explains_desktop_access(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    monkeypatch.setenv("JARVIS_DESKTOP_PATH", str(desktop))
    original_mkdir = Path.mkdir

    def deny_desktop(path, *args, **kwargs):
        if path == desktop:
            raise PermissionError("desktop blocked")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_desktop)
    result = LocalCommandExecutor().execute("在桌面创建一个权限测试文件夹")

    assert result is not None
    assert result.executed is False
    assert "没有写入桌面的权限" in result.message


@pytest.mark.parametrize("message", [
    "帮我在桌面新建一个../secret文件夹",
    "帮我在桌面新建一个foo/bar文件夹",
])
def test_folder_command_rejects_path_traversal(tmp_path, monkeypatch, message):
    monkeypatch.setenv("JARVIS_DESKTOP_PATH", str(tmp_path / "Desktop"))

    result = LocalCommandExecutor().execute(message)

    assert result is not None
    assert result.executed is False
    assert "路径分隔符" in result.message


def test_unrecognized_message_is_left_for_the_model():
    assert LocalCommandExecutor().execute("帮我总结这段文字") is None
