#!/usr/bin/env python3
"""End-to-end checks for the current Docker Compose deployment."""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests


CORE_URL = os.environ.get("JARVIS_CORE_URL", "http://127.0.0.1:8000")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
API_TOKEN = os.environ.get("JARVIS_API_TOKEN", "")
DB_PATH = Path(os.environ.get("JARVIS_DB_PATH", "data/jarvis_learning.db"))
API_HEADERS = {"X-Jarvis-Token": API_TOKEN} if API_TOKEN else {}
REQUIRED_CONTAINERS = (
    "jarvis-core",
    "jarvis-prometheus",
    "jarvis-pushgateway",
    "jarvis-grafana",
    "jarvis-cron",
)


class DeploymentChecks:
    def __init__(self) -> None:
        self.failures = []

    def check(self, name, operation) -> None:
        try:
            detail = operation()
            print(f"PASS  {name}" + (f": {detail}" if detail else ""))
        except Exception as error:
            self.failures.append((name, str(error)))
            print(f"FAIL  {name}: {error}")

    def containers(self) -> str:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        running = {
            line.split("\t", 1)[0]: line.split("\t", 1)[1]
            for line in result.stdout.splitlines() if "\t" in line
        }
        missing = [name for name in REQUIRED_CONTAINERS if name not in running]
        unhealthy = [
            name for name in REQUIRED_CONTAINERS
            if name in running and "Up" not in running[name]
        ]
        if missing or unhealthy:
            raise RuntimeError(f"missing={missing}, unhealthy={unhealthy}")
        return f"{len(REQUIRED_CONTAINERS)} running"

    def core_health(self) -> str:
        response = requests.get(f"{CORE_URL}/health", timeout=10)
        response.raise_for_status()
        if response.json().get("status") != "healthy":
            raise RuntimeError(response.text)
        return "healthy"

    def core_status(self) -> str:
        response = requests.get(
            f"{CORE_URL}/api/status", headers=API_HEADERS, timeout=10
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(response.text)
        data = payload["data"]
        return f"model={data['model']}, interactions={data['interactions']}"

    def metrics(self) -> str:
        response = requests.get(f"{CORE_URL}/metrics", timeout=10)
        response.raise_for_status()
        if "jarvis_http_requests_total" not in response.text:
            raise RuntimeError("application metrics are missing")
        return f"{len(response.text.splitlines())} lines"

    def prometheus(self) -> str:
        response = requests.get(f"{PROMETHEUS_URL}/-/healthy", timeout=10)
        response.raise_for_status()
        targets = requests.get(
            f"{PROMETHEUS_URL}/api/v1/targets", timeout=10
        ).json()["data"]["activeTargets"]
        jarvis_targets = [
            target for target in targets
            if target.get("labels", {}).get("job") == "jarvis-core"
        ]
        if not jarvis_targets or jarvis_targets[0]["health"] != "up":
            raise RuntimeError("jarvis-core target is not up")
        return "jarvis-core up"

    def grafana(self) -> str:
        response = requests.get(f"{GRAFANA_URL}/api/health", timeout=10)
        response.raise_for_status()
        return response.json().get("database", "ok")

    def database(self) -> str:
        if not DB_PATH.exists():
            raise RuntimeError(f"missing {DB_PATH}")
        with sqlite3.connect(DB_PATH) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            connection.execute("PRAGMA foreign_keys=ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or violations:
            raise RuntimeError(
                f"integrity={integrity}, foreign_key_violations={len(violations)}"
            )
        return f"{DB_PATH.stat().st_size / 1024:.1f} KiB"

    def security_headers(self) -> str:
        response = requests.get(f"{CORE_URL}/health", timeout=10)
        required = (
            "Content-Security-Policy",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Permissions-Policy",
        )
        missing = [name for name in required if name not in response.headers]
        if missing:
            raise RuntimeError(f"missing {missing}")
        return "present"

    def latency(self) -> str:
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            response = requests.get(f"{CORE_URL}/health", timeout=10)
            response.raise_for_status()
            samples.append((time.perf_counter() - started) * 1000)
        average = sum(samples) / len(samples)
        if average > 500:
            raise RuntimeError(f"average health latency {average:.1f} ms")
        return f"{average:.1f} ms average"

    def run(self) -> bool:
        checks = (
            ("containers", self.containers),
            ("core health", self.core_health),
            ("core status", self.core_status),
            ("metrics", self.metrics),
            ("Prometheus", self.prometheus),
            ("Grafana", self.grafana),
            ("database", self.database),
            ("security headers", self.security_headers),
            ("health latency", self.latency),
        )
        for name, operation in checks:
            self.check(name, operation)
        print(f"\n{len(checks) - len(self.failures)}/{len(checks)} checks passed")
        return not self.failures


if __name__ == "__main__":
    sys.exit(0 if DeploymentChecks().run() else 1)
