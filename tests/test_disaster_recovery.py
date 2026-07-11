import hashlib
import json
import sqlite3

import pytest

from scripts.disaster_recovery import (
    MANIFEST_FILENAME,
    MANIFEST_HASH_FILENAME,
    DisasterRecovery,
    RecoveryError,
    main,
)


def create_database(path, value="initial"):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO state VALUES (?)", (value,))
    connection.commit()
    connection.close()


def read_value(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT value FROM state").fetchone()[0]


def rewrite_manifest_hash(backup_path):
    manifest_bytes = (backup_path / MANIFEST_FILENAME).read_bytes()
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    (backup_path / MANIFEST_HASH_FILENAME).write_text(
        f"{digest}  {MANIFEST_FILENAME}\n",
        encoding="ascii",
    )


@pytest.fixture()
def recovery(tmp_path):
    project_root = tmp_path / "project"
    database = project_root / "data" / "jarvis.db"
    create_database(database)
    (project_root / "config.yaml").write_text("learning:\n  enabled: true\n")
    (project_root / "docker-compose.yml").write_text("services: {}\n")
    (project_root / "Dockerfile").write_text("FROM python:3.12-slim\n")
    monitoring = project_root / "monitoring"
    monitoring.mkdir()
    (monitoring / "prometheus.yml").write_text("scrape_configs: []\n")
    manager = DisasterRecovery(
        db_path=database,
        backup_dir=project_root / "backups" / "recovery",
        project_root=project_root,
    )
    return manager, project_root, database


def test_create_uses_online_sqlite_backup_and_verifiable_manifest(recovery):
    manager, project_root, database = recovery
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("UPDATE state SET value = 'committed-in-wal'")
    writer.commit()

    result = manager.create_backup(reason="test online snapshot")
    writer.close()

    backup_path = manager.backup_dir / result["backup_id"]
    assert result["valid"] is True
    assert read_value(backup_path / "database.sqlite3") == "committed-in-wal"
    assert (backup_path / MANIFEST_HASH_FILENAME).is_file()
    assert (
        backup_path / "configuration" / "monitoring" / "prometheus.yml"
    ).is_file()
    assert not (backup_path / "configuration" / "config_docker.yaml").exists()
    assert manager.verify_backup(result["backup_id"])["valid"] is True


def test_verify_rejects_payload_tampering(recovery):
    manager, _, _ = recovery
    result = manager.create_backup()
    backup_path = manager.backup_dir / result["backup_id"]
    config_copy = backup_path / "configuration" / "config.yaml"
    config_copy.write_text("tampered: true\n")

    with pytest.raises(RecoveryError, match="(Size|SHA-256) mismatch"):
        manager.verify_backup(result["backup_id"])

    listed = {item["backup_id"]: item for item in manager.list_backups()}
    assert listed[result["backup_id"]]["valid"] is False


def test_verify_rejects_manifest_path_traversal_even_with_valid_manifest_hash(
    recovery,
):
    manager, _, _ = recovery
    result = manager.create_backup()
    backup_path = manager.backup_dir / result["backup_id"]
    manifest_path = backup_path / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["path"] = "../outside.db"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rewrite_manifest_hash(backup_path)

    with pytest.raises(RecoveryError, match="Unsafe manifest path"):
        manager.verify_backup(result["backup_id"])


def test_restore_requires_confirmation_and_keeps_configuration_unchanged(recovery):
    manager, project_root, database = recovery
    selected = manager.create_backup(reason="known good")
    selected_id = selected["backup_id"]

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE state SET value = 'newer live data'")
    (project_root / "config.yaml").write_text("learning:\n  enabled: false\n")
    inode_before_restore = database.stat().st_ino

    with pytest.raises(RecoveryError, match="explicit confirmation"):
        manager.restore_backup(selected_id)

    restored = manager.restore_backup(selected_id, confirmed=True)

    assert restored["restored"] is True
    assert restored["configuration_restored"] is False
    assert restored["pre_restore_backup_id"].startswith("pre-restore-")
    assert database.stat().st_ino != inode_before_restore
    assert read_value(database) == "initial"
    assert (project_root / "config.yaml").read_text() == (
        "learning:\n  enabled: false\n"
    )

    pre_restore_path = (
        manager.backup_dir
        / restored["pre_restore_backup_id"]
        / "database.sqlite3"
    )
    assert read_value(pre_restore_path) == "newer live data"


def test_restore_refuses_database_still_open_by_another_connection(recovery):
    manager, _, database = recovery
    selected = manager.create_backup(reason="known good")
    live_connection = sqlite3.connect(database)
    assert live_connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    live_connection.execute("SELECT * FROM state").fetchall()

    try:
        with pytest.raises(RecoveryError, match="before database replacement"):
            manager.restore_backup(selected["backup_id"], confirmed=True)
    finally:
        live_connection.close()

    assert read_value(database) == "initial"
    assert any(
        item["backup_id"].startswith("pre-restore-")
        for item in manager.list_backups()
    )


def test_cli_returns_nonzero_when_create_fails(tmp_path, capsys):
    missing_database = tmp_path / "missing.db"
    exit_code = main([
        "--db-path",
        str(missing_database),
        "--backup-dir",
        str(tmp_path / "backups"),
        "create",
    ])

    assert exit_code == 1
    assert not missing_database.exists()
