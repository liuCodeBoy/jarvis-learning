#!/usr/bin/env python3
"""Generate a read-only local capacity report for Jarvis.

The report deliberately separates measured values from estimates. It does not
guess cloud prices or translate row counts into CPU and memory requirements.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECONDS_PER_DAY = 24 * 60 * 60


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def resolve_db_path(value: str | os.PathLike[str]) -> Path:
    """Resolve relative database paths from the project root, not the CWD."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def default_db_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    return resolve_db_path(values.get("JARVIS_DB_PATH") or "data/jarvis_learning.db")


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _existing_ancestor(path: Path) -> Path:
    current = path if path.is_dir() else path.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _error_message(component: str, error: BaseException) -> str:
    detail = str(error) or "unavailable in this environment"
    return f"{component}: {type(error).__name__}: {detail}"


def _ordinary_tables(connection: sqlite3.Connection) -> list[str]:
    """Return ordinary application tables, excluding SQLite/FTS shadow tables."""
    try:
        rows = connection.execute("PRAGMA table_list").fetchall()
        return sorted(
            row[1]
            for row in rows
            if row[2] == "table" and not row[1].startswith("sqlite_")
        )
    except sqlite3.DatabaseError:
        rows = connection.execute(
            """
            SELECT name
            FROM sqlite_schema
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'
            ORDER BY name
            """
        ).fetchall()
        return [row[0] for row in rows]


