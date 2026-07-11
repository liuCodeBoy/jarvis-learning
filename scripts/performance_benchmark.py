#!/usr/bin/env python3
"""Benchmark the public HTTP and storage surfaces of J.A.R.V.I.S."""

from __future__ import annotations

import concurrent.futures
import argparse
import json
import os
import sqlite3
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CORE_URL = "http://127.0.0.1:8000"
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090"


def _project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


class BenchmarkRequestError(RuntimeError):
    """Raised when a benchmark target returns an invalid or failed response."""


def _percentile(samples: Iterable[float], percentile: float) -> float:
    """Return an interpolated percentile, including for a single sample."""
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("samples must not be empty")
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class PerformanceBenchmark:
    """Performance checks aligned with the current Flask API contract."""

    API_ENDPOINT_NAMES = ("健康检查", "监控指标", "系统状态")

    def __init__(
        self,
        core_url: Optional[str] = None,
        prometheus_url: Optional[str] = None,
        db_path: Optional[Path] = None,
        api_token: Optional[str] = None,
        report_path: Optional[Path] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.core_url = (
            core_url or os.getenv("JARVIS_CORE_URL", DEFAULT_CORE_URL)
        ).rstrip("/")
        self.prometheus_url = (
            prometheus_url
            or os.getenv("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL)
        ).rstrip("/")
        configured_db = os.getenv("JARVIS_DB_PATH", "data/jarvis_learning.db")
        self.db_path = Path(db_path) if db_path is not None else _project_path(configured_db)
        token = api_token if api_token is not None else os.getenv("JARVIS_API_TOKEN", "")
        self.api_headers = {"X-Jarvis-Token": token} if token else {}
        self.report_path = (
            Path(report_path) if report_path is not None
            else PROJECT_DIR / "performance_benchmark_report.json"
        )
        self.http = session or requests.Session()
        self.results: Dict[str, Dict[str, Any]] = {}
        self.targets = {
            "response_time_ms": 1_000,
            "throughput_rps": 100,
            "success_rate": 0.95,
            "learning_endpoint_time_s": 60,
            "evolution_endpoint_time_s": 1,
            "db_query_time_ms": 50,
        }

    def _request_api_data(
        self, method: str, path: str, *, timeout: float
    ) -> Dict[str, Any]:
        """Call an API route and validate its ``{ok, data, error}`` envelope."""
        response = self.http.request(
            method,
            f"{self.core_url}{path}",
            headers=self.api_headers,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise BenchmarkRequestError(
                f"{path} 返回了非 JSON 响应（HTTP {response.status_code}）"
            ) from error

        if not isinstance(payload, dict):
            raise BenchmarkRequestError(f"{path} 的 JSON 顶层必须是对象")
        if not 200 <= response.status_code < 300 or payload.get("ok") is not True:
            api_error = payload.get("error")
            if isinstance(api_error, dict):
                code = api_error.get("code", "api_error")
                message = api_error.get("message", "请求失败")
            else:
                code, message = "http_error", "请求失败"
            raise BenchmarkRequestError(
                f"{path} 请求失败（HTTP {response.status_code}, {code}）：{message}"
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise BenchmarkRequestError(f"{path} 响应缺少对象类型的 data 字段")
        return data

    def run_all_tests(self) -> float:
        print(f"\n{Colors.BOLD}{Colors.BLUE}贾维斯系统性能基准测试{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.RESET}\n")
        print(f"测试开始时间: {datetime.now().isoformat()}\n")

        self.test_api_response_time()
        self.test_concurrent_requests()
        self.test_learning_system()
        self.test_evolution_engine()
        self.test_database_performance()
        self.test_monitoring_system()
        return self.generate_benchmark_report()

    def test_api_response_time(self) -> None:
        print(f"\n{Colors.BOLD}测试1: API响应时间测试{Colors.RESET}")
        print(f"{Colors.BLUE}{'-' * 70}{Colors.RESET}")
        endpoints: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("健康检查", self._check_health),
            ("监控指标", self._check_metrics),
            (
                "系统状态",
                lambda: self._request_api_data("GET", "/api/status", timeout=10),
            ),
        )

        for name, operation in endpoints:
            response_times = []
            success_count = 0
            last_error: Optional[str] = None
            for _ in range(100):
                started = time.perf_counter()
                try:
                    operation()
                    success_count += 1
                except (requests.RequestException, BenchmarkRequestError) as error:
                    last_error = str(error)
                finally:
                    response_times.append((time.perf_counter() - started) * 1_000)

            avg_time = statistics.mean(response_times)
            median_time = statistics.median(response_times)
            success_rate = success_count / len(response_times)
            self.results[name] = {
                "avg_time_ms": avg_time,
                "median_time_ms": median_time,
                "p95_time_ms": _percentile(response_times, 0.95),
                "p99_time_ms": _percentile(response_times, 0.99),
                "success_rate": success_rate,
                "total_requests": len(response_times),
            }
            if last_error:
                self.results[name]["last_error"] = last_error
            self._print_http_result(name, self.results[name])

    def _check_health(self) -> None:
        response = self.http.get(f"{self.core_url}/health", timeout=10)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise BenchmarkRequestError("/health 返回了非 JSON 响应") from error
        if not isinstance(payload, dict) or payload.get("status") != "healthy":
            raise BenchmarkRequestError("/health 未报告 healthy")

    def _check_metrics(self) -> None:
        response = self.http.get(f"{self.core_url}/metrics", timeout=10)
        response.raise_for_status()
        if "jarvis_http_requests_total" not in response.text:
            raise BenchmarkRequestError("/metrics 缺少 jarvis_http_requests_total")

    def _print_http_result(self, name: str, result: Dict[str, Any]) -> None:
        passed = (
            result["avg_time_ms"] < self.targets["response_time_ms"]
            and result["success_rate"] >= self.targets["success_rate"]
        )
        icon = (
            f"{Colors.GREEN}✓{Colors.RESET}"
            if passed
            else f"{Colors.RED}✗{Colors.RESET}"
        )
        print(f"{icon} {name}")
        print(
            f"  平均/中位数: {result['avg_time_ms']:.2f}ms / "
            f"{result['median_time_ms']:.2f}ms"
        )
        print(f"  P95/P99: {result['p95_time_ms']:.2f}ms / {result['p99_time_ms']:.2f}ms")
        print(f"  成功率: {result['success_rate'] * 100:.1f}%")
        if result.get("last_error"):
            print(f"  最近错误: {result['last_error']}")

    def test_concurrent_requests(self) -> None:
        print(f"\n{Colors.BOLD}测试2: 并发请求压力测试{Colors.RESET}")
        print(f"{Colors.BLUE}{'-' * 70}{Colors.RESET}")

        for concurrency in (10, 50, 100, 200):
            print(f"\n并发数: {concurrency}")

            def make_request() -> Dict[str, Any]:
                started = time.perf_counter()
                try:
                    self._check_health()
                    return {
                        "success": True,
                        "response_time": (time.perf_counter() - started) * 1_000,
                    }
                except (requests.RequestException, BenchmarkRequestError):
                    return {"success": False, "response_time": None}

            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = [executor.submit(make_request) for _ in range(concurrency * 10)]
                samples = [future.result() for future in concurrent.futures.as_completed(futures)]
            total_time = time.perf_counter() - started

            successful = [sample for sample in samples if sample["success"]]
            response_times = [sample["response_time"] for sample in successful]
            result = {
                "throughput_rps": len(samples) / total_time,
                "success_rate": len(successful) / len(samples),
                "avg_response_time_ms": statistics.mean(response_times) if response_times else 0,
                "total_requests": len(samples),
                "successful_requests": len(successful),
                "total_time_s": total_time,
            }
            self.results[f"并发{concurrency}"] = result
            print(f"  吞吐量: {result['throughput_rps']:.2f} RPS")
            print(f"  成功率: {result['success_rate'] * 100:.1f}%")
            print(f"  平均响应时间: {result['avg_response_time_ms']:.2f}ms")
            print(f"  总请求数/耗时: {len(samples)} / {total_time:.2f}s")

    def test_learning_system(self) -> None:
        print(f"\n{Colors.BOLD}测试3: 学习接口性能测试{Colors.RESET}")
        print(f"{Colors.BLUE}{'-' * 70}{Colors.RESET}")
        started = time.perf_counter()
        try:
            data = self._request_api_data("POST", "/api/learn", timeout=120)
            cycle_time = time.perf_counter() - started
            patterns = data.get("patterns")
            if not isinstance(patterns, list):
                raise BenchmarkRequestError("/api/learn 的 patterns 字段必须是数组")
            confidences = [
                float(pattern["confidence"])
                for pattern in patterns
                if isinstance(pattern, dict)
                and isinstance(pattern.get("confidence"), (int, float))
            ]
            result = {
                "cycle_time_s": cycle_time,
                "patterns_found": len(patterns),
                "average_confidence": statistics.mean(confidences) if confidences else 0,
                "ai_analysis_available": bool(data.get("ai_analysis")),
                "success": True,
            }
            self.results["学习接口"] = result
            print(f"  接口耗时: {cycle_time:.2f}s")
            print(f"  发现模式数: {len(patterns)}")
            print(f"  平均置信度: {result['average_confidence']:.3f}")
        except (requests.RequestException, BenchmarkRequestError) as error:
            print(f"{Colors.RED}✗ 学习接口测试失败: {error}{Colors.RESET}")
            self.results["学习接口"] = {"success": False, "error": str(error)}

    def test_evolution_engine(self) -> None:
        print(f"\n{Colors.BOLD}测试4: 进化状态接口性能测试{Colors.RESET}")
        print(f"{Colors.BLUE}{'-' * 70}{Colors.RESET}")
        started = time.perf_counter()
        try:
            data = self._request_api_data("GET", "/api/evolve", timeout=30)
            elapsed = time.perf_counter() - started
            history = data.get("history")
            available_cases = data.get("available_cases")
            if not isinstance(history, list) or not isinstance(available_cases, int):
                raise BenchmarkRequestError("/api/evolve 返回了无效的进化状态")
            result = {
                "response_time_s": elapsed,
                "history_count": len(history),
                "available_cases": available_cases,
                "success": True,
            }
            self.results["进化状态接口"] = result
            print(f"  接口耗时: {elapsed:.3f}s")
            print(f"  历史记录数: {len(history)}")
            print(f"  可用评估案例: {available_cases}")
        except (requests.RequestException, BenchmarkRequestError) as error:
            print(f"{Colors.RED}✗ 进化状态接口测试失败: {error}{Colors.RESET}")
            self.results["进化状态接口"] = {"success": False, "error": str(error)}

    def test_database_performance(self) -> None:
        print(f"\n{Colors.BOLD}测试5: 数据库性能测试{Colors.RESET}")
        print(f"{Colors.BLUE}{'-' * 70}{Colors.RESET}")
        if not self.db_path.exists():
            message = f"数据库文件不存在: {self.db_path}"
            print(f"{Colors.RED}✗ {message}{Colors.RESET}")
            self.results["数据库"] = {"success": False, "error": message}
            return

        queries = (
            ("SELECT COUNT(*) FROM episodes", "Episodes计数"),
            ("SELECT COUNT(*) FROM patterns", "Patterns计数"),
            ("SELECT * FROM episodes LIMIT 10", "Episodes查询"),
            ("SELECT * FROM patterns LIMIT 10", "Patterns查询"),
        )
        try:
            with sqlite3.connect(str(self.db_path), timeout=30) as connection:
                for query, name in queries:
                    query_times = []
                    row_count = 0
                    for _ in range(100):
                        started = time.perf_counter()
                        rows = connection.execute(query).fetchall()
                        query_times.append((time.perf_counter() - started) * 1_000)
                        row_count = len(rows)
                    result = {
                        "avg_time_ms": statistics.mean(query_times),
                        "median_time_ms": statistics.median(query_times),
                        "success": True,
                    }
                    self.results[f"数据库_{name}"] = result
                    print(
                        f"  {name}: 平均 {result['avg_time_ms']:.2f}ms, "
                        f"结果 {row_count} 行"
                    )
        except sqlite3.Error as error:
            print(f"{Colors.RED}✗ 数据库性能测试失败: {error}{Colors.RESET}")
            self.results["数据库"] = {"success": False, "error": str(error)}

    def test_monitoring_system(self) -> None:
        print(f"\n{Colors.BOLD}测试6: Prometheus查询性能测试{Colors.RESET}")
        print(f"{Colors.BLUE}{'-' * 70}{Colors.RESET}")
        queries = (
            ('up{job="jarvis-core"}', "服务状态查询"),
            ("sum(rate(jarvis_http_requests_total[5m]))", "请求速率查询"),
            (
                "histogram_quantile(0.95, "
                "sum(rate(jarvis_http_request_duration_seconds_bucket[5m])) by (le))",
                "P95延迟查询",
            ),
        )
        for query, name in queries:
            query_times = []
            try:
                for _ in range(100):
                    started = time.perf_counter()
                    response = self.http.get(
                        f"{self.prometheus_url}/api/v1/query",
                        params={"query": query},
                        timeout=10,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("status") != "success":
                        raise BenchmarkRequestError(f"Prometheus 查询失败: {payload}")
                    query_times.append((time.perf_counter() - started) * 1_000)
                result = {
                    "avg_time_ms": statistics.mean(query_times),
                    "median_time_ms": statistics.median(query_times),
                    "success": True,
                }
                self.results[f"监控_{name}"] = result
                print(f"  {name}: 平均 {result['avg_time_ms']:.2f}ms")
            except (requests.RequestException, ValueError, BenchmarkRequestError) as error:
                print(f"{Colors.RED}✗ {name}失败: {error}{Colors.RESET}")
                self.results[f"监控_{name}"] = {"success": False, "error": str(error)}

    def _target_outcomes(self) -> tuple[tuple[str, bool], ...]:
        api_results = [self.results.get(name, {}) for name in self.API_ENDPOINT_NAMES]
        concurrency_results = [
            value for key, value in self.results.items() if key.startswith("并发")
        ]
        database_results = [
            value for key, value in self.results.items() if key.startswith("数据库_")
        ]
        learning = self.results.get("学习接口", {})
        evolution = self.results.get("进化状态接口", {})
        return (
            (
                "API平均响应时间",
                bool(api_results)
                and all(
                    result.get("avg_time_ms", float("inf"))
                    < self.targets["response_time_ms"]
                    and result.get("success_rate", 0)
                    >= self.targets["success_rate"]
                    for result in api_results
                ),
            ),
            (
                "峰值吞吐量",
                any(
                    result.get("throughput_rps", 0) > self.targets["throughput_rps"]
                    for result in concurrency_results
                ),
            ),
            (
                "并发成功率",
                bool(concurrency_results)
                and all(
                    result.get("success_rate", 0) >= self.targets["success_rate"]
                    for result in concurrency_results
                ),
            ),
            (
                "学习接口耗时",
                learning.get("success") is True
                and learning.get("cycle_time_s", float("inf"))
                < self.targets["learning_endpoint_time_s"],
            ),
            (
                "进化状态接口耗时",
                evolution.get("success") is True
                and evolution.get("response_time_s", float("inf"))
                < self.targets["evolution_endpoint_time_s"],
            ),
            (
                "数据库查询时间",
                bool(database_results)
                and all(
                    result.get("avg_time_ms", float("inf"))
                    < self.targets["db_query_time_ms"]
                    for result in database_results
                ),
            ),
        )

    def generate_benchmark_report(self) -> float:
        print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.BLUE}性能基准测试总结{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 70}{Colors.RESET}\n")

        outcomes = self._target_outcomes()
        targets_achieved = sum(passed for _, passed in outcomes)
        for name, passed in outcomes:
            marker = (
                f"{Colors.GREEN}✓{Colors.RESET}"
                if passed
                else f"{Colors.RED}✗{Colors.RESET}"
            )
            print(f"  {marker} {name}")
        achievement_rate = targets_achieved / len(outcomes) * 100
        print(f"\n  达成目标数: {targets_achieved}/{len(outcomes)}")
        print(f"  达成率: {achievement_rate:.1f}%")

        if achievement_rate >= 80:
            print(f"\n{Colors.GREEN}{Colors.BOLD}性能测试优秀{Colors.RESET}")
        elif achievement_rate >= 60:
            print(f"\n{Colors.YELLOW}{Colors.BOLD}性能测试合格，需要优化{Colors.RESET}")
        else:
            print(f"\n{Colors.RED}{Colors.BOLD}性能测试未达标，需要改进{Colors.RESET}")

        report_file = self.report_path
        report_file.parent.mkdir(parents=True, exist_ok=True)
        with report_file.open("w", encoding="utf-8") as stream:
            json.dump(
                {
                    "timestamp": datetime.now().isoformat(),
                    "targets": self.targets,
                    "results": self.results,
                    "summary": {
                        "targets_achieved": targets_achieved,
                        "total_targets": len(outcomes),
                        "achievement_rate": achievement_rate,
                    },
                },
                stream,
                ensure_ascii=False,
                indent=2,
            )
        print(f"\n{Colors.BLUE}详细报告已保存: {report_file}{Colors.RESET}")
        print(f"{Colors.BLUE}测试完成时间: {datetime.now().isoformat()}{Colors.RESET}\n")
        return achievement_rate


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark the live Jarvis API and local SQLite database."
    )
    parser.add_argument("--core-url", help="Jarvis base URL")
    parser.add_argument("--prometheus-url", help="Prometheus base URL")
    parser.add_argument("--db", type=Path, help="SQLite database path")
    parser.add_argument(
        "--output", type=Path,
        default=PROJECT_DIR / "performance_benchmark_report.json",
        help="JSON report path",
    )
    args = parser.parse_args(argv)
    benchmark = PerformanceBenchmark(
        core_url=args.core_url,
        prometheus_url=args.prometheus_url,
        db_path=args.db,
        report_path=args.output,
    )
    return 0 if benchmark.run_all_tests() >= 60 else 1


if __name__ == "__main__":
    raise SystemExit(main())
