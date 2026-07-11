#!/usr/bin/env python3
"""Evidence-based security checks for a Jarvis project checkout.

The audit deliberately limits itself to facts that can be established from the
project tree, its SQLite database, Docker configuration, and an explicitly
configured Jarvis URL. It does not scan machine-wide ports or infer whether a
deployment should use HTTPS.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = Path("data/jarvis_learning.db")
VALID_STATUSES = ("passed", "warning", "critical", "skipped")

SCANNED_SUFFIXES = {
    ".conf",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".key",
    ".pem",
    ".properties",
    ".py",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
SCANNED_NAMES = {"Dockerfile", "Containerfile"}
IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "backups",
    "data",
    "logs",
    "node_modules",
    "venv",
}
MAX_SCANNED_FILE_BYTES = 2 * 1024 * 1024

SECRET_NAME_RE = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|"
    r"(?:access|api|auth|bearer|refresh|session)[_-]?token|"
    r"credential|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
ASSIGNMENT_RE = re.compile(
    r"^\s*(?:(?:export|const|let|var)\s+)?"
    r"(?P<quote>['\"]?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)"
    r"(?:\s*:\s*[A-Za-z_][A-Za-z0-9_\[\], .|]*)?"
    r"\s*(?P<operator>[:=])\s*(?P<value>.+?)\s*$"
)
ENV_TEMPLATE_RE = re.compile(
    r"^\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:(?P<operator>:-|-|:\?|\?)(?P<argument>[^}]*))?\}$"
)
KNOWN_SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[opusr]_[A-Za-z0-9]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)

GROUP_OTHER_WRITE = stat.S_IWGRP | stat.S_IWOTH
GROUP_OTHER_READ = stat.S_IRGRP | stat.S_IROTH
SENSITIVE_TOP_LEVEL_DIRECTORIES = {"backups", "data", "logs"}
CONFIGURATION_FILES = {
    ".env",
    "Containerfile",
    "Dockerfile",
    "config.yaml",
    "config.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
}


class SecurityAuditor:
    """Run security checks against one project root."""

    def __init__(
        self,
        project_root: Path | str = PROJECT_ROOT,
        db_path: Path | str | None = None,
        core_url: Optional[str] = None,
        dependency_timeout: float = 180.0,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        configured_db = db_path or os.environ.get("JARVIS_DB_PATH") or DEFAULT_DATABASE
        self.db_path = self._project_path(configured_db)
        self.core_url = (
            core_url if core_url is not None else os.environ.get("JARVIS_CORE_URL", "")
        ).strip()
        self.dependency_timeout = dependency_timeout
        self.checks: list[dict[str, Any]] = []

    def _project_path(self, value: Path | str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    def _record(
        self,
        check_id: str,
        category: str,
        status: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Unsupported check status: {status}")
        result: dict[str, Any] = {
            "id": check_id,
            "category": category,
            "status": status,
            "message": message,
        }
        if details:
            result["details"] = dict(details)
        self.checks.append(result)

    def run(self) -> dict[str, Any]:
        self.checks.clear()
        if not self.project_root.is_dir():
            self._record(
                "project.root",
                "project",
                "critical",
                "Project root does not exist or is not a directory",
                {"path": str(self.project_root)},
            )
            return self.report()

        self._record(
            "project.root",
            "project",
            "passed",
            "Project root resolved",
            {"path": str(self.project_root)},
        )
        self.audit_secrets()
        self.audit_permissions()
        self.audit_database()
        self.audit_dockerfile()
        self.audit_compose()
        self.audit_security_headers()
        self.audit_dependencies()
        return self.report()

    def report(self) -> dict[str, Any]:
        summary = {status: 0 for status in VALID_STATUSES}
        for check in self.checks:
            summary[check["status"]] += 1
        summary["total"] = len(self.checks)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_root": str(self.project_root),
            "database": str(self.db_path),
            "summary": summary,
            "checks": list(self.checks),
        }

    def _iter_source_and_config_files(self) -> Iterable[Path]:
        own_file = Path(__file__).resolve()
        for path in self.project_root.rglob("*"):
            try:
                relative = path.relative_to(self.project_root)
            except ValueError:
                continue
            if any(part in IGNORED_DIRECTORIES for part in relative.parts[:-1]):
                continue
            if path.is_symlink() or not path.is_file() or path.resolve() == own_file:
                continue
            if not self._is_source_or_config(path):
                continue
            try:
                if path.stat().st_size > MAX_SCANNED_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path

    @staticmethod
    def _is_source_or_config(path: Path) -> bool:
        return (
            path.name in SCANNED_NAMES
            or path.name == ".env"
            or path.name.startswith(".env.")
            or path.suffix.lower() in SCANNED_SUFFIXES
        )

    @staticmethod
    def _literal_value(raw_value: str, suffix: str) -> Optional[str]:
        value = raw_value.strip().rstrip(",;").strip()
        if not value:
            return ""
        if value[0] in {'"', "'"}:
            quote = value[0]
            escaped = False
            end = None
            for index, character in enumerate(value[1:], start=1):
                if character == quote and not escaped:
                    end = index
                    break
                escaped = character == "\\" and not escaped
                if character != "\\":
                    escaped = False
            if end is None:
                return None
            remainder = value[end + 1 :].strip()
            if remainder.startswith((",", ";")):
                remainder = remainder[1:].strip()
            if remainder and not remainder.startswith(("#", "//")):
                return None
            try:
                parsed = ast.literal_eval(value[: end + 1])
            except (SyntaxError, ValueError):
                return None
            return parsed if isinstance(parsed, str) else None

        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if value.lower() in {"none", "null", "nil", "~"}:
            return ""
        if value.startswith(("$", "{{", "<")):
            return value
        if suffix in {".py", ".js"} and value.startswith(("(", "[", "{")):
            return None
        if suffix in {".py", ".js"} and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*(?:\([^)]*\))?", value
        ):
            return None
        if value.startswith(("os.environ", "os.getenv", "getenv(", "process.env")):
            return None
        return value

    @staticmethod
    def _is_placeholder(value: str, relative_path: Path) -> bool:
        stripped = value.strip()
        if not stripped:
            return True
        if stripped.lower() in {"include", "omit", "same-origin"}:
            return True
        if ENV_TEMPLATE_RE.fullmatch(stripped) or re.fullmatch(
            r"\$[A-Za-z_][A-Za-z0-9_]*", stripped
        ):
            return True
        if (
            (stripped.startswith("{{") and stripped.endswith("}}"))
            or (stripped.startswith("<") and stripped.endswith(">"))
        ):
            return True

        normalized = re.sub(r"[^a-z0-9]", "", stripped.lower())
        placeholder_words = (
            "changeme",
            "dummy",
            "example",
            "fake",
            "placeholder",
            "redacted",
            "replaceme",
            "sample",
            "yourapikey",
            "yourpassword",
            "yourtoken",
        )
        if any(word in normalized for word in placeholder_words):
            return True
        if "tests" in relative_path.parts and (
            stripped.lower() == "secret"
            or re.match(
                r"^(?:correct|secret|test|wrong)[_-]", stripped, re.IGNORECASE
            )
        ):
            return True
        return False

    @staticmethod
    def _secret_fingerprint(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    def audit_secrets(self) -> None:
        findings: list[dict[str, Any]] = []
        unreadable: list[str] = []
        scanned = 0

        for path in self._iter_source_and_config_files():
            relative = path.relative_to(self.project_root)
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                unreadable.append(str(relative))
                continue
            scanned += 1
            known_lines: set[int] = set()

            for label, pattern in KNOWN_SECRET_PATTERNS:
                for match in pattern.finditer(content):
                    line_number = content.count("\n", 0, match.start()) + 1
                    known_lines.add(line_number)
                    findings.append({
                        "file": str(relative),
                        "line": line_number,
                        "kind": label,
                        "fingerprint": self._secret_fingerprint(match.group(0)),
                    })

            for line_number, line in enumerate(content.splitlines(), start=1):
                if line_number in known_lines:
                    continue
                assignment = ASSIGNMENT_RE.match(line)
                if not assignment or not SECRET_NAME_RE.search(assignment.group("name")):
                    continue
                value = self._literal_value(assignment.group("value"), path.suffix.lower())
                if value is None or self._is_placeholder(value, relative):
                    continue
                findings.append({
                    "file": str(relative),
                    "line": line_number,
                    "kind": "literal assigned to a secret-like setting",
                    "setting": assignment.group("name"),
                    "fingerprint": self._secret_fingerprint(value),
                })

        details = {"scanned_files": scanned, "findings": findings, "unreadable": unreadable}
        if findings:
            status = "critical"
            message = f"Found {len(findings)} suspected hardcoded secret(s)"
        elif unreadable:
            status = "warning"
            message = "No hardcoded secret found, but candidate files were unreadable"
        else:
            status = "passed"
            message = "No suspected hardcoded secrets found"
        self._record("source.hardcoded_secrets", "source", status, message, details)

    def _permission_targets(self) -> Iterable[Path]:
        for path in self.project_root.rglob("*"):
            try:
                relative = path.relative_to(self.project_root)
            except ValueError:
                continue
            ignored = IGNORED_DIRECTORIES - SENSITIVE_TOP_LEVEL_DIRECTORIES
            if any(part in ignored for part in relative.parts):
                continue
            if path.is_symlink():
                continue
            if path.is_dir():
                yield path
            elif path.is_file() and (
                path.name in CONFIGURATION_FILES
                or self._is_source_or_config(path)
                or relative.parts[0] in SENSITIVE_TOP_LEVEL_DIRECTORIES
            ):
                yield path

    @staticmethod
    def _is_read_sensitive(relative: Path, is_directory: bool) -> bool:
        if relative.name == ".gitkeep":
            return False
        if relative.name == ".env" or relative.name.startswith(".env."):
            return True
        return bool(relative.parts) and relative.parts[0] in SENSITIVE_TOP_LEVEL_DIRECTORIES

    def audit_permissions(self) -> None:
        write_violations: list[dict[str, str]] = []
        read_exposures: list[dict[str, str]] = []
        checked = 0

        for path in self._permission_targets():
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except OSError:
                continue
            checked += 1
            relative = path.relative_to(self.project_root)
            if mode & GROUP_OTHER_WRITE:
                write_violations.append({
                    "path": str(relative),
                    "mode": oct(mode),
                    "bits": oct(mode & GROUP_OTHER_WRITE),
                })
            if self._is_read_sensitive(relative, path.is_dir()) and mode & GROUP_OTHER_READ:
                read_exposures.append({
                    "path": str(relative),
                    "mode": oct(mode),
                    "bits": oct(mode & GROUP_OTHER_READ),
                })

        details = {
            "checked_paths": checked,
            "group_or_other_writable": write_violations,
            "sensitive_group_or_other_readable": read_exposures,
        }
        if write_violations:
            status = "critical"
            message = "Group or other users can modify audited project paths"
        elif read_exposures:
            status = "warning"
            message = "Sensitive data paths are readable by group or other users"
        else:
            status = "passed"
            message = "No prohibited group/other permission bits found"
        self._record("filesystem.permissions", "filesystem", status, message, details)

    def audit_database(self) -> None:
        if not self.db_path.exists():
            self._record(
                "database.integrity",
                "database",
                "skipped",
                "SQLite database does not exist",
                {"path": str(self.db_path)},
            )
            return
        if not self.db_path.is_file():
            self._record(
                "database.integrity",
                "database",
                "critical",
                "Configured SQLite path is not a regular file",
                {"path": str(self.db_path)},
            )
            return

        try:
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as connection:
                integrity_rows = [
                    str(row[0]) for row in connection.execute("PRAGMA integrity_check")
                ]
                connection.execute("PRAGMA foreign_keys=ON")
                foreign_key_rows = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
        except sqlite3.Error as exc:
            self._record(
                "database.integrity",
                "database",
                "critical",
                "SQLite validation failed",
                {"path": str(self.db_path), "error": str(exc)},
            )
            return

        integrity_ok = integrity_rows == ["ok"]
        details = {
            "path": str(self.db_path),
            "integrity": integrity_rows,
            "foreign_key_violation_count": len(foreign_key_rows),
            "foreign_key_violations": [list(row) for row in foreign_key_rows[:20]],
        }
        if not integrity_ok or foreign_key_rows:
            self._record(
                "database.integrity",
                "database",
                "critical",
                "SQLite integrity or foreign-key validation failed",
                details,
            )
        else:
            self._record(
                "database.integrity",
                "database",
                "passed",
                "SQLite integrity and foreign keys are valid",
                details,
            )

    @staticmethod
    def _final_docker_user(path: Path) -> Optional[str]:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        final_user: Optional[str] = None
        logical_content = re.sub(r"\\\s*\n\s*", " ", content)
        for raw_line in logical_content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = re.match(r"(?P<directive>[A-Za-z]+)\s+(?P<argument>.*)", line)
            if not parsed:
                continue
            directive = parsed.group("directive").upper()
            argument = parsed.group("argument")
            if directive == "FROM":
                final_user = None
            elif directive == "USER":
                final_user = argument.split(" #", 1)[0].strip()
        return final_user

    @staticmethod
    def _user_security(user: Any) -> tuple[str, str]:
        if user is None or str(user).strip() == "":
            return "unknown", "no user configured"
        value = str(user).strip()
        if value.startswith("${") and "}" in value:
            principal = value[: value.index("}") + 1]
        else:
            principal = value.split(":", 1)[0]
        if principal.lower() == "root" or principal == "0":
            return "root", value
        if principal.startswith("$"):
            template = ENV_TEMPLATE_RE.fullmatch(principal)
            if template is None:
                return "unknown", value
        else:
            template = None
        if template:
            default = template.group("argument")
            if default is None or default == "":
                return "unknown", value
            if default.lower() == "root" or default == "0":
                return "root", value
        return "non-root", value

    def audit_dockerfile(self) -> None:
        dockerfile = self.project_root / "Dockerfile"
        if not dockerfile.is_file():
            self._record(
                "container.dockerfile_user",
                "container",
                "skipped",
                "Dockerfile is not present",
            )
            return
        final_user = self._final_docker_user(dockerfile)
        security, evidence = self._user_security(final_user)
        if security == "non-root":
            status = "passed"
            message = "Final Dockerfile stage selects a non-root user"
        elif security == "root":
            status = "critical"
            message = "Final Dockerfile stage selects root"
        else:
            status = "critical"
            message = "Final Dockerfile stage has no verifiable non-root USER"
        self._record(
            "container.dockerfile_user",
            "container",
            status,
            message,
            {"user": evidence},
        )

    @staticmethod
    def _environment_map(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
        result: dict[str, Any] = {}
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, str):
                    continue
                key, separator, configured = item.partition("=")
                result[key] = configured if separator else None
        return result

    @staticmethod
    def _is_token_template(value: Any) -> bool:
        template = ENV_TEMPLATE_RE.fullmatch(str(value).strip())
        if not template or template.group("name") != "JARVIS_API_TOKEN":
            return False
        operator = template.group("operator")
        argument = template.group("argument")
        return operator is None or operator in {":?", "?"} or (
            operator in {":-", "-"} and argument == ""
        )

    @staticmethod
    def _is_loopback_host(value: Any) -> bool:
        host = str(value).strip()
        template = ENV_TEMPLATE_RE.fullmatch(host)
        if template:
            default = template.group("argument")
            if template.group("operator") not in {":-", "-"} or not default:
                return False
            host = default
        host = host.strip("[]")
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @classmethod
    def _published_port_host(cls, port: Any) -> Optional[str]:
        if isinstance(port, dict):
            if "published" not in port:
                return None
            host = port.get("host_ip")
            return str(host) if host is not None else ""
        value = str(port).strip()
        match = re.fullmatch(
            r"(?P<host>\$\{[^}]+\}|\[[^]]+\]|[^:]+):"
            r"(?P<published>\$\{[^}]+\}|[^:]+):(?P<target>[^:]+)",
            value,
        )
        return match.group("host") if match else ""

    def _record_compose_group(
        self,
        status: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        for check_id in (
            "container.compose_ports",
            "container.compose_token",
            "container.compose_users",
        ):
            self._record(check_id, "container", status, message, details)

    def audit_compose(self) -> None:
        compose_path = next(
            (
                candidate
                for candidate in (
                    self.project_root / "docker-compose.yml",
                    self.project_root / "docker-compose.yaml",
                )
                if candidate.is_file()
            ),
            None,
        )
        if compose_path is None:
            self._record_compose_group("skipped", "Compose file is not present")
            return

        try:
            import yaml
        except ImportError as exc:
            self._record_compose_group(
                "critical",
                "Compose configuration could not be audited",
                {"error": str(exc)},
            )
            return

        try:
            document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
            if not isinstance(document, dict):
                raise ValueError("Compose document must be a mapping")
            services = document.get("services", {})
            if not isinstance(services, dict):
                raise ValueError("services must be a mapping")
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
            self._record_compose_group(
                "critical",
                "Compose configuration could not be audited",
                {"error": str(exc)},
            )
            return

        unsafe_ports: list[dict[str, str]] = []
        published_count = 0
        for service_name, raw_service in services.items():
            service = raw_service if isinstance(raw_service, dict) else {}
            if str(service.get("network_mode", "")).lower() == "host":
                unsafe_ports.append({
                    "service": str(service_name),
                    "port": "network_mode",
                    "host": "host network",
                })
            ports = service.get("ports", [])
            if not isinstance(ports, list):
                ports = [ports]
            for port in ports:
                published_count += 1
                host = self._published_port_host(port)
                if host is None:
                    continue
                if not host or not self._is_loopback_host(host):
                    unsafe_ports.append({
                        "service": str(service_name),
                        "port": str(port),
                        "host": host or "all interfaces",
                    })
        port_details = {"published_ports": published_count, "unsafe": unsafe_ports}
        if unsafe_ports:
            port_status = "critical"
            port_message = "Compose publishes ports beyond an explicit loopback address"
        else:
            port_status = "passed"
            port_message = "All published Compose ports are bound to loopback"
        self._record(
            "container.compose_ports",
            "container",
            port_status,
            port_message,
            port_details,
        )

        application_services: list[tuple[str, Mapping[str, Any]]] = []
        for service_name, raw_service in services.items():
            service = raw_service if isinstance(raw_service, dict) else {}
            image = str(service.get("image", ""))
            if service.get("build") is not None or image.startswith("jarvis-learning"):
                application_services.append((str(service_name), service))

        token_issues: list[dict[str, str]] = []
        for service_name, service in application_services:
            environment = self._environment_map(service.get("environment", {}))
            value = environment.get("JARVIS_API_TOKEN")
            if not self._is_token_template(value):
                token_issues.append({
                    "service": service_name,
                    "issue": (
                        "JARVIS_API_TOKEN must use its environment placeholder "
                        "without a literal default"
                    ),
                })
        token_details: dict[str, Any]
        if not application_services:
            token_status = "skipped"
            token_message = "No local Jarvis application service found in Compose"
            token_details = {}
        elif token_issues:
            token_status = "critical"
            token_message = "Jarvis services do not safely source the API token"
            token_details = {"issues": token_issues}
        else:
            token_status = "passed"
            token_message = "Jarvis services use the JARVIS_API_TOKEN placeholder"
            token_details = {"services": [name for name, _ in application_services]}
        self._record(
            "container.compose_token",
            "container",
            token_status,
            token_message,
            token_details,
        )

        final_docker_user = self._final_docker_user(self.project_root / "Dockerfile")
        docker_user_security, _ = self._user_security(final_docker_user)
        root_services: list[dict[str, str]] = []
        unknown_services: list[dict[str, str]] = []
        verified_services: list[str] = []
        for service_name, raw_service in services.items():
            service = raw_service if isinstance(raw_service, dict) else {}
            configured_user = service.get("user")
            security, evidence = self._user_security(configured_user)
            if security == "non-root":
                verified_services.append(str(service_name))
            elif security == "root":
                root_services.append({"service": str(service_name), "user": evidence})
            elif service.get("build") is not None and docker_user_security == "non-root":
                verified_services.append(str(service_name))
            else:
                unknown_services.append({
                    "service": str(service_name),
                    "reason": "external image has no explicit non-root user",
                })
        user_details = {
            "verified_non_root": verified_services,
            "explicit_root": root_services,
            "unverified": unknown_services,
        }
        if root_services:
            user_status = "critical"
            user_message = "One or more Compose services explicitly run as root"
        elif unknown_services:
            user_status = "warning"
            user_message = "Some Compose service users cannot be verified locally"
        else:
            user_status = "passed"
            user_message = "All Compose services have a verifiable non-root user"
        self._record(
            "container.compose_users",
            "container",
            user_status,
            user_message,
            user_details,
        )

    def audit_security_headers(self) -> None:
        if not self.core_url:
            self._record(
                "http.security_headers",
                "http",
                "skipped",
                "JARVIS_CORE_URL is not set; live security headers were not tested",
            )
            return
        parsed = urlparse(self.core_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            self._record(
                "http.security_headers",
                "http",
                "warning",
                "JARVIS_CORE_URL is not a valid HTTP(S) URL",
                {"url": self.core_url},
            )
            return

        try:
            import requests

            response = requests.get(
                f"{self.core_url.rstrip('/')}/health",
                timeout=5,
                allow_redirects=False,
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(f"health endpoint returned HTTP {response.status_code}")
        except Exception as exc:
            self._record(
                "http.security_headers",
                "http",
                "warning",
                "Live security-header request failed",
                {"url": self.core_url, "error": str(exc)},
            )
            return

        headers = {name.lower(): value for name, value in response.headers.items()}
        invalid: list[str] = []
        if not headers.get("content-security-policy", "").strip():
            invalid.append("Content-Security-Policy")
        if headers.get("x-content-type-options", "").lower() != "nosniff":
            invalid.append("X-Content-Type-Options: nosniff")
        if headers.get("x-frame-options", "").upper() not in {"DENY", "SAMEORIGIN"}:
            invalid.append("X-Frame-Options: DENY|SAMEORIGIN")
        if not headers.get("referrer-policy", "").strip():
            invalid.append("Referrer-Policy")
        if not headers.get("permissions-policy", "").strip():
            invalid.append("Permissions-Policy")

        if invalid:
            self._record(
                "http.security_headers",
                "http",
                "critical",
                "Live Jarvis response is missing required security headers",
                {"url": self.core_url, "missing_or_invalid": invalid},
            )
        else:
            self._record(
                "http.security_headers",
                "http",
                "passed",
                "Live Jarvis response includes the required security headers",
                {"url": self.core_url},
            )

    @staticmethod
    def _pip_audit_command() -> Optional[list[str]]:
        executable = shutil.which("pip-audit")
        if executable:
            return [executable]
        try:
            available = importlib.util.find_spec("pip_audit") is not None
        except (ImportError, ValueError):
            available = False
        return [sys.executable, "-m", "pip_audit"] if available else None

    @staticmethod
    def _vulnerability_count(output: str) -> Optional[int]:
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return None
        if isinstance(payload, dict):
            dependencies = payload.get("dependencies", [])
        elif isinstance(payload, list):
            dependencies = payload
        else:
            return None
        if not isinstance(dependencies, list):
            return None
        return sum(
            len(item.get("vulns", []))
            for item in dependencies
            if isinstance(item, dict) and isinstance(item.get("vulns", []), list)
        )

    def audit_dependencies(self) -> None:
        requirements = self.project_root / "requirements.txt"
        if not requirements.is_file():
            self._record(
                "dependencies.pip_audit",
                "dependencies",
                "skipped",
                "requirements.txt is not present",
            )
            return
        command = self._pip_audit_command()
        if command is None:
            self._record(
                "dependencies.pip_audit",
                "dependencies",
                "skipped",
                "pip-audit is not installed; dependency vulnerabilities were not scanned",
            )
            return

        command.extend([
            "--requirement",
            str(requirements),
            "--cache-dir",
            os.environ.get(
                "PIP_AUDIT_CACHE_DIR",
                str(Path(tempfile.gettempdir()) / "jarvis-pip-audit-cache"),
            ),
            "--format",
            "json",
            "--progress-spinner",
            "off",
        ])
        try:
            completed = subprocess.run(
                command,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=self.dependency_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._record(
                "dependencies.pip_audit",
                "dependencies",
                "warning",
                "pip-audit could not complete",
                {"error": str(exc)},
            )
            return

        vulnerability_count = self._vulnerability_count(completed.stdout)
        details: dict[str, Any] = {
            "exit_code": completed.returncode,
            "vulnerability_count": vulnerability_count,
        }
        if completed.stderr.strip():
            details["stderr"] = completed.stderr.strip()[-1000:]
        if completed.returncode == 0:
            self._record(
                "dependencies.pip_audit",
                "dependencies",
                "passed",
                "pip-audit found no known dependency vulnerabilities",
                details,
            )
        elif completed.returncode == 1:
            self._record(
                "dependencies.pip_audit",
                "dependencies",
                "critical",
                "pip-audit reported known dependency vulnerabilities",
                details,
            )
        else:
            self._record(
                "dependencies.pip_audit",
                "dependencies",
                "warning",
                "pip-audit failed; dependency status is unknown",
                details,
            )


def _render_console(report: Mapping[str, Any]) -> None:
    print(f"Security audit: {report['project_root']}")
    for check in report["checks"]:
        print(f"[{check['status'].upper():8}] {check['id']}: {check['message']}")
    summary = report["summary"]
    print(
        "Summary: "
        f"{summary['passed']} passed, {summary['warning']} warning, "
        f"{summary['critical']} critical, {summary['skipped']} skipped"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="project directory (default: directory above this script)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        help="SQLite path, relative to the project root when not absolute",
    )
    parser.add_argument(
        "--core-url",
        help="optional live Jarvis base URL (defaults to JARVIS_CORE_URL)",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="write the structured report to this path",
    )
    parser.add_argument(
        "--dependency-timeout",
        type=float,
        default=180.0,
        help="pip-audit timeout in seconds (default: 180)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.dependency_timeout <= 0:
        _build_parser().error("--dependency-timeout must be positive")

    auditor = SecurityAuditor(
        project_root=args.project_root,
        db_path=args.db_path,
        core_url=args.core_url,
        dependency_timeout=args.dependency_timeout,
    )
    report = auditor.run()
    _render_console(report)

    if args.json_output is not None:
        output_path = args.json_output.expanduser()
        if not output_path.is_absolute():
            output_path = auditor.project_root / output_path
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"Could not write JSON report: {exc}", file=sys.stderr)
            return 2
        print(f"JSON report: {output_path}")

    return 1 if report["summary"]["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