def _open_read_only(path: Path) -> sqlite3.Connection:
    # mode=ro is important: sqlite3.connect(path) silently creates a missing DB.
    uri = f"{path.resolve(strict=False).as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _count_recent_interactions(
    connection: sqlite3.Connection, now_epoch: float, days: int
) -> int:
    lower_bound = now_epoch - days * SECONDS_PER_DAY
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM interactions
        WHERE typeof(timestamp) IN ('integer', 'real')
          AND timestamp >= ?
          AND timestamp <= ?
        """,
        (lower_bound, now_epoch),
    ).fetchone()
    return int(row[0])


def analyze_database(path: Path, now_epoch: float, growth_window_days: int) -> dict[str, Any]:
    """Inspect an existing SQLite database without creating or changing it."""
    main_bytes = _file_size(path)
    wal_bytes = _file_size(Path(f"{path}-wal"))
    shm_bytes = _file_size(Path(f"{path}-shm"))
    result: dict[str, Any] = {
        "path": str(path),
        "status": "missing" if not path.is_file() else "ok",
        "main_db_bytes": main_bytes,
        "wal_bytes": wal_bytes,
        "shm_bytes": shm_bytes,
        "total_on_disk_bytes": main_bytes + wal_bytes + shm_bytes,
        "page_size_bytes": None,
        "page_count": None,
        "freelist_pages": None,
        "sqlite_allocated_bytes": None,
        "sqlite_reusable_bytes": None,
        "table_rows": {},
        "total_table_rows": 0,
        "interactions": {
            "available": False,
            "total": None,
            "unix_timestamp_rows": None,
            "last_7_days": None,
            "last_30_days": None,
            "growth_window_days": growth_window_days,
            "growth_window_count": None,
            "observed_daily_rate": None,
        },
        "error": None,
    }
    if result["status"] == "missing":
        result["error"] = "database file does not exist"
        return result

    try:
        with closing(_open_read_only(path)) as connection:
            result["page_size_bytes"] = int(
                connection.execute("PRAGMA page_size").fetchone()[0]
            )
            result["page_count"] = int(
                connection.execute("PRAGMA page_count").fetchone()[0]
            )
            result["freelist_pages"] = int(
                connection.execute("PRAGMA freelist_count").fetchone()[0]
            )
            result["sqlite_allocated_bytes"] = (
                result["page_size_bytes"] * result["page_count"]
            )
            result["sqlite_reusable_bytes"] = (
                result["page_size_bytes"] * result["freelist_pages"]
            )

            tables = _ordinary_tables(connection)
            table_rows: dict[str, int] = {}
            for table in tables:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                ).fetchone()
                table_rows[table] = int(row[0])
            result["table_rows"] = table_rows
            result["total_table_rows"] = sum(table_rows.values())

            if "interactions" in table_rows:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(interactions)")
                }
                interaction_data = result["interactions"]
                interaction_data["total"] = table_rows["interactions"]
                if "timestamp" not in columns:
                    interaction_data["error"] = "interactions.timestamp is missing"
                else:
                    unix_rows = connection.execute(
                        """
                        SELECT COUNT(*) FROM interactions
                        WHERE typeof(timestamp) IN ('integer', 'real')
                        """
                    ).fetchone()[0]
                    interaction_data.update(
                        {
                            "available": True,
                            "unix_timestamp_rows": int(unix_rows),
                            "last_7_days": _count_recent_interactions(
                                connection, now_epoch, 7
                            ),
                            "last_30_days": _count_recent_interactions(
                                connection, now_epoch, 30
                            ),
                            "growth_window_count": _count_recent_interactions(
                                connection, now_epoch, growth_window_days
                            ),
                        }
                    )
                    interaction_data["observed_daily_rate"] = (
                        interaction_data["growth_window_count"] / growth_window_days
                    )
            else:
                result["interactions"]["error"] = "interactions table does not exist"
    except (OSError, sqlite3.DatabaseError) as exc:
        result["status"] = "unreadable"
        result["error"] = str(exc)

    # A WAL may change while the report runs, so use the latest measured sizes.
    result["main_db_bytes"] = _file_size(path)
    result["wal_bytes"] = _file_size(Path(f"{path}-wal"))
    result["shm_bytes"] = _file_size(Path(f"{path}-shm"))
    result["total_on_disk_bytes"] = sum(
        result[key] for key in ("main_db_bytes", "wal_bytes", "shm_bytes")
    )
    return result


def collect_local_resources(db_path: Path) -> dict[str, Any]:
    """Collect current host and reporter-process observations."""
    filesystem_path = _existing_ancestor(db_path)
    errors: list[str] = []

    try:
        disk = psutil.disk_usage(str(filesystem_path))
        filesystem: dict[str, Any] = {
            "path": str(filesystem_path),
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "used_percent": disk.percent,
        }
    except (OSError, psutil.Error) as exc:
        errors.append(_error_message("filesystem", exc))
        filesystem = {
            "path": str(filesystem_path),
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "used_percent": None,
        }

    try:
        memory = psutil.virtual_memory()
        memory_values: dict[str, Any] = {
            "memory_total_bytes": memory.total,
            "memory_available_bytes": memory.available,
            "memory_used_percent": memory.percent,
        }
    except (OSError, psutil.Error) as exc:
        errors.append(_error_message("memory", exc))
        memory_values = {
            "memory_total_bytes": None,
            "memory_available_bytes": None,
            "memory_used_percent": None,
        }

    try:
        swap = psutil.swap_memory()
        swap_values: dict[str, Any] = {
            "swap_total_bytes": swap.total,
            "swap_used_bytes": swap.used,
        }
    except (OSError, psutil.Error) as exc:
        errors.append(_error_message("swap", exc))
        swap_values = {"swap_total_bytes": None, "swap_used_bytes": None}

    try:
        load_average = list(os.getloadavg())
    except (AttributeError, OSError):
        load_average = None

    try:
        boot_time = datetime.fromtimestamp(
            psutil.boot_time(), tz=timezone.utc
        ).isoformat()
    except (OSError, psutil.Error) as exc:
        errors.append(_error_message("boot_time", exc))
        boot_time = None

    process_values: dict[str, Any]
    try:
        process = psutil.Process()
        process_memory = process.memory_info()
        process_cpu = process.cpu_times()
        process_values = {
            "pid": process.pid,
            "rss_bytes": process_memory.rss,
            "vms_bytes": process_memory.vms,
            "cpu_user_seconds": process_cpu.user,
            "cpu_system_seconds": process_cpu.system,
            "threads": process.num_threads(),
            "started_at": datetime.fromtimestamp(
                process.create_time(), tz=timezone.utc
            ).isoformat(),
        }
    except (OSError, psutil.Error) as exc:
        errors.append(_error_message("process", exc))
        process_values = {
            "pid": os.getpid(),
            "rss_bytes": None,
            "vms_bytes": None,
            "cpu_user_seconds": None,
            "cpu_system_seconds": None,
            "threads": None,
            "started_at": None,
        }

    return {
        "filesystem": filesystem,
        "system": {
            "cpu_logical": psutil.cpu_count(logical=True),
            "cpu_physical": psutil.cpu_count(logical=False),
            "load_average_1m_5m_15m": load_average,
            **memory_values,
            **swap_values,
            "boot_time": boot_time,
        },
        "process": process_values,
        "errors": errors,
    }


def build_projection(
    database: Mapping[str, Any],
    *,
    now_epoch: float,
    horizon_days: int,
    growth_window_days: int,
    retention_days: int | None,
    connection_factory: Callable[[Path], sqlite3.Connection] = _open_read_only,
) -> dict[str, Any]:
    """Build a simple linear interaction/storage estimate from measured data."""
    assumptions = [
        (
            f"The observed rate is interactions in the last {growth_window_days} "
            "full days divided by that window; zero-activity days are included."
        ),
        (
            f"The next {horizon_days} days are assumed to continue at that constant "
            "rate; seasonality and confidence bounds are not inferred from one snapshot."
        ),
        (
            "Projected storage uses SQLite allocated pages divided by all ordinary-table "
            "rows as a blended per-row estimate; row mix, indexes, and page utilization "
            "are assumed to remain similar."
        ),
        "WAL/SHM files are reported but excluded from projected permanent storage because they are transient.",
        "CPU and memory values are observations only; this report does not infer future hardware needs or cost.",
    ]
    if retention_days is None:
        assumptions.append("No interaction-retention policy is assumed.")
    else:
        assumptions.append(
            f"The {retention_days}-day retention estimate assumes expired interactions "
            "are deleted and the database is compacted; DELETE alone does not shrink a SQLite file."
        )

    projection: dict[str, Any] = {
        "status": "unavailable",
        "horizon_days": horizon_days,
        "growth_window_days": growth_window_days,
        "retention_days": retention_days,
        "observed_interactions": None,
        "observed_daily_rate": None,
        "projected_new_interactions": None,
        "current_interactions": None,
        "projected_interactions_at_horizon": None,
        "estimated_blended_bytes_per_row": None,
        "projected_incremental_bytes_without_retention": None,
        "projected_main_db_bytes_without_retention": None,
        "projected_compacted_main_db_bytes_with_retention": None,
        "assumptions": assumptions,
    }
    interaction_data = database.get("interactions", {})
    if database.get("status") != "ok" or not interaction_data.get("available"):
        return projection

    observed = int(interaction_data["growth_window_count"])
    daily_rate = observed / growth_window_days
    projected_new = daily_rate * horizon_days
    current_interactions = int(interaction_data["total"])
    total_rows = int(database.get("total_table_rows", 0))
    main_bytes = int(database.get("main_db_bytes", 0))
    allocated_bytes = int(database.get("sqlite_allocated_bytes") or main_bytes)
    bytes_per_row = allocated_bytes / total_rows if total_rows else None

    projection.update(
        {
            "status": "estimated",
            "observed_interactions": observed,
            "observed_daily_rate": daily_rate,
            "projected_new_interactions": projected_new,
            "current_interactions": current_interactions,
            "estimated_blended_bytes_per_row": bytes_per_row,
        }
    )

    if bytes_per_row is None:
        incremental_bytes = 0 if projected_new == 0 else None
        projected_main_bytes = allocated_bytes if projected_new == 0 else None
    else:
        incremental_bytes = math.ceil(projected_new * bytes_per_row)
        projected_main_bytes = allocated_bytes + incremental_bytes
    projection["projected_incremental_bytes_without_retention"] = incremental_bytes
    projection["projected_main_db_bytes_without_retention"] = projected_main_bytes

    if retention_days is None:
        projection["projected_interactions_at_horizon"] = (
            current_interactions + projected_new
        )
        return projection

    surviving_current: int | None = None
    threshold = now_epoch + (horizon_days - retention_days) * SECONDS_PER_DAY
    try:
        with closing(connection_factory(Path(database["path"]))) as connection:
            surviving_current = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM interactions
                    WHERE typeof(timestamp) IN ('integer', 'real')
                      AND timestamp >= ?
                      AND timestamp <= ?
                    """,
                    (threshold, now_epoch),
                ).fetchone()[0]
            )
    except (OSError, sqlite3.DatabaseError):
        projection["status"] = "partial"
        projection["assumptions"].append(
            "Current interactions surviving the retention horizon could not be read."
        )

    if surviving_current is None:
        return projection

    retained_future = daily_rate * min(horizon_days, retention_days)
    retained_at_horizon = surviving_current + retained_future
    projection["projected_interactions_at_horizon"] = retained_at_horizon
    if bytes_per_row is not None:
        estimated_non_interaction_bytes = max(
            0.0, allocated_bytes - current_interactions * bytes_per_row
        )
        projection["projected_compacted_main_db_bytes_with_retention"] = math.ceil(
            estimated_non_interaction_bytes + retained_at_horizon * bytes_per_row
        )
    return projection


