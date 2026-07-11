from collections import deque

import pytest

from scripts.performance_benchmark import BenchmarkRequestError, PerformanceBenchmark


class StubResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class StubSession:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.popleft()


def test_api_client_applies_optional_token_and_unwraps_data(tmp_path):
    session = StubSession(StubResponse({"ok": True, "data": {"status": "running"}}))
    benchmark = PerformanceBenchmark(
        core_url="http://jarvis.test/",
        db_path=tmp_path / "test.db",
        api_token="secret-token",
        session=session,
    )

    data = benchmark._request_api_data("GET", "/api/status", timeout=5)

    assert data == {"status": "running"}
    assert session.calls == [
        (
            "GET",
            "http://jarvis.test/api/status",
            {"headers": {"X-Jarvis-Token": "secret-token"}, "timeout": 5},
        )
    ]


def test_api_client_surfaces_envelope_errors(tmp_path):
    session = StubSession(
        StubResponse(
            {
                "ok": False,
                "error": {"code": "unauthorized", "message": "需要有效的访问令牌"},
            },
            status_code=401,
        )
    )
    benchmark = PerformanceBenchmark(
        db_path=tmp_path / "test.db", api_token="", session=session
    )

    with pytest.raises(BenchmarkRequestError, match="unauthorized"):
        benchmark._request_api_data("GET", "/api/status", timeout=5)

    assert session.calls[0][2]["headers"] == {}


def test_learning_and_evolution_use_current_get_endpoints(tmp_path):
    session = StubSession(
        StubResponse(
            {
                "ok": True,
                "data": {
                    "patterns": [
                        {"sequence": "A -> B", "support": 0.8, "confidence": 0.5},
                        {"sequence": "A -> C", "support": 0.6, "confidence": 1.0},
                    ],
                    "ai_analysis": "analysis",
                },
            }
        ),
        StubResponse(
            {
                "ok": True,
                "data": {"history": [{"generation": 1}], "available_cases": 3},
            }
        ),
    )
    benchmark = PerformanceBenchmark(
        core_url="http://jarvis.test",
        db_path=tmp_path / "test.db",
        session=session,
    )

    benchmark.test_learning_system()
    benchmark.test_evolution_engine()

    assert [call[:2] for call in session.calls] == [
        ("POST", "http://jarvis.test/api/learn"),
        ("GET", "http://jarvis.test/api/evolve"),
    ]
    assert benchmark.results["学习接口"]["patterns_found"] == 2
    assert benchmark.results["学习接口"]["average_confidence"] == pytest.approx(0.75)
    assert benchmark.results["进化状态接口"]["history_count"] == 1
    assert benchmark.results["进化状态接口"]["available_cases"] == 3
