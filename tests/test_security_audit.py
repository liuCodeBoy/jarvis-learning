import importlib.util
import json
import shutil
import sqlite3
import subprocess

import requests

from scripts.security_audit import PROJECT_ROOT, SecurityAuditor, main


def result_for(auditor, check_id):
    return next(check for check in auditor.checks if check["id"] == check_id)


def test_default_project_root_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert SecurityAuditor().project_root == PROJECT_ROOT


def test_secret_scan_ignores_placeholders_and_redacts_real_secret(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    secret = "sk-ant-" + ("A" * 32)
    (project / "app.py").write_text(
        "import os\n"
        "api_key = os.getenv('ANTHROPIC_API_KEY')\n"
        "self.auth_token = env_token or local_token or ''\n"
        f"client_secret = '{secret}'  # accidental credential\n",
        encoding="utf-8",
    )
    (project / "config.yaml").write_text(
        "auth_token: ${AUTH_TOKEN:-}\n",
        encoding="utf-8",
    )
    (project / ".env.production").write_text(
        "JARVIS_API_TOKEN=${JARVIS_API_TOKEN:-}\n",
        encoding="utf-8",
    )
    (project / "client.js").write_text(
        'credentials: "same-origin"\n',
        encoding="utf-8",
    )

    auditor = SecurityAuditor(project)
    auditor.audit_secrets()

    check = result_for(auditor, "source.hardcoded_secrets")
    serialized = json.dumps(check)
    assert check["status"] == "critical"
    assert len(check["details"]["findings"]) == 1
    assert check["details"]["findings"][0]["kind"] == "Anthropic API key"
    assert secret not in serialized
    assert "fingerprint" in serialized


def test_database_audit_retries_a_transient_read_only_open(tmp_path, monkeypatch):
    project = tmp_path / "project"
    database = project / "data" / "jarvis.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE item (id INTEGER PRIMARY KEY)")

    real_connect = sqlite3.connect
    attempts = 0

    def flaky_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("scripts.security_audit.sqlite3.connect", flaky_connect)
    monkeypatch.setattr("scripts.security_audit.time.sleep", lambda _delay: None)
    auditor = SecurityAuditor(project, db_path=database)

    auditor.audit_database()

    check = result_for(auditor, "database.integrity")
    assert attempts == 2
    assert check["status"] == "passed"


def test_permission_audit_uses_group_and_other_write_masks(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = project / "config.yaml"
    config.write_text("learning: false\n", encoding="utf-8")
    config.chmod(0o620)

    auditor = SecurityAuditor(project)
    auditor.audit_permissions()

    check = result_for(auditor, "filesystem.permissions")
    assert check["status"] == "critical"
    assert check["details"]["group_or_other_writable"] == [{
        "path": "config.yaml",
        "mode": "0o620",
        "bits": "0o20",
    }]


def test_permission_audit_reports_sensitive_read_bits_as_warning(tmp_path):
    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    data.chmod(0o700)
    database = data / "jarvis.db"
    database.write_bytes(b"not needed for this check")
    database.chmod(0o604)

    auditor = SecurityAuditor(project, db_path=database)
    auditor.audit_permissions()

    check = result_for(auditor, "filesystem.permissions")
    assert check["status"] == "warning"
    assert check["details"]["sensitive_group_or_other_readable"] == [{
        "path": "data/jarvis.db",
        "mode": "0o604",
        "bits": "0o4",
    }]


def test_database_audit_runs_integrity_and_foreign_key_checks(tmp_path):
    project = tmp_path / "project"
    database = project / "data" / "jarvis.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
        )
        connection.execute("INSERT INTO child VALUES (42)")

    auditor = SecurityAuditor(project, db_path=database)
    auditor.audit_database()

    check = result_for(auditor, "database.integrity")
    assert check["status"] == "critical"
    assert check["details"]["integrity"] == ["ok"]
    assert check["details"]["foreign_key_violation_count"] == 1


def test_docker_audit_accepts_final_non_root_and_loopback_placeholders(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "Dockerfile").write_text(
        "FROM python:3.12 AS builder\n"
        "USER root\n"
        "FROM python:3.12-slim\n"
        "USER\tapp\n",
        encoding="utf-8",
    )
    (project / "docker-compose.yml").write_text(
        "services:\n"
        "  app:\n"
        "    build: .\n"
        '    user: "${JARVIS_UID:-1000}:${JARVIS_GID:-1000}"\n'
        "    environment:\n"
        '      JARVIS_API_TOKEN: "${JARVIS_API_TOKEN:-}"\n'
        "    ports:\n"
        '      - "${JARVIS_BIND_HOST:-127.0.0.1}:${JARVIS_PORT:-8000}:8000"\n',
        encoding="utf-8",
    )

    auditor = SecurityAuditor(project)
    auditor.audit_dockerfile()
    auditor.audit_compose()

    assert result_for(auditor, "container.dockerfile_user")["status"] == "passed"
    assert result_for(auditor, "container.compose_ports")["status"] == "passed"
    assert result_for(auditor, "container.compose_token")["status"] == "passed"
    assert result_for(auditor, "container.compose_users")["status"] == "passed"