class CapacityReporter:
    """Coordinate database, filesystem, process, and system observations."""

    def __init__(
        self,
        db_path: Path,
        *,
        horizon_days: int = 90,
        growth_window_days: int = 30,
        retention_days: int | None = None,
        clock: Callable[[], float] = time.time,
        resource_collector: Callable[[Path], dict[str, Any]] = collect_local_resources,
    ) -> None:
        if horizon_days <= 0 or growth_window_days <= 0:
            raise ValueError("horizon and growth window must be greater than zero")
        if retention_days is not None and retention_days <= 0:
            raise ValueError("retention must be greater than zero")
        self.db_path = resolve_db_path(db_path)
        self.horizon_days = horizon_days
        self.growth_window_days = growth_window_days
        self.retention_days = retention_days
        self.clock = clock
        self.resource_collector = resource_collector

    def generate(self) -> dict[str, Any]:
        now_epoch = self.clock()
        database = analyze_database(
            self.db_path, now_epoch, self.growth_window_days
        )
        resources: dict[str, Any]
        try:
            resources = self.resource_collector(self.db_path)
            resources.setdefault("error", None)
        except (OSError, psutil.Error) as exc:
            resources = {"filesystem": None, "system": None, "process": None, "error": str(exc)}

        projection = build_projection(
            database,
            now_epoch=now_epoch,
            horizon_days=self.horizon_days,
            growth_window_days=self.growth_window_days,
            retention_days=self.retention_days,
        )
        return {
            "generated_at": datetime.fromtimestamp(
                now_epoch, tz=timezone.utc
            ).isoformat(),
            "project_root": str(PROJECT_ROOT),
            "database": database,
            "resources": resources,
            "projection": projection,
            "observations": self._observations(database, resources, projection),
        }

    @staticmethod
    def _observations(
        database: Mapping[str, Any],
        resources: Mapping[str, Any],
        projection: Mapping[str, Any],
    ) -> list[str]:
        observations: list[str] = []
        if database.get("status") != "ok":
            observations.append(
                "Database measurements are unavailable; verify the configured path and permissions."
            )
            return observations
        if database.get("wal_bytes", 0) > database.get("main_db_bytes", 0):
            observations.append(
                "The WAL is larger than the main DB snapshot; check whether checkpoints are completing if this persists."
            )
        interactions = database.get("interactions", {})
        if (
            interactions.get("total") is not None
            and interactions.get("unix_timestamp_rows") != interactions.get("total")
        ):
            observations.append(
                "Some interactions do not have numeric Unix-second timestamps and are excluded from time-window estimates."
            )
        filesystem = resources.get("filesystem") or {}
        used_percent = filesystem.get("used_percent")
        if used_percent is not None and used_percent >= 90:
            observations.append(
                "The database filesystem is at least 90% used; investigate free space before projected growth."
            )
        incremental = projection.get(
            "projected_incremental_bytes_without_retention"
        )
        free_bytes = filesystem.get("free_bytes")
        if incremental is not None and free_bytes is not None and incremental > free_bytes:
            observations.append(
                "The linear no-retention estimate exceeds currently free filesystem space."
            )
        if not observations:
            observations.append(
                "This snapshot does not show an immediate storage-capacity warning; keep historical reports to validate the trend."
            )
        return observations


