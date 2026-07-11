#!/usr/bin/env python3
"""Local, verifiable disaster recovery for the Jarvis SQLite database.

Backups use a directory layout instead of an archive. This keeps verification
and restore simple and removes archive extraction from the trust boundary.
Configuration files are retained for reference, but restore never applies them.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_FORMAT = "jarvis-sqlite-backup"
BACKUP_FORMAT_VERSION = 1
DATABASE_FILENAME = "database.sqlite3"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_HASH_FILENAME = "manifest.sha256"
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ROOT_CONFIG_FILES = ("config.yaml", "docker-compose.yml", "Dockerfile")

logger = logging.getLogger("jarvis-disaster-recovery")


class RecoveryError(RuntimeError):
    """Raised when a backup or restore cannot be completed safely."""


def _path_from_project(value: Union[str, Path], project_root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def default_database_path(project_root: Path = PROJECT_ROOT) -> Path:
    return _path_from_project(
        os.environ.get("JARVIS_DB_PATH", "data/jarvis_learning.db"),
        project_root,
    )


def default_backup_directory(project_root: Path = PROJECT_ROOT) -> Path:
    configured = os.environ.get(
        "JARVIS_RECOVERY_BACKUP_DIR",
        os.environ.get("JARVIS_BACKUP_DIR", "backups/disaster_recovery"),
    )
    return _path_from_project(configured, project_root)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DisasterRecovery:
    """Create and restore self-verifying local SQLite backups."""

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        backup_dir: Optional[Union[str, Path]] = None,
        project_root: Union[str, Path] = PROJECT_ROOT,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.db_path = _path_from_project(
            db_path if db_path is not None else default_database_path(self.project_root),
            self.project_root,
        )
        self.backup_dir = _path_from_project(
            backup_dir
            if backup_dir is not None
            else default_backup_directory(self.project_root),
            self.project_root,
        )

    def create_backup(
        self,
        reason: str = "manual",
        prefix: str = "backup",
    ) -> Dict[str, Any]:
        """Create a consistent SQLite snapshot and its hashed manifest."""
        self._validate_source_database()
        reason = reason.strip()
        if not reason or len(reason) > 256:
            raise RecoveryError("Backup reason must contain 1 to 256 characters")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", prefix):
            raise RecoveryError("Invalid backup prefix")

        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_id = self._new_backup_id(prefix)
        final_directory = self.backup_dir / backup_id
        staging_directory = Path(
            tempfile.mkdtemp(prefix=".creating-", dir=str(self.backup_dir))
        )

        try:
            database_file = staging_directory / DATABASE_FILENAME
            self._online_backup(database_file)
            os.chmod(database_file, 0o600)

            file_entries = [self._manifest_file_entry(
                database_file,
                DATABASE_FILENAME,
                role="database",
            )]
            file_entries.extend(self._backup_configuration_files(staging_directory))

            manifest = {
                "format": BACKUP_FORMAT,
                "version": BACKUP_FORMAT_VERSION,
                "backup_id": backup_id,
                "created_at": _utc_now().isoformat(),
                "reason": reason,
                "source_database": str(self.db_path),
                "files": file_entries,
            }
            manifest_bytes = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            manifest_path = staging_directory / MANIFEST_FILENAME
            manifest_path.write_bytes(manifest_bytes)
            os.chmod(manifest_path, 0o600)

            manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
            hash_path = staging_directory / MANIFEST_HASH_FILENAME
            hash_path.write_text(
                f"{manifest_digest}  {MANIFEST_FILENAME}\n",
                encoding="ascii",
            )
            os.chmod(hash_path, 0o600)
            self._sync_backup_files(staging_directory)

            os.replace(staging_directory, final_directory)
            self._sync_directory(self.backup_dir)
        except Exception:
            shutil.rmtree(staging_directory, ignore_errors=True)
            raise

        verification = self.verify_backup(backup_id)
        logger.info("Created verified backup %s", backup_id)
        return verification

    def list_backups(self) -> List[Dict[str, Any]]:
        """List complete backup directories and report invalid entries."""
        if not self.backup_dir.exists():
            return []

        results: List[Dict[str, Any]] = []
        for candidate in sorted(self.backup_dir.iterdir(), reverse=True):
            if candidate.name.startswith(".") or not candidate.is_dir():
                continue
            try:
                results.append(self.verify_backup(candidate.name))
            except (RecoveryError, OSError) as exc:
                results.append({
                    "backup_id": candidate.name,
                    "valid": False,
                    "error": str(exc),
                    "path": str(candidate),
                })
        return results

    def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        """Verify manifest integrity, every payload hash, and SQLite integrity."""
        backup_directory = self._backup_path(backup_id)
        if not backup_directory.is_dir() or backup_directory.is_symlink():
            raise RecoveryError(f"Backup does not exist: {backup_id}")

        manifest_path = self._ordinary_file(
            backup_directory / MANIFEST_FILENAME,
            "manifest",
        )
        hash_path = self._ordinary_file(
            backup_directory / MANIFEST_HASH_FILENAME,
            "manifest hash",
        )
        manifest_bytes = manifest_path.read_bytes()
        expected_manifest_hash = self._read_manifest_hash(hash_path)
        actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_manifest_hash != expected_manifest_hash:
            raise RecoveryError("Manifest SHA-256 mismatch")

        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"Manifest is not valid UTF-8 JSON: {exc}") from exc
        self._validate_manifest_header(manifest, backup_id)

        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise RecoveryError("Manifest must contain a non-empty files list")

        expected_payloads = set()
        database_paths: List[Path] = []
        configuration_count = 0
        for entry in entries:
            relative_path, payload_path, role = self._validate_payload_entry(
                backup_directory,
                entry,
            )
            if relative_path in expected_payloads:
                raise RecoveryError(f"Duplicate manifest path: {relative_path}")
            expected_payloads.add(relative_path)

            expected_size = entry["size"]
            if payload_path.stat().st_size != expected_size:
                raise RecoveryError(f"Size mismatch: {relative_path}")
            if sha256_file(payload_path) != entry["sha256"]:
                raise RecoveryError(f"SHA-256 mismatch: {relative_path}")

            if role == "database":
                database_paths.append(payload_path)
            else:
                configuration_count += 1

        if len(database_paths) != 1:
            raise RecoveryError("Manifest must contain exactly one database payload")
        self._reject_unlisted_files(backup_directory, expected_payloads)
        self._check_sqlite_integrity(database_paths[0])

        return {
            "backup_id": backup_id,
            "valid": True,
            "created_at": manifest["created_at"],
            "reason": manifest["reason"],
            "path": str(backup_directory),
            "database_path": str(database_paths[0]),
            "configuration_path": str(backup_directory / "configuration"),
            "configuration_files": configuration_count,
            "size": sum(entry["size"] for entry in entries),
            "manifest_sha256": actual_manifest_hash,
        }

    def restore_backup(self, backup_id: str, confirmed: bool = False) -> Dict[str, Any]:
        """Atomically replace the database after verification and a safety backup."""
        if not confirmed:
            raise RecoveryError("Restore requires explicit confirmation (--confirm)")

        selected = self.verify_backup(backup_id)
        self._validate_source_database()
        if self.db_path.is_symlink():
            raise RecoveryError("Refusing to replace a database symlink")

        pre_restore = self.create_backup(
            reason=f"before restoring {backup_id}",
            prefix="pre-restore",
        )
        source_database = Path(selected["database_path"])
        old_mode = stat.S_IMODE(self.db_path.stat().st_mode)

        database_replaced = False
        try:
            self._prepare_database_for_replace()
            self._atomic_database_replace(source_database, old_mode)
            database_replaced = True
            self._check_sqlite_integrity(self.db_path)
        except Exception as restore_error:
            if not database_replaced:
                raise RecoveryError(
                    "Restore stopped before database replacement; "
                    f"pre-restore backup is {pre_restore['backup_id']}: "
                    f"{restore_error}"
                ) from restore_error
            try:
                rollback_database = Path(pre_restore["database_path"])
                self._prepare_database_for_replace()
                self._atomic_database_replace(rollback_database, old_mode)
                self._check_sqlite_integrity(self.db_path)
            except Exception as rollback_error:
                raise RecoveryError(
                    "Restore failed and automatic database rollback also failed; "
                    f"pre-restore backup is {pre_restore['backup_id']}: "
                    f"restore={restore_error}; rollback={rollback_error}"
                ) from rollback_error
            raise RecoveryError(
                "Restore failed; the database was rolled back from "
                f"{pre_restore['backup_id']}: {restore_error}"
            ) from restore_error

        logger.info("Restored %s from %s", self.db_path, backup_id)
        return {
            "restored": True,
            "backup_id": backup_id,
            "database_path": str(self.db_path),
            "pre_restore_backup_id": pre_restore["backup_id"],
            "pre_restore_backup_path": pre_restore["path"],
            "configuration_backup_path": selected["configuration_path"],
            "configuration_restored": False,
        }

    def _validate_source_database(self) -> None:
        if not self.db_path.exists():
            raise RecoveryError(f"Database does not exist: {self.db_path}")
        if not self.db_path.is_file():
            raise RecoveryError(f"Database path is not a regular file: {self.db_path}")

    def _new_backup_id(self, prefix: str) -> str:
        timestamp = _utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
        return f"{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}"

    def _online_backup(self, destination: Path) -> None:
        source_uri = f"{self.db_path.as_uri()}?mode=ro"
        source: Optional[sqlite3.Connection] = None
        target: Optional[sqlite3.Connection] = None
        try:
            source = sqlite3.connect(source_uri, uri=True, timeout=30)
            target = sqlite3.connect(str(destination), timeout=30)
            source.backup(target)
            checkpoint = target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and checkpoint[0] != 0:
                raise RecoveryError("Could not checkpoint the backup database")
            journal_mode = target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(journal_mode).lower() != "delete":
                raise RecoveryError("Could not make the backup database self-contained")
        except sqlite3.Error as exc:
            raise RecoveryError(f"SQLite online backup failed: {exc}") from exc
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(destination) + suffix)
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
        self._check_sqlite_integrity(destination)

    def _configuration_sources(self) -> Iterable[Tuple[Path, Path]]:
        for relative_name in ROOT_CONFIG_FILES:
            relative_path = Path(relative_name)
            source = self.project_root / relative_path
            if source.exists():
                yield relative_path, source

        monitoring_root = self.project_root / "monitoring"
        if monitoring_root.exists():
            for source in sorted(monitoring_root.rglob("*")):
                if source.is_file():
                    yield source.relative_to(self.project_root), source

    def _backup_configuration_files(
        self,
        staging_directory: Path,
    ) -> List[Dict[str, Any]]:
        entries = []
        for source_relative, source in self._configuration_sources():
            if source.is_symlink():
                raise RecoveryError(f"Refusing to back up configuration symlink: {source}")
            resolved_source = source.resolve()
            try:
                resolved_source.relative_to(self.project_root)
            except ValueError as exc:
                raise RecoveryError(
                    f"Configuration file is outside the project: {source}"
                ) from exc

            destination_relative = Path("configuration") / source_relative
            destination = staging_directory / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
            entries.append(self._manifest_file_entry(
                destination,
                destination_relative.as_posix(),
                role="configuration",
                source=source_relative.as_posix(),
            ))
        return entries

    @staticmethod
    def _manifest_file_entry(
        path: Path,
        relative_path: str,
        role: str,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "path": relative_path,
            "role": role,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        if source is not None:
            entry["source"] = source
        return entry

    def _backup_path(self, backup_id: str) -> Path:
        if not isinstance(backup_id, str) or not BACKUP_ID_RE.fullmatch(backup_id):
            raise RecoveryError("Invalid backup ID")
        return self.backup_dir / backup_id

    @staticmethod
    def _ordinary_file(path: Path, description: str) -> Path:
        if not path.is_file() or path.is_symlink():
            raise RecoveryError(f"Missing or unsafe {description}: {path.name}")
        return path

    @staticmethod
    def _read_manifest_hash(path: Path) -> str:
        try:
            parts = path.read_text(encoding="ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise RecoveryError("Manifest hash file is not ASCII") from exc
        if (
            len(parts) != 2
            or not SHA256_RE.fullmatch(parts[0])
            or parts[1] != MANIFEST_FILENAME
        ):
            raise RecoveryError("Manifest hash file has an invalid format")
        return parts[0]

    @staticmethod
    def _validate_manifest_header(manifest: Any, backup_id: str) -> None:
        if not isinstance(manifest, dict):
            raise RecoveryError("Manifest root must be an object")
        if manifest.get("format") != BACKUP_FORMAT:
            raise RecoveryError("Unsupported backup format")
        version = manifest.get("version")
        if isinstance(version, bool) or version != BACKUP_FORMAT_VERSION:
            raise RecoveryError("Unsupported backup format version")
        if manifest.get("backup_id") != backup_id:
            raise RecoveryError("Manifest backup ID does not match its directory")
        if not isinstance(manifest.get("reason"), str) or not manifest["reason"]:
            raise RecoveryError("Manifest reason is missing")
        created_at = manifest.get("created_at")
        if not isinstance(created_at, str):
            raise RecoveryError("Manifest creation time is missing")
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise RecoveryError("Manifest creation time is invalid") from exc
        if parsed.tzinfo is None:
            raise RecoveryError("Manifest creation time must include a timezone")

    def _validate_payload_entry(
        self,
        backup_directory: Path,
        entry: Any,
    ) -> Tuple[str, Path, str]:
        if not isinstance(entry, dict):
            raise RecoveryError("Manifest file entries must be objects")
        relative_value = entry.get("path")
        if not isinstance(relative_value, str) or not relative_value:
            raise RecoveryError("Manifest payload path is missing")
        if "\\" in relative_value:
            raise RecoveryError(f"Unsafe manifest path: {relative_value}")
        relative_path = PurePosixPath(relative_value)
        if (
            relative_path.is_absolute()
            or relative_path == PurePosixPath(".")
            or any(part in ("", ".", "..") for part in relative_path.parts)
        ):
            raise RecoveryError(f"Unsafe manifest path: {relative_value}")

        role = entry.get("role")
        if role not in ("database", "configuration"):
            raise RecoveryError(f"Unsupported payload role: {role}")
        if role == "database" and relative_value != DATABASE_FILENAME:
            raise RecoveryError("Database payload has an unexpected path")
        if role == "configuration":
            source = entry.get("source")
            if not isinstance(source, str) or not source:
                raise RecoveryError("Configuration payload is missing its source path")

        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise RecoveryError(f"Invalid payload size: {relative_value}")
        checksum = entry.get("sha256")
        if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
            raise RecoveryError(f"Invalid payload SHA-256: {relative_value}")

        payload = backup_directory.joinpath(*relative_path.parts)
        resolved_backup = backup_directory.resolve()
        try:
            payload.resolve().relative_to(resolved_backup)
        except ValueError as exc:
            raise RecoveryError(f"Payload escapes backup directory: {relative_value}") from exc
        self._ordinary_file(payload, f"payload {relative_value}")
        return relative_value, payload, role

    @staticmethod
    def _reject_unlisted_files(
        backup_directory: Path,
        expected_payloads: set,
    ) -> None:
        expected = set(expected_payloads)
        expected.update({MANIFEST_FILENAME, MANIFEST_HASH_FILENAME})
        actual = set()
        for path in backup_directory.rglob("*"):
            if path.is_symlink():
                raise RecoveryError(
                    f"Backup contains an unsafe symlink: {path.relative_to(backup_directory)}"
                )
            if path.is_file():
                actual.add(path.relative_to(backup_directory).as_posix())
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise RecoveryError(
                f"Backup file set differs from manifest; missing={missing}, "
                f"unexpected={unexpected}"
            )

    @staticmethod
    def _check_sqlite_integrity(path: Path) -> None:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                rows = connection.execute("PRAGMA integrity_check").fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise RecoveryError(f"SQLite integrity check failed for {path}: {exc}") from exc
        messages = [str(row[0]) for row in rows]
        if messages != ["ok"]:
            raise RecoveryError(
                f"SQLite integrity check failed for {path}: {'; '.join(messages)}"
            )

    def _prepare_database_for_replace(self) -> None:
        """Checkpoint WAL and require exclusive journal-mode control.

        The application must be stopped before restore. Switching out of WAL
        mode gives a practical refusal signal when another process still owns
        the database and removes stale sidecar files before replacement.
        """
        try:
            connection = sqlite3.connect(str(self.db_path), timeout=1)
            try:
                checkpoint = connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if checkpoint is not None and checkpoint[0] != 0:
                    raise RecoveryError(
                        "Database is busy; stop Jarvis before restoring"
                    )
                journal_mode = connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()[0]
                if str(journal_mode).lower() != "delete":
                    raise RecoveryError(
                        "Could not obtain exclusive journal control; "
                        "stop Jarvis before restoring"
                    )
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise RecoveryError(
                f"Database is busy or cannot be prepared for restore: {exc}"
            ) from exc

        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists():
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise RecoveryError(f"Unsafe SQLite sidecar file: {sidecar}")
                sidecar.unlink()

    def _atomic_database_replace(self, source: Path, mode: int) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.db_path.name}.restore-",
            dir=str(self.db_path.parent),
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary_path)
            os.chmod(temporary_path, mode)
            self._check_sqlite_integrity(temporary_path)
            self._sync_file(temporary_path)
            os.replace(temporary_path, self.db_path)
            self._sync_directory(self.db_path.parent)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def _sync_backup_files(cls, backup_directory: Path) -> None:
        for path in backup_directory.rglob("*"):
            if path.is_file():
                cls._sync_file(path)
        directories = [
            path for path in backup_directory.rglob("*") if path.is_dir()
        ]
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            cls._sync_directory(directory)
        cls._sync_directory(backup_directory)

    @staticmethod
    def _sync_file(path: Path) -> None:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _sync_directory(path: Path) -> None:
        try:
            descriptor = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Some filesystems do not support fsync on directories.
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, and restore local Jarvis SQLite backups",
    )
    parser.add_argument(
        "--db-path",
        help="SQLite database path (default: JARVIS_DB_PATH or data/jarvis_learning.db)",
    )
    parser.add_argument(
        "--backup-dir",
        help=(
            "Backup directory (default: JARVIS_RECOVERY_BACKUP_DIR, "
            "JARVIS_BACKUP_DIR, or backups/disaster_recovery)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logs")

    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create", help="Create a verified backup")
    create_parser.add_argument(
        "--reason",
        default="manual",
        help="Short reason recorded in the manifest",
    )
    commands.add_parser("list", help="List backups and their validation status")
    verify_parser = commands.add_parser("verify", help="Verify one backup")
    verify_parser.add_argument("backup_id")
    restore_parser = commands.add_parser("restore", help="Restore one backup")
    restore_parser.add_argument("backup_id")
    restore_parser.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="Confirm replacement of the configured database",
    )
    return parser


def _print_human(command: str, result: Any) -> None:
    if command == "list":
        if not result:
            print("No backups found")
            return
        for item in result:
            status = "valid" if item.get("valid") else "INVALID"
            detail = item.get("created_at") or item.get("error", "")
            print(f"{item['backup_id']}\t{status}\t{detail}")
        return

    if command == "restore":
        print(f"Restored database from {result['backup_id']}")
        print(f"Pre-restore backup: {result['pre_restore_backup_id']}")
        print(
            "Configuration was not restored; retained copy: "
            f"{result['configuration_backup_path']}"
        )
        return

    print(f"Backup: {result['backup_id']}")
    print(f"Status: {'valid' if result['valid'] else 'INVALID'}")
    print(f"Path: {result['path']}")
    print(f"Manifest SHA-256: {result['manifest_sha256']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        manager = DisasterRecovery(db_path=args.db_path, backup_dir=args.backup_dir)
        if args.command == "create":
            result: Any = manager.create_backup(reason=args.reason)
        elif args.command == "list":
            result = manager.list_backups()
        elif args.command == "verify":
            result = manager.verify_backup(args.backup_id)
        elif args.command == "restore":
            result = manager.restore_backup(args.backup_id, confirmed=args.confirm)
        else:  # pragma: no cover - argparse guarantees the command
            parser.error("unknown command")
            return 2
    except (RecoveryError, OSError, sqlite3.Error) as exc:
        logger.error("%s", exc)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(args.command, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
