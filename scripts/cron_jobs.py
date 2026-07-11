#!/usr/bin/env python3
"""Run the small set of maintenance jobs needed by the Docker deployment.

The scheduler deliberately avoids deleting application data. SQLite maintenance is
limited to an online WAL checkpoint and ``PRAGMA optimize``; backups use SQLite's
online backup API so writers can continue to use the database.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

import psutil
import requests


LOGGER = logging.getLogger("jarvis-cron")
LOGGER.addHandler(logging.NullHandler())


def _connect_sqlite(path: Path, mode: str, timeout: float) -> sqlite3.Connection:
    """Open an existing database without ever creating it implicitly."""
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    return sqlite3.connect(uri, uri=True, timeout=timeout)


def _positive_int(value: Optional[str], default: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _positive_float(value: Optional[str], default: float, name: str) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True)
class Settings:
    core_url: str
    db_path: Path
    log_dir: Path
    backup_dir: Path
    pushgateway_url: str
    api_token: str = ""
    backup_retention_days: int = 30
    http_timeout_seconds: float = 10.0
    push_timeout_seconds: float = 5.0
    poll_interval_seconds: float = 60.0
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 7

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if environ is None else environ
        return cls(
            core_url=(
                env.get("JARVIS_CORE_URL") or "http://jarvis-core:8000"
            ).rstrip("/"),
            db_path=Path(
                env.get("JARVIS_DB_PATH") or "/app/data/jarvis_learning.db"
            ).expanduser(),
            log_dir=Path(env.get("JARVIS_LOG_DIR") or "/app/logs").expanduser(),
            backup_dir=Path(
                env.get("JARVIS_BACKUP_DIR") or "/app/backups"
            ).expanduser(),
            pushgateway_url=env.get(
                "PROMETHEUS_PUSHGATEWAY", "http://prometheus-pushgateway:9091"
            ).rstrip("/"),
            api_token=env.get("JARVIS_API_TOKEN", ""),
            backup_retention_days=_positive_int(
                env.get("JARVIS_BACKUP_RETENTION_DAYS"),
                30,
                "JARVIS_BACKUP_RETENTION_DAYS",
            ),
            http_timeout_seconds=_positive_float(
                env.get("JARVIS_HTTP_TIMEOUT_SECONDS"),
                10.0,
                "JARVIS_HTTP_TIMEOUT_SECONDS",
            ),
            push_timeout_seconds=_positive_float(
                env.get("JARVIS_PUSH_TIMEOUT_SECONDS"),
                5.0,
                "JARVIS_PUSH_TIMEOUT_SECONDS",
            ),
            poll_interval_seconds=_positive_float(
                env.get("JARVIS_CRON_POLL_SECONDS"),
                60.0,
                "JARVIS_CRON_POLL_SECONDS",
            ),
            log_max_bytes=_positive_int(
                env.get("JARVIS_LOG_MAX_BYTES"),
                10 * 1024 * 1024,
                "JARVIS_LOG_MAX_BYTES",
            ),
            log_backup_count=_positive_int(
                env.get("JARVIS_LOG_BACKUP_COUNT"),
                7,
                "JARVIS_LOG_BACKUP_COUNT",
            ),
        )


def configure_logging(settings: Settings) -> None:
    """Configure bounded logging without renaming an open log file."""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = RotatingFileHandler(
        settings.log_dir / "cron.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(stream_handler)


class CronScheduler:
    """Run maintenance jobs sequentially and publish their latest state."""

    TASK_INTERVALS = {
        "database_maintenance": 24 * 60 * 60,
        "health_check": 5 * 60,
        "collect_metrics": 60,
        "trigger_backup": 24 * 60 * 60,
    }

    def __init__(
        self,
        settings: Settings,
        session: Optional[requests.Session] = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.http = session or requests.Session()
        self._now = now
        self._monotonic = monotonic
        self._stop_event = threading.Event()
        self._next_run = {task: 0.0 for task in self.TASK_INTERVALS}
        self._health_metrics = {
            "core_healthy": 0.0,
            "database_healthy": 0.0,
            "disk_healthy": 0.0,
            "disk_usage_ratio": 0.0,
            "healthcheck_timestamp_seconds": 0.0,
        }
        self._runtime_metrics: dict[str, float] = {}
        self._task_metrics: dict[str, dict[str, float]] = {}
        self.settings.backup_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        LOGGER.info("Cron scheduler started")
        while not self._stop_event.is_set():
            current = self._monotonic()
            for task_name, interval in self.TASK_INTERVALS.items():
                if current >= self._next_run[task_name]:
                    self.execute_task(task_name)
                    self._next_run[task_name] = self._monotonic() + interval
            self._stop_event.wait(self.settings.poll_interval_seconds)
        LOGGER.info("Cron scheduler stopped")

    def execute_task(self, task_name: str) -> bool:
        task = getattr(self, task_name, None)
        if task is None or task_name not in self.TASK_INTERVALS:
            raise ValueError(f"unknown cron task: {task_name}")

        LOGGER.info("Running task: %s", task_name)
        started = self._monotonic()
        success = False
        try:
            result = task()
            success = result is not False
        except Exception:
            LOGGER.exception("Task failed: %s", task_name)
        duration = max(0.0, self._monotonic() - started)
        timestamp = self._now()
        previous = self._task_metrics.get(task_name, {})
        self._task_metrics[task_name] = {
            "duration_seconds": duration,
            "success": float(success),
            "last_run_timestamp_seconds": timestamp,
            "last_success_timestamp_seconds": (
                timestamp
                if success
                else previous.get("last_success_timestamp_seconds", 0.0)
            ),
        }
        LOGGER.info(
            "Task finished: %s (success=%s, duration=%.2fs)",
            task_name,
            success,
            duration,
        )
        self.push_metrics()
        return success

    def database_maintenance(self) -> dict[str, Any]:
        """Perform safe online SQLite maintenance without deleting history."""
        if not self.settings.db_path.is_file():
            raise FileNotFoundError(f"database does not exist: {self.settings.db_path}")

        with _connect_sqlite(
            self.settings.db_path, mode="rw", timeout=10
        ) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            connection.execute("PRAGMA optimize")
        busy, checkpointed = 0, 0
        if checkpoint and len(checkpoint) >= 3:
            busy, _, checkpointed = checkpoint
        if busy:
            LOGGER.warning("WAL checkpoint was partially blocked by active clients")
        LOGGER.info(
            "SQLite maintenance completed; checkpointed_frames=%s", checkpointed
        )
        return {"checkpointed_frames": checkpointed, "busy": busy}

    def health_check(self) -> bool:
        """Check each dependency independently and retain honest gauge values."""
        core_healthy = False
        try:
            response = self.http.get(
                f"{self.settings.core_url}/health",
                headers=self._api_headers,
                timeout=self.settings.http_timeout_seconds,
            )
            payload = response.json() if response.status_code == 200 else {}
            core_healthy = (
                response.status_code == 200
                and isinstance(payload, dict)
                and payload.get("status") == "healthy"
            )
        except (requests.RequestException, ValueError, TypeError) as error:
            LOGGER.warning("Core health request failed: %s", error)

        database_healthy = False
        if self.settings.db_path.is_file():
            try:
                with _connect_sqlite(
                    self.settings.db_path, mode="ro", timeout=5
                ) as connection:
                    result = connection.execute("PRAGMA quick_check").fetchone()
                database_healthy = bool(result and result[0] == "ok")
            except sqlite3.Error as error:
                LOGGER.warning("Database health check failed: %s", error)

        disk = shutil.disk_usage(self.settings.backup_dir)
        disk_usage_ratio = disk.used / disk.total if disk.total else 1.0
        disk_healthy = disk_usage_ratio < 0.90
        self._health_metrics.update(
            {
                "core_healthy": float(core_healthy),
                "database_healthy": float(database_healthy),
                "disk_healthy": float(disk_healthy),
                "disk_usage_ratio": disk_usage_ratio,
                "healthcheck_timestamp_seconds": self._now(),
            }
        )
        LOGGER.info(
            "Health: core=%s database=%s disk=%s (%.1f%% used)",
            core_healthy,
            database_healthy,
            disk_healthy,
            disk_usage_ratio * 100,
        )
        return core_healthy and database_healthy and disk_healthy

    def collect_metrics(self) -> dict[str, float]:
        process = psutil.Process()
        memory = psutil.virtual_memory()
        self._runtime_metrics = {
            "database_size_bytes": float(
                self.settings.db_path.stat().st_size
                if self.settings.db_path.is_file()
                else 0
            ),
            "process_resident_memory_bytes": float(process.memory_info().rss),
            "process_cpu_percent": float(process.cpu_percent(interval=None)),
            "system_memory_usage_ratio": memory.percent / 100,
            "heartbeat_timestamp_seconds": self._now(),
        }
        return dict(self._runtime_metrics)

    @property
    def _api_headers(self) -> dict[str, str]:
        if not self.settings.api_token:
            return {}
        return {"X-Jarvis-Token": self.settings.api_token}

    def trigger_backup(self) -> dict[str, str]:
        """Request an application backup, falling back to SQLite online backup."""
        try:
            response = self.http.post(
                f"{self.settings.core_url}/api/backup",
                headers=self._api_headers,
                timeout=max(30.0, self.settings.http_timeout_seconds),
            )
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"backup API returned HTTP {response.status_code}")
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            filename = data.get("filename") if isinstance(data, dict) else None
            if (
                not isinstance(payload, dict)
                or payload.get("ok") is not True
                or not isinstance(filename, str)
                or not filename
            ):
                raise ValueError("backup API returned an invalid response")
            result = {"method": "api", "filename": Path(filename).name}
            LOGGER.info("Application backup completed: %s", result["filename"])
        except (
            requests.RequestException,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            LOGGER.warning(
                "Application backup unavailable, using SQLite backup: %s", error
            )
            target = self._sqlite_backup()
            result = {"method": "sqlite", "filename": target.name}
            LOGGER.info("SQLite fallback backup completed: %s", target)

        removed = self.prune_backups()
        if removed:
            LOGGER.info("Removed %d expired backup(s)", removed)
        return result

    def _sqlite_backup(self) -> Path:
        source_path = self.settings.db_path
        if not source_path.is_file():
            raise FileNotFoundError(f"database does not exist: {source_path}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = self.settings.backup_dir / f"jarvis_fallback_{timestamp}.db"
        temporary = target.with_suffix(".db.tmp")
        source: Optional[sqlite3.Connection] = None
        destination: Optional[sqlite3.Connection] = None
        try:
            source = _connect_sqlite(source_path, mode="ro", timeout=10)
            destination = sqlite3.connect(str(temporary))
            source.backup(destination)
            check = destination.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError("fallback backup failed integrity check")
            destination.close()
            destination = None
            temporary.replace(target)
            target.chmod(0o600)
            return target
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            if temporary.exists():
                temporary.unlink()

    def prune_backups(self) -> int:
        """Delete expired managed backups while always retaining the newest one."""
        backups = sorted(
            (
                path
                for path in self.settings.backup_dir.glob("jarvis_*.db")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        cutoff = self._now() - self.settings.backup_retention_days * 24 * 60 * 60
        removed = 0
        for path in backups[1:]:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return removed

    def push_metrics(self) -> bool:
        """Replace the Pushgateway group with one complete scheduler snapshot."""
        if not self.settings.pushgateway_url:
            return False
        body = self._render_metrics()
        try:
            response = self.http.put(
                f"{self.settings.pushgateway_url}/metrics/job/jarvis-cron",
                data=body,
                headers={
                    "Content-Type": "text/plain; version=0.0.4; charset=utf-8"
                },
                timeout=self.settings.push_timeout_seconds,
            )
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"Pushgateway returned HTTP {response.status_code}")
            return True
        except (requests.RequestException, RuntimeError) as error:
            LOGGER.warning("Metric push failed: %s", error)
            return False

    def _render_metrics(self) -> str:
        lines = [
            "# TYPE jarvis_cron_scheduler_running gauge",
            "jarvis_cron_scheduler_running 1",
        ]
        for name, value in {**self._health_metrics, **self._runtime_metrics}.items():
            lines.extend(
                [
                    f"# TYPE jarvis_cron_{name} gauge",
                    f"jarvis_cron_{name} {value}",
                ]
            )
        for task_name, values in sorted(self._task_metrics.items()):
            label = _escape_label(task_name)
            for name, value in values.items():
                lines.append(
                    f'jarvis_cron_task_{name}{{task="{label}"}} {value}'
                )
        return "\n".join(lines) + "\n"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def main() -> int:
    try:
        settings = Settings.from_env()
        configure_logging(settings)
    except (OSError, ValueError) as error:
        print(f"Invalid cron configuration: {error}", file=sys.stderr)
        return 2

    LOGGER.info("Core URL: %s", settings.core_url)
    LOGGER.info("Database path: %s", settings.db_path)
    scheduler = CronScheduler(settings)

    def request_stop(signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s", signum)
        scheduler.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    scheduler.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