def _format_bytes(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units[:-1]:
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {units[-1]}"


def render_text(report: Mapping[str, Any]) -> str:
    database = report["database"]
    interactions = database["interactions"]
    resources = report["resources"]
    filesystem = resources.get("filesystem") or {}
    system = resources.get("system") or {}
    process = resources.get("process") or {}
    projection = report["projection"]
    lines = [
        "Jarvis local capacity report",
        f"Generated (UTC): {report['generated_at']}",
        f"Project root: {report['project_root']}",
        f"Database: {database['path']} [{database['status']}]",
    ]
    if database.get("error"):
        lines.append(f"Database error: {database['error']}")
    lines.extend(
        [
            "",
            "Storage",
            f"  Main DB: {_format_bytes(database['main_db_bytes'])}",
            f"  WAL: {_format_bytes(database['wal_bytes'])}",
            f"  SHM: {_format_bytes(database['shm_bytes'])}",
            f"  Total files: {_format_bytes(database['total_on_disk_bytes'])}",
            f"  SQLite allocated pages: {_format_bytes(database['sqlite_allocated_bytes'])}",
            f"  SQLite reusable pages: {_format_bytes(database['sqlite_reusable_bytes'])}",
            f"  Filesystem free: {_format_bytes(filesystem.get('free_bytes'))}",
            "",
            "Ordinary table rows",
        ]
    )
    if database["table_rows"]:
        lines.extend(
            f"  {table}: {count:,}"
            for table, count in database["table_rows"].items()
        )
    else:
        lines.append("  n/a")
    lines.extend(
        [
            "",
            "Interactions",
            f"  Total: {interactions.get('total') if interactions.get('total') is not None else 'n/a'}",
            f"  Last 7 days: {interactions.get('last_7_days') if interactions.get('last_7_days') is not None else 'n/a'}",
            f"  Last 30 days: {interactions.get('last_30_days') if interactions.get('last_30_days') is not None else 'n/a'}",
            (
                f"  Observed rate ({projection['growth_window_days']}d): "
                f"{projection['observed_daily_rate']:.3f}/day"
                if projection["observed_daily_rate"] is not None
                else f"  Observed rate ({projection['growth_window_days']}d): n/a"
            ),
            "",
            "Current resources (not forecasts)",
            f"  CPU logical/physical: {system.get('cpu_logical', 'n/a')}/{system.get('cpu_physical', 'n/a')}",
            f"  Memory available: {_format_bytes(system.get('memory_available_bytes'))}",
            f"  Reporter RSS: {_format_bytes(process.get('rss_bytes'))}",
        ]
    )
    if resources.get("errors"):
        lines.append("  Partial collection errors: " + "; ".join(resources["errors"]))
    lines.extend(
        [
            "",
            f"Linear projection ({projection['horizon_days']} days)",
            f"  Status: {projection['status']}",
            (
                f"  New interactions: {projection['projected_new_interactions']:.1f}"
                if projection["projected_new_interactions"] is not None
                else "  New interactions: n/a"
            ),
            f"  Main DB without retention: {_format_bytes(projection['projected_main_db_bytes_without_retention'])}",
        ]
    )
    if projection["retention_days"] is not None:
        lines.append(
            "  Compacted DB with retention: "
            + _format_bytes(
                projection["projected_compacted_main_db_bytes_with_retention"]
            )
        )
    lines.append("")
    lines.append("Assumptions")
    lines.extend(f"  - {item}" for item in projection["assumptions"])
    lines.append("")
    lines.append("Observations")
    lines.extend(f"  - {item}" for item in report["observations"])
    return "\n".join(lines)


def build_parser(env: Mapping[str, str] | None = None) -> argparse.ArgumentParser:
    values = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        description="Generate a read-only local Jarvis capacity report."
    )
    parser.add_argument(
        "--db",
        default=values.get("JARVIS_DB_PATH") or "data/jarvis_learning.db",
        help="SQLite path (relative paths use the project root; env: JARVIS_DB_PATH)",
    )
    parser.add_argument(
        "--horizon-days",
        type=_positive_int,
        default=values.get("JARVIS_CAPACITY_HORIZON_DAYS", "90"),
        help="linear projection horizon (env: JARVIS_CAPACITY_HORIZON_DAYS)",
    )
    parser.add_argument(
        "--growth-window-days",
        type=_positive_int,
        default=values.get("JARVIS_CAPACITY_GROWTH_WINDOW_DAYS", "30"),
        help="observed interaction-rate window (env: JARVIS_CAPACITY_GROWTH_WINDOW_DAYS)",
    )
    parser.add_argument(
        "--retention-days",
        type=_positive_int,
        default=values.get("JARVIS_CAPACITY_RETENTION_DAYS") or None,
        help="optional assumed interaction retention window (env: JARVIS_CAPACITY_RETENTION_DAYS)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON only"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = CapacityReporter(
        resolve_db_path(args.db),
        horizon_days=args.horizon_days,
        growth_window_days=args.growth_window_days,
        retention_days=args.retention_days,
    ).generate()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report["database"]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
