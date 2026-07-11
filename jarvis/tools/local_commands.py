"""Deterministic local information commands that do not require model tools."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Optional


_DATE_MARKERS = (
    "今天几号", "今天多少号", "现在几号", "查看系统日历", "看一下系统日历",
    "今天星期几", "今天周几",
)


@dataclass(frozen=True)
class LocalCommandResult:
    """A user-facing result from one local operation."""

    operation: str
    message: str
    executed: bool


class LocalCommandExecutor:
    """Handle only deterministic commands that require no side effects."""

    def execute(self, message: str) -> Optional[LocalCommandResult]:
        if any(marker in message for marker in _DATE_MARKERS):
            return self._current_date()
        return None

    @staticmethod
    def _current_date() -> LocalCommandResult:
        now = datetime.now()
        weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
        return LocalCommandResult(
            "get_current_date",
            f"今天是 {now.year}年{now.month}月{now.day}日，{weekdays[now.weekday()]}。",
            True,
        )
