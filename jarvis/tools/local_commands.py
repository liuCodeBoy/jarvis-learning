"""Small, explicit local actions with path and input guardrails.

This module intentionally does not execute arbitrary shell commands.  New
actions should be added as typed operations with a narrow allowed path scope,
then covered by tests before being exposed to chat.
"""

from __future__ import annotations

import os
import re
import logging
import subprocess
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_CREATE_FOLDER_RE = re.compile(r"(?:新建|创建|建立)\s*(?:一个|个)?\s*(.+?)\s*文件夹")
_LOCAL_FOLDER_RE = re.compile(r"(?:新建|创建|建立)\s*(?:一个|个)?\s*(?:本地)?\s*文件夹\s*(?:命名为|叫做|叫|名为)?\s*[“\"']?([^\s“\"']+)")
_DATE_MARKERS = (
    "今天几号", "今天多少号", "现在几号", "查看系统日历", "看一下系统日历",
    "今天星期几", "今天周几",
)
_LOCAL_ACTION_MARKERS = (
    "新建", "创建", "建立", "写入", "保存", "运行", "打开", "删除",
)
_QUOTES = " \t\r\n\"'“”‘’「」『』《》【】[]()（）"
_MAX_FOLDER_NAME = 64
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalCommandResult:
    """A user-facing result from one local operation."""

    operation: str
    message: str
    executed: bool


def _desktop_path() -> Path:
    configured = os.environ.get("JARVIS_DESKTOP_PATH", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.home() / "Desktop"


def _extract_folder_name(message: str) -> Optional[str]:
    normalized = " ".join(message.strip().split())
    if "桌面" not in normalized or "文件夹" not in normalized:
        return None
    match = _CREATE_FOLDER_RE.search(normalized)
    if not match:
        return None

    name = match.group(1).strip(_QUOTES)
    name = re.sub(r"^(?:名为|叫做|叫|命名为)\s*", "", name)
    name = re.sub(r"\s*(?:命名的?|文件夹名称)$", "", name).strip(_QUOTES)
    if not name or name in {"文件夹", "一个"}:
        return None
    return name.strip()


def _validate_folder_name(name: str) -> Optional[str]:
    if not 1 <= len(name) <= _MAX_FOLDER_NAME:
        return f"文件夹名称长度必须在 1 到 {_MAX_FOLDER_NAME} 个字符之间"
    if name in {".", ".."} or "/" in name or "\\" in name:
        return "文件夹名称不能包含路径分隔符"
    if any(ord(character) < 32 for character in name):
        return "文件夹名称包含不可用字符"
    return None


class LocalCommandExecutor:
    """Dispatch explicitly supported natural-language local commands."""

    def execute(self, message: str) -> Optional[LocalCommandResult]:
        name = _extract_folder_name(message)
        if name is not None:
            return self._create_folder(name)
        local_match = _LOCAL_FOLDER_RE.search(message)
        if local_match:
            return self._create_folder(local_match.group(1), root=Path.cwd())
        if any(marker in message for marker in _DATE_MARKERS):
            return self._current_date()
        return None

    @staticmethod
    def is_local_action_request(message: str) -> bool:
        """Return whether a request asks for a side effect we must not fake."""
        return any(marker in message for marker in _LOCAL_ACTION_MARKERS)

    @staticmethod
    def _current_date() -> LocalCommandResult:
        now = datetime.now()
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        return LocalCommandResult(
            "get_current_date",
            f"今天是 {now.year}年{now.month}月{now.day}日，{weekdays[now.weekday()]}。",
            True,
        )

    @staticmethod
    def _create_folder(name: str, root: Optional[Path] = None) -> LocalCommandResult:

        validation_error = _validate_folder_name(name)
        if validation_error:
            return LocalCommandResult("create_folder", validation_error, False)

        root = root or _desktop_path()
        target = root / name
        try:
            root.mkdir(parents=True, exist_ok=True)
            target.mkdir(exist_ok=False)
        except FileExistsError:
            return LocalCommandResult(
                "create_folder", f"桌面上的“{name}”文件夹已经存在，没有覆盖它。", False
            )
        except PermissionError:
            return LocalCommandResult(
                "create_folder",
                (
                    f"当前运行环境没有写入目录的权限（{root}）。"
                    + ("没有写入桌面的权限。" if root == _desktop_path() else "")
                    + "请给运行终端开启“桌面与文稿文件夹”权限，"
                    + "或设置 JARVIS_DESKTOP_PATH 到可写目录后重启服务。"
                ),
                False,
            )
        except OSError as exc:
            # Do not expose OS error text to the model, but retain the path
            # and errno in logs for diagnosing platform-specific failures.
            logger.warning(
                "Local folder creation failed path=%s errno=%s", root, exc.errno
            )
            return LocalCommandResult(
                "create_folder", f"无法创建文件夹，请检查路径权限：{root}。", False
            )

        prefix = "已在桌面创建文件夹" if root == _desktop_path() else "已创建文件夹"
        return LocalCommandResult(
            "create_folder", f"{prefix}“{name}”，路径：{target}。", True
        )
