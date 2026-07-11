import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import time

import requests

from scripts.cron_jobs import CronScheduler, Settings, configure_logging


class StubResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class StubSession:
    def __init__(self):
        self.get_response = StubResponse({"status": "healthy"})
        self.post_response = StubResponse(
            {"ok": True, "data": {"filename": "jarvis_remote.db"}}, 201
        )
        self.get_error = None
        self.post_error = None
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.get_error:
            raise self.get_error
        return self.get_response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if self.post_error:
            raise self.post_error
        return self.post_response

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return StubResponse({}, 202)


def make_settings(tmp_path, **overrides):
    values = {
        "core_url": "http://jarvis.test",
        "db_path": tmp_path / "data" / "jarvis.db",
        "log_dir": tmp_path / "logs",
        "backup_dir": tmp_path / "backups",
        "pushgateway_url": "http://pushgateway.test",
        "backup_retention_days": 7,
    }
    values.update(overrides)
    values["db_path"].parent.mkdir(parents=True, exist_ok=True)
    return Settings(**values)


def create_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE episodes (id INTEGER PRIMARY KEY, timestamp REAL, value TEXT)"
        )
        connection.execute(
            "INSERT INTO episodes(timestamp, value) VALUES (0, 'keep-history')"
        )


def test_database_maintenance_preserves_historical_rows(tmp_path):
    settings = make_settings(tmp_path)
    create_database(settings.db_path)
    scheduler = CronScheduler(settings, session=StubSession())

    result = scheduler.database_maintenance()

    with sqlite3.connect(settings.db_path) as connection:
        row = connection.execute("SELECT timestamp, value FROM episodes").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert row == (0.0, "keep-history")
    assert integrity == "ok"
    assert set(result) == {"checkpointed_frames", "busy"}


def test_http_backup_exception_uses_online_backup_and_prunes_managed_files(tmp_path):
    settings = make_settings(tmp_path, api_token="test-token")
    create_database(settings.db_path)
    settings.backup_dir.mkdir(parents=True)
    old_backup = settings.backup_dir / "jarvis_old.db"
    old_backup.write_bytes(b"old")
    unrelated = settings.backup_dir / "customer.db"
    unrelated.write_bytes(b"do-not-delete")
    current_time = time.time()
    old_time = current_time - 8 * 24 * 60 * 60
    os.utime(old_backup, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))
    session = StubSession()
    session.post_error = requests.ConnectionError("core unavailable")
    scheduler = CronScheduler(settings, session=session, now=lambda: current_time)

    result = scheduler.trigger_backup()

    assert result["method"] == "sqlite"
    fallback = settings.backup_dir / result["filename"]
    with sqlite3.connect(fallback) as connection:
        assert connection.execute("SELECT value FROM episodes").fetchone()[0] == (
            "keep-history"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not old_backup.exists()
    assert unrelated.read_bytes() == b"do-not-delete"
    assert session.calls[0][2]["headers"] == {"X-Jarvis-Token": "test-token"}


def test_invalid_success_response_also_uses_fallback(tmp_path):
    settings = make_settings(tmp_path)
    create_database(settings.db_path)
    session = StubSession()
    session.post_response = StubResponse({"ok": True, "data": {}}, 201)

    result = CronScheduler(settings, session=session).trigger_backup()

    assert result["method"] == "sqlite"
    assert (settings.backup_dir / result["filename"]).is_file()


def test_health_checks_core_and_database_without_obsolete_components(tmp_path):
    settings = make_settings(tmp_path)
    create_database(settings.db_path)
    session = StubSession()
    session.get_error = requests.ConnectionError("core unavailable")
    scheduler = CronScheduler(settings, session=session)

    assert scheduler.health_check() is False
    assert scheduler._health_metrics["core_healthy"] == 0
    assert scheduler._health_metrics["database_healthy"] == 1
    assert scheduler._health_metrics["disk_healthy"] == 1


def test_pushgateway_receives_complete_snapshot_with_current_task_state(tmp_path):
    settings = make_settings(tmp_path)
    create_database(settings.db_path)
    session = StubSession()
    scheduler = CronScheduler(settings, session=session)
    scheduler._health_metrics["core_healthy"] = 1

    assert scheduler.execute_task("collect_metrics") is True

    put_calls = [call for call in session.calls if call[0] == "PUT"]
    assert put_calls
    _, url, request = put_calls[-1]
    assert url == "http://pushgateway.test/metrics/job/jarvis-cron"
    assert "jarvis_cron_scheduler_running 1" in request["data"]
    assert "jarvis_cron_core_healthy 1" in request["data"]
    assert "jarvis_cron_database_size_bytes" in request["data"]
    assert 'jarvis_cron_task_success{task="collect_metrics"} 1.0' in request["data"]


def test_logging_uses_rotating_file_handler(tmp_path):
    settings = make_settings(tmp_path, log_max_bytes=1024, log_backup_count=2)

    configure_logging(settings)

    logger = logging.getLogger("jarvis-cron")
    handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 1024
    assert handlers[0].backupCount == 2
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.addHandler(logging.NullHandler())


def test_settings_read_paths_token_and_retention_from_environment(tmp_path):
    settings = Settings.from_env(
        {
            "JARVIS_CORE_URL": "http://core.test/",
            "JARVIS_DB_PATH": str(tmp_path / "custom.db"),
            "JARVIS_LOG_DIR": str(tmp_path / "custom-logs"),
            "JARVIS_BACKUP_DIR": str(tmp_path / "custom-backups"),
            "JARVIS_API_TOKEN": "secret",
            "PROMETHEUS_PUSHGATEWAY": "http://push.test/",
            "JARVIS_BACKUP_RETENTION_DAYS": "14",
        }
    )

    assert settings.core_url == "http://core.test"
    assert settings.db_path == tmp_path / "custom.db"
    assert settings.log_dir == tmp_path / "custom-logs"
    assert settings.backup_dir == tmp_path / "custom-backups"
    assert settings.api_token == "secret"
    assert settings.pushgateway_url == "http://push.test"
    assert settings.backup_retention_days == 14
