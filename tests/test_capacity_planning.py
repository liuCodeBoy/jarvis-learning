import json
import sqlite3

import pytest

from scripts.capacity_planning import (
    PROJECT_ROOT,
    SECONDS_PER_DAY,
    CapacityReporter,
    default_db_path,
    main,
    resolve_db_path,
)


NOW = 1_800_000_000.0


def stub_resources(_path):
    return {
        "filesystem": {
            "path": "/test",
            "total_bytes": 10_000_000,
            "used_bytes": 4_000_000,
            "free_bytes": 6_000_000,
            "used_percent": 40.0,
        },
        "system": {
            "cpu_logical": 4,
            "cpu_physical": 2,
            "memory_available_bytes": 2_000_000,
        },
        "process": {"pid": 123, "rss_bytes": 10_000},
    }


def create_capacity_db(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "CREATE TABLE interactions (id INTEGER PRIMARY KEY, timestamp REAL, value TEXT)"
    )
    connection.execute(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    timestamps = [
        NOW - SECONDS_PER_DAY,
        NOW - 6 * SECONDS_PER_DAY,
        NOW - 8 * SECONDS_PER_DAY,
        NOW - 29 * SECONDS_PER_DAY,
        NOW - 31 * SECONDS_PER_DAY,
        NOW + SECONDS_PER_DAY,
    ]
    connection.executemany(
        "INSERT INTO interactions(timestamp, value) VALUES (?, 'value')",
        ((timestamp,) for timestamp in timestamps),
    )
    connection.executemany(
        "INSERT INTO episodes(timestamp) VALUES (?)", ((NOW,), (NOW,))
    )
    connection.commit()
    return connection


def make_reporter(path, **overrides):
    values = {
        "horizon_days": 20,
        "growth_window_days": 10,
        "clock": lambda: NOW,
        "resource_collector": stub_resources,
    }
    values.update(overrides)
    return CapacityReporter(path, **values)


def test_relative_and_environment_paths_are_rooted_at_project():
    assert resolve_db_path("data/custom.db") == PROJECT_ROOT / "data" / "custom.db"
    assert default_db_path({"JARVIS_DB_PATH": "state/test.db"}) == (
        PROJECT_ROOT / "state" / "test.db"
    )


def test_missing_database_is_reported_without_creating_it(tmp_path):
    missing = tmp_path / "missing" / "jarvis.db"

    report = make_reporter(missing).generate()

    assert report["database"]["status"] == "missing"
    assert report["database"]["table_rows"] == {}
    assert report["projection"]["status"] == "unavailable"
    assert not missing.exists()
    assert not missing.parent.exists()


def test_report_uses_unix_seconds_and_measures_real_files_and_rows(tmp_path):
    db_path = tmp_path / "jarvis.db"
    writer = create_capacity_db(db_path)
    try:
        report = make_reporter(db_path).generate()

        database = report["database"]
        assert database["status"] == "ok"
        assert database["main_db_bytes"] == db_path.stat().st_size
        assert database["wal_bytes"] == (tmp_path / "jarvis.db-wal").stat().st_size
        assert database["sqlite_allocated_bytes"] == (
            database["page_size_bytes"] * database["page_count"]
        )
        assert database["table_rows"] == {"episodes": 2, "interactions": 6}
        assert database["total_table_rows"] == 8
        assert database["interactions"]["last_7_days"] == 2
        assert database["interactions"]["last_30_days"] == 4
        assert database["interactions"]["growth_window_count"] == 3

        projection = report["projection"]
        assert projection["observed_daily_rate"] == pytest.approx(0.3)
        assert projection["projected_new_interactions"] == pytest.approx(6.0)
        assert projection["projected_interactions_at_horizon"] == pytest.approx(12.0)
        expected_per_row = database["sqlite_allocated_bytes"] / 8
        assert projection["estimated_blended_bytes_per_row"] == pytest.approx(
            expected_per_row
        )
        assert projection["projected_main_db_bytes_without_retention"] == pytest.approx(
            database["sqlite_allocated_bytes"] + 6 * expected_per_row, abs=1
        )
    finally:
        writer.close()


def test_retention_projection_only_keeps_rows_alive_at_horizon(tmp_path):
    db_path = tmp_path / "jarvis.db"
    writer = create_capacity_db(db_path)
    try:
        report = make_reporter(db_path, retention_days=7).generate()
    finally:
        writer.close()

    projection = report["projection"]
    assert projection["projected_interactions_at_horizon"] == pytest.approx(2.1)
    assert projection["projected_compacted_main_db_bytes_with_retention"] is not None
    assert any("DELETE alone" in item for item in projection["assumptions"])


def test_database_without_interactions_table_remains_a_valid_snapshot(tmp_path):
    db_path = tmp_path / "jarvis.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE state (value TEXT)")
        connection.execute("INSERT INTO state VALUES ('ready')")

    report = make_reporter(db_path).generate()

    assert report["database"]["status"] == "ok"
    assert report["database"]["table_rows"] == {"state": 1}
    assert report["database"]["interactions"]["available"] is False
    assert report["projection"]["status"] == "unavailable"


def test_json_mode_emits_only_parseable_report(tmp_path, capsys):
    db_path = tmp_path / "jarvis.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE interactions (id INTEGER PRIMARY KEY, timestamp REAL)"
        )

    exit_code = main(["--db", str(db_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["database"]["path"] == str(db_path)
    assert payload["database"]["status"] == "ok"
    assert "estimates" not in payload
    assert "recommendations" not in payload
