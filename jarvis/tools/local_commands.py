"""Small, explicit local actions with path and input guardrails.

This module intentionally does not execute arbitrary shell commands.  New
actions should be added as typed operations with a narrow allowed path scope,
then covered by tests before being exposed to chat.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_CREATE_FOLDER_RE = re.compile(r"(?:新建|创建|建立)\s*(?:一个|个)?\s*(.+?)\s*文件夹")
_QUOTES = " \t\r\n\"'“”‘’「」『』《》【】[]()（）"
_MAX_FOLDER_NAME = 64


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
        if name is None:
            return None

        validation_error = _validate_folder_name(name)
        if validation_error:
            return LocalCommandResult("create_folder", validation_error, False)

        desktop = _desktop_path()
        target = desktop / name
        try:
            desktop.mkdir(parents=True, exist_ok=True)
            target.mkdir(exist_ok=False)
        except FileExistsError:
            return LocalCommandResult(
                "create_folder", f"桌面上的“{name}”文件夹已经存在，没有覆盖它。", False
            )
        except OSError:
            return LocalCommandResult(
                "create_folder", "无法在桌面创建文件夹，请检查桌面路径权限。", False
            )

        return LocalCommandResult(
            "create_folder", f"已在桌面创建文件夹“{name}”。", True
        )
