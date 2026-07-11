import json
from pathlib import Path

import pytest

from jarvis.tools.host_tools import execute
from jarvis.tools.local_commands import LocalCommandExecutor


def _result(name, arguments):
    return json.loads(execute(name, arguments))


def test_create_write_read_and_list_workspace_files(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))

    created = _result("create_directory", {"path": "tes"})
    written = _result(
        "write_file", {"path": "tes/index.html", "content": "<h1>updated</h1>"}
    )
    read = _result("read_file", {"path": "tes/index.html"})
    listed = _result("list_directory", {"path": "tes"})

    assert created["ok"] is True
    assert written["ok"] is True
    assert written["bytes"] == len("<h1>updated</h1>")
    assert read["content"] == "<h1>updated</h1>"
    assert listed["entries"] == [{"name": "index.html", "type": "file"}]


def test_write_file_updates_existing_content(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    target = tmp_path / "tes" / "index.html"

    assert _result("write_file", {"path": "tes/index.html", "content": "first"})["ok"]
    assert _result("write_file", {"path": "tes/index.html", "content": "second"})["ok"]

    assert target.read_text(encoding="utf-8") == "second"


@pytest.mark.parametrize("path", ["../secret", "/tmp/outside", "tes/../../outside"])
def test_tools_reject_paths_outside_workspace(tmp_path, monkeypatch, path):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))

    result = _result("write_file", {"path": path, "content": "blocked"})

    assert result["ok"] is False
    assert "outside" in result["error"]


def test_tools_reject_symlink_escape(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(workspace))

    result = _result("write_file", {"path": "link/secret.txt", "content": "blocked"})

    assert result["ok"] is False
    assert "outside" in result["error"]
    assert not (outside / "secret.txt").exists()


def test_open_file_reports_real_process_result(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(tmp_path))
    target = tmp_path / "index.html"
    target.write_text("ok", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr("jarvis.tools.host_tools.subprocess.run", fake_run)

    result = _result("open_file", {"path": "index.html"})

    assert result["ok"] is True
    assert calls == [(["open", str(target)], {"check": True, "timeout": 10})]


def test_unrecognized_message_is_left_for_the_model():
    executor = LocalCommandExecutor()
    assert executor.execute("帮我在桌面创建一个文件夹") is None
    assert executor.execute("帮我总结这段文字") is None


def test_current_date_command_does_not_require_model_or_filesystem():
    result = LocalCommandExecutor().execute("看一下系统日历，今天几号")

    assert result is not None
    assert result.operation == "get_current_date"
    assert result.executed is True
    assert "今天是" in result.message