def test_docker_audit_rejects_root_public_port_and_literal_token(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "Dockerfile").write_text(
        "FROM python:3.12-slim\nUSER root\n",
        encoding="utf-8",
    )
    (project / "docker-compose.yml").write_text(
        "services:\n"
        "  app:\n"
        "    build: .\n"
        "    user: root\n"
        "    environment:\n"
        "      JARVIS_API_TOKEN: hardcoded-token\n"
        "    ports:\n"
        '      - "8000:8000"\n',
        encoding="utf-8",
    )

    auditor = SecurityAuditor(project)
    auditor.audit_dockerfile()
    auditor.audit_compose()

    for check_id in (
        "container.dockerfile_user",
        "container.compose_ports",
        "container.compose_token",
        "container.compose_users",
    ):
        assert result_for(auditor, check_id)["status"] == "critical"


def test_compose_audit_rejects_host_network_and_reports_invalid_yaml(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    compose = project / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    build: .\n"
        "    user: '1000'\n"
        "    network_mode: host\n"
        "    environment:\n"
        '      JARVIS_API_TOKEN: "${JARVIS_API_TOKEN:-}"\n',
        encoding="utf-8",
    )
    auditor = SecurityAuditor(project)
    auditor.audit_compose()
    assert result_for(auditor, "container.compose_ports")["status"] == "critical"

    compose.write_text("services: [\n", encoding="utf-8")
    invalid = SecurityAuditor(project)
    invalid.audit_compose()
    for check in invalid.checks:
        assert check["status"] == "critical"


def test_variable_docker_user_without_a_default_is_not_assumed_non_root(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "Dockerfile").write_text(
        "FROM python:3.12-slim\nUSER $APP_USER\n",
        encoding="utf-8",
    )

    auditor = SecurityAuditor(project)
    auditor.audit_dockerfile()

    assert result_for(auditor, "container.dockerfile_user")["status"] == "critical"


def test_security_header_check_is_optional_and_can_test_live_response(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    skipped = SecurityAuditor(project, core_url="")
    skipped.audit_security_headers()
    assert result_for(skipped, "http.security_headers")["status"] == "skipped"

    class Response:
        status_code = 200
        headers = {
            "Content-Security-Policy": "default-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        }

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    live = SecurityAuditor(project, core_url="http://127.0.0.1:8000")
    live.audit_security_headers()

    check = result_for(live, "http.security_headers")
    assert check["status"] == "critical"
    assert check["details"]["missing_or_invalid"] == ["Permissions-Policy"]
    assert calls == [(
        "http://127.0.0.1:8000/health",
        {"timeout": 5, "allow_redirects": False},
    )]


def test_dependency_audit_unavailable_is_skipped_not_passed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("Flask>=3\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)

    auditor = SecurityAuditor(project)
    auditor.audit_dependencies()

    check = result_for(auditor, "dependencies.pip_audit")
    assert check["status"] == "skipped"
    assert auditor.report()["summary"]["passed"] == 0


def test_dependency_audit_honors_vulnerability_exit_code(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    requirements = project / "requirements.txt"
    requirements.write_text("example==1\n", encoding="utf-8")
    monkeypatch.setattr(
        SecurityAuditor,
        "_pip_audit_command",
        staticmethod(lambda: ["pip-audit"]),
    )
    payload = {
        "dependencies": [{"name": "example", "vulns": [{"id": "CVE-1"}]}]
    }

    def fake_run(command, **kwargs):
        assert command[:3] == ["pip-audit", "--requirement", str(requirements)]
        assert kwargs["cwd"] == project
        return subprocess.CompletedProcess(command, 1, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    auditor = SecurityAuditor(project)
    auditor.audit_dependencies()

    check = result_for(auditor, "dependencies.pip_audit")
    assert check["status"] == "critical"
    assert check["details"]["exit_code"] == 1
    assert check["details"]["vulnerability_count"] == 1


def test_cli_writes_optional_json_and_returns_nonzero_only_for_critical(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    hardcoded = "production-static-value"
    (project / "app.py").write_text(
        f'JARVIS_API_TOKEN = "{hardcoded}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("JARVIS_CORE_URL", raising=False)

    exit_code = main([
        "--project-root",
        str(project),
        "--json-output",
        "reports/security.json",
    ])

    report_path = project / "reports" / "security.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["summary"]["critical"] == 1
    assert hardcoded not in report_path.read_text(encoding="utf-8")


def test_cli_returns_zero_when_checks_are_only_passed_or_skipped(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.delenv("JARVIS_CORE_URL", raising=False)
    monkeypatch.delenv("JARVIS_DB_PATH", raising=False)

    assert main(["--project-root", str(project)]) == 0
