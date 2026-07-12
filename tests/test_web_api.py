import base64
import json
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import jarvis.api.web_app as web_module
from jarvis.voice import (
    SpeechSynthesisError,
    SpeechSynthesisResult,
    UnavailableSpeechSynthesizer,
)


class FakeLLM:
    available = True
    model = "test-model"

    @staticmethod
    def chat_completion(messages, temperature=None):
        return "[]"

    @staticmethod
    def chat_with_memory(message, history=None, memory_context=None, db_path=None):
        return '<img src=x onerror="alert(1)"> safe text'

    @staticmethod
    def chat_with_prompt(message, system_prompt, history=None, context=None):
        return "skill response"

    @staticmethod
    def analyze_patterns(patterns):
        return "analysis"

    @staticmethod
    def record_eval_case(*args, **kwargs):
        return None

    @staticmethod
    def get_current_best_prompt(db_path=None):
        return "system"


class FakeSpeechSynthesizer:
    available = True
    provider = "test-speech"
    voice = "test-voice"
    reason = ""

    def __init__(self):
        self.calls = []

    def synthesize(self, text):
        self.calls.append(text)
        return SpeechSynthesisResult(
            audio=b"RIFFtest-wave",
            mime_type="audio/wav",
            visemes=[
                {"offset_ms": 0.0, "id": 0},
                {"offset_ms": 82.5, "id": 6},
            ],
            provider=self.provider,
            voice=self.voice,
        )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_module, "_post_chat_tasks", lambda *args: None)
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "web.db",
            "JARVIS_BACKUP_DIR": tmp_path / "backups",
        },
        llm_client=FakeLLM(),
        speech_synthesizer=UnavailableSpeechSynthesizer(
            "azure", "尚未配置 AZURE_SPEECH_KEY"
        ),
    )
    return app.test_client()


def create_session(client):
    response = client.post("/api/session")
    assert response.status_code == 201
    return response.get_json()["data"]["session_id"]


def test_fresh_app_health_status_and_security_headers(client):
    assert not hasattr(web_module, "app")
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "healthy"

    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json()["data"]["model"] == "test-model"
    assert status.get_json()["data"]["speech"]["available"] is False
    assert status.get_json()["data"]["speech"]["provider"] == "azure"
    assert status.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in status.headers["Content-Security-Policy"]
    assert status.headers["Cache-Control"] == "no-store"
    assert stat.S_IMODE(client.application.config["JARVIS_DB_PATH"].stat().st_mode) == 0o600


def test_auto_speech_prefers_azure_then_audio_reactive_edge(monkeypatch):
    class FakeAzure:
        provider = "azure"
        reason = "azure unavailable"

        def __init__(self, **_kwargs):
            self.available = FakeAzure.is_available

    class FakeEdge:
        provider = "edge"
        available = True
        reason = ""

        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(web_module, "AzureSpeechSynthesizer", FakeAzure)
    monkeypatch.setattr(web_module, "EdgeSpeechSynthesizer", FakeEdge)

    FakeAzure.is_available = True
    selected = web_module._create_speech_synthesizer({
        "voice": {"provider": "auto"}
    })
    assert selected.provider == "azure"

    FakeAzure.is_available = False
    selected = web_module._create_speech_synthesizer({
        "voice": {"provider": "auto"}
    })
    assert selected.provider == "edge"


def test_online_backup_uses_private_permissions(client):
    response = client.post("/api/backup")
    assert response.status_code == 201
    backup = (
        client.application.config["JARVIS_BACKUP_DIR"]
        / response.get_json()["data"]["filename"]
    )
    assert backup.is_file()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700


def test_chat_contract_uses_http_errors_and_safe_json(client):
    missing = client.post("/api/chat", json={"message": "hello"})
    assert missing.status_code == 401
    assert missing.get_json()["error"]["code"] == "invalid_session"

    session_id = create_session(client)
    response = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "hello",
    })
    assert response.status_code == 200
    assert response.get_json()["data"]["response"].startswith("<img")
    assert response.content_type == "application/json"


def test_chat_stream_flushes_deltas_and_holds_session_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(web_module, "_post_chat_tasks", lambda *args: None)

    class StreamingLLM(FakeLLM):
        @staticmethod
        def chat_with_memory_stream(*_args, **_kwargs):
            yield {"type": "delta", "text": "流式"}
            yield {"type": "delta", "text": "回答"}

    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "stream.db",
            "JARVIS_BACKUP_DIR": tmp_path / "backups",
        },
        llm_client=StreamingLLM(),
    )
    first_client = app.test_client()
    second_client = app.test_client()
    session_id = create_session(first_client)

    response = first_client.post("/api/chat/stream", json={
        "session_id": session_id,
        "message": "hello",
    }, buffered=False)
    chunks = iter(response.response)
    start = json.loads(next(chunks))

    assert response.content_type == "application/x-ndjson; charset=utf-8"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert start["type"] == "start"
    assert start["session_id"] == session_id

    overlap = second_client.post("/api/chat/stream", json={
        "session_id": session_id,
        "message": "second",
    })
    assert overlap.status_code == 409
    assert overlap.get_json()["error"]["code"] == "session_busy"

    events = [json.loads(chunk) for chunk in chunks]
    response.close()
    assert events == [
        {"type": "delta", "text": "流式"},
        {"type": "delta", "text": "回答"},
        {
            "type": "done",
            "response": "流式回答",
            "session_id": session_id,
            "interaction_id": start["interaction_id"],
        },
    ]
    with sqlite3.connect(app.config["JARVIS_DB_PATH"]) as connection:
        stored = connection.execute(
            "SELECT agent_response FROM interactions WHERE id = ?",
            (start["interaction_id"],),
        ).fetchone()[0]
    assert stored == "流式回答"


def test_filesystem_request_is_routed_to_tool_capable_model(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(workspace))
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "local-command.db",
        },
        llm_client=FakeLLM(),
    )
    client = app.test_client()
    session_id = create_session(client)

    response = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "帮我在桌面新建一个强强命名的文件夹",
    })

    assert response.status_code == 200
    assert response.get_json()["data"]["response"].endswith("safe text")
    assert not workspace.exists()


def test_filesystem_request_requires_model_credentials(tmp_path, monkeypatch):
    class OfflineLLM(FakeLLM):
        available = False

    workspace = tmp_path / "workspace"
    monkeypatch.setenv("JARVIS_WORKSPACE_PATH", str(workspace))
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "offline-command.db",
        },
        llm_client=OfflineLLM(),
    )
    client = app.test_client()
    session_id = create_session(client)

    response = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "在桌面新建一个离线测试文件夹",
    })

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "llm_unavailable"
    assert not workspace.exists()


def test_model_exception_removes_pending_interaction(tmp_path):
    class RaisingLLM(FakeLLM):
        @staticmethod
        def chat_with_memory(*args, **kwargs):
            raise RuntimeError("provider failed")

    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "raising.db",
        },
        llm_client=RaisingLLM(),
    )
    raising_client = app.test_client()
    session_id = create_session(raising_client)
    response = raising_client.post("/api/chat", json={
        "session_id": session_id,
        "message": "hello",
    })

    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "model_request_failed"
    with sqlite3.connect(app.config["JARVIS_DB_PATH"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM interactions"
        ).fetchone()[0] == 0


def test_provider_auth_failure_has_actionable_error(tmp_path):
    class UnauthorizedLLM(FakeLLM):
        @staticmethod
        def chat_with_memory(*args, **kwargs):
            return "[JARVIS_LLM_ERROR] http_401"

        @staticmethod
        def response_is_error(value):
            return value.startswith("[JARVIS_LLM_ERROR]")

    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "auth-failure.db",
        },
        llm_client=UnauthorizedLLM(),
    )
    client = app.test_client()
    session_id = create_session(client)
    response = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "hello",
    })

    assert response.status_code == 502
    payload = response.get_json()["error"]
    assert payload["code"] == "model_auth_failed"
    assert "ANTHROPIC_API_KEY" in payload["message"]


def test_same_session_rejects_overlapping_model_requests(tmp_path):
    class BlockingLLM(FakeLLM):
        entered = threading.Event()
        release = threading.Event()

        @staticmethod
        def chat_with_memory(*args, **kwargs):
            BlockingLLM.entered.set()
            assert BlockingLLM.release.wait(timeout=3)
            return "serialized response"

    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "concurrent-chat.db",
        },
        llm_client=BlockingLLM(),
    )
    session_id = create_session(app.test_client())

    def first_request():
        return app.test_client().post("/api/chat", json={
            "session_id": session_id,
            "message": "first",
        })

    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(first_request)
        assert BlockingLLM.entered.wait(timeout=2)
        try:
            overlap = app.test_client().post("/api/chat", json={
                "session_id": session_id,
                "message": "second",
            })
        finally:
            BlockingLLM.release.set()
        completed = future.result(timeout=3)

    assert overlap.status_code == 409
    assert overlap.get_json()["error"]["code"] == "session_busy"
    assert completed.status_code == 200
    with sqlite3.connect(app.config["JARVIS_DB_PATH"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM interactions"
        ).fetchone()[0] == 1
    gate = app.extensions["session_request_gate"]
    assert gate._locks == {}
    assert gate._references == {}


def test_feedback_labels_only_owned_helpful_responses(client):
    owner = create_session(client)
    other = create_session(client)
    chat = client.post("/api/chat", json={
        "session_id": owner,
        "message": "hello",
    }).get_json()["data"]

    rejected = client.post("/api/feedback", json={
        "session_id": other,
        "interaction_id": chat["interaction_id"],
        "helpful": True,
    })
    accepted = client.post("/api/feedback", json={
        "session_id": owner,
        "interaction_id": chat["interaction_id"],
        "helpful": True,
    })
    assert rejected.status_code == 404
    assert accepted.status_code == 200
    assert accepted.get_json()["data"]["eligible_for_evolution"] is True

    with sqlite3.connect(client.application.config["JARVIS_DB_PATH"]) as connection:
        row = connection.execute("""
            SELECT expected, feedback_score, source FROM eval_cases
            WHERE interaction_id = ?
        """, (chat["interaction_id"],)).fetchone()
    assert row == (chat["response"], 1.0, "user_approved")

    history = client.get("/api/chat/history", query_string={
        "session_id": owner,
    }).get_json()["data"]["history"]
    assert history[-1]["helpful"] is True


def test_negative_feedback_never_creates_reference_answer(client):
    session_id = create_session(client)
    chat = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "hello",
    }).get_json()["data"]
    response = client.post("/api/feedback", json={
        "session_id": session_id,
        "interaction_id": chat["interaction_id"],
        "helpful": False,
    })
    assert response.status_code == 200
    with sqlite3.connect(client.application.config["JARVIS_DB_PATH"]) as connection:
        row = connection.execute("""
            SELECT expected, feedback_score, source FROM eval_cases
            WHERE interaction_id = ?
        """, (chat["interaction_id"],)).fetchone()
    assert row == (None, 0.0, "user_rejected")


def test_repeated_feedback_does_not_requeue_consumed_evolution_case(client):
    session_id = create_session(client)
    chat = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "hello",
    }).get_json()["data"]
    first = client.post("/api/feedback", json={
        "session_id": session_id,
        "interaction_id": chat["interaction_id"],
        "helpful": True,
    })
    assert first.get_json()["data"]["changed"] is True

    db_path = client.application.config["JARVIS_DB_PATH"]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE eval_cases SET used_in_evolution = 1 WHERE interaction_id = ?",
            (chat["interaction_id"],),
        )

    repeated = client.post("/api/feedback", json={
        "session_id": session_id,
        "interaction_id": chat["interaction_id"],
        "helpful": True,
    })
    assert repeated.get_json()["data"]["changed"] is False
    with sqlite3.connect(db_path) as connection:
        used, feedback_count = connection.execute("""
            SELECT e.used_in_evolution, COUNT(f.id)
            FROM eval_cases AS e
            LEFT JOIN user_feedback AS f ON f.interaction_id = e.interaction_id
            WHERE e.interaction_id = ?
            GROUP BY e.id
        """, (chat["interaction_id"],)).fetchone()
    assert (used, feedback_count) == (1, 1)

    changed = client.post("/api/feedback", json={
        "session_id": session_id,
        "interaction_id": chat["interaction_id"],
        "helpful": False,
    })
    assert changed.get_json()["data"]["changed"] is True
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("""
            SELECT used_in_evolution, expected, feedback_score
            FROM eval_cases WHERE interaction_id = ?
        """, (chat["interaction_id"],)).fetchone()
    assert row == (0, None, 0.0)


def test_feedback_race_with_background_eval_record_is_atomic(client):
    session_id = create_session(client)
    chat = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "hello",
    }).get_json()["data"]
    barrier = threading.Barrier(2)
    llm = web_module.LLMConfig()
    db_path = str(client.application.config["JARVIS_DB_PATH"])

    def record_unlabeled():
        barrier.wait()
        return llm.record_eval_case(
            "hello", chat["response"], interaction_id=chat["interaction_id"],
            db_path=db_path,
        )

    def record_feedback():
        barrier.wait()
        return client.application.test_client().post("/api/feedback", json={
            "session_id": session_id,
            "interaction_id": chat["interaction_id"],
            "helpful": True,
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        eval_future = executor.submit(record_unlabeled)
        feedback_future = executor.submit(record_feedback)
        assert eval_future.result() > 0
        assert feedback_future.result() == 200

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("""
            SELECT expected, feedback_score, source FROM eval_cases
            WHERE interaction_id = ?
        """, (chat["interaction_id"],)).fetchall()
    assert rows == [(chat["response"], 1.0, "user_approved")]


def test_history_returns_latest_rows_and_clamps_limit(client):
    session_id = create_session(client)
    app = client.application
    db_path = app.config["JARVIS_DB_PATH"]
    with sqlite3.connect(db_path) as connection:
        for index in range(5):
            connection.execute("""
                INSERT INTO interactions
                    (session_id, timestamp, interaction_type, user_input, agent_response)
                VALUES (?, ?, 'test', ?, ?)
            """, (session_id, index, f"u{index}", f"a{index}"))

    response = client.get("/api/chat/history", query_string={
        "session_id": session_id,
        "limit": 2,
    })
    history = response.get_json()["data"]["history"]
    assert [item["content"] for item in history] == ["u3", "a3", "u4", "a4"]


def test_memory_is_scoped_to_session(client):
    first = create_session(client)
    second = create_session(client)
    stored = client.post("/api/memory/store", json={
        "session_id": first, "key": "secret", "value": "only-first",
    })
    assert stored.status_code == 201

    own = client.get("/api/memory/retrieve", query_string={
        "session_id": first, "key": "secret",
    }).get_json()["data"]
    other = client.get("/api/memory/retrieve", query_string={
        "session_id": second, "key": "secret",
    }).get_json()["data"]
    assert own["value"] == "only-first"
    assert other["found"] is False


def test_memory_persists_across_sessions_for_same_browser_user(client):
    first_data = client.post("/api/session").get_json()["data"]
    second_data = client.post("/api/session", json={
        "user_id": first_data["user_id"]
    }).get_json()["data"]
    assert first_data["session_id"] != second_data["session_id"]
    assert first_data["user_id"] == second_data["user_id"]

    client.post("/api/memory/store", json={
        "session_id": first_data["session_id"],
        "key": "preference",
        "value": "persistent",
    })
    retrieved = client.get("/api/memory/retrieve", query_string={
        "session_id": second_data["session_id"],
        "key": "preference",
    }).get_json()["data"]
    assert retrieved["value"] == "persistent"


def test_optional_api_token_is_enforced(tmp_path):
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "token.db",
            "JARVIS_API_TOKEN": "correct-token",
        },
        llm_client=FakeLLM(),
    )
    token_client = app.test_client()
    assert token_client.get("/api/status").status_code == 401
    assert token_client.get(
        "/api/status", headers={"X-Jarvis-Token": "correct-token"}
    ).status_code == 200


def test_configured_public_origin_allows_tls_proxy_writes(tmp_path):
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "origin.db",
            "JARVIS_PUBLIC_ORIGIN": "https://jarvis.example",
            "JARVIS_API_TOKEN": "test-origin-token",
        },
        llm_client=FakeLLM(),
    )
    origin_client = app.test_client()
    accepted = origin_client.post(
        "/api/session", base_url="https://jarvis.example", headers={
            "Origin": "https://jarvis.example",
            "X-Jarvis-Token": "test-origin-token",
        }
    )
    rejected = origin_client.post(
        "/api/session", base_url="https://jarvis.example", headers={
            "Origin": "https://attacker.example",
            "X-Jarvis-Token": "test-origin-token",
        }
    )
    assert accepted.status_code == 201
    assert rejected.status_code == 403


def test_tokenless_api_rejects_dns_rebinding_host(tmp_path):
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "rebind.db",
            "JARVIS_API_TOKEN": "",
        },
        llm_client=FakeLLM(),
    )
    response = app.test_client().post(
        "/api/session",
        base_url="http://attacker.example",
        headers={"Origin": "http://attacker.example"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_host"


def test_valid_token_allows_internal_compose_host_with_public_origin(tmp_path):
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "internal-host.db",
            "JARVIS_BACKUP_DIR": tmp_path / "backups",
            "JARVIS_API_TOKEN": "test-internal-token",
            "JARVIS_PUBLIC_ORIGIN": "https://jarvis.example",
        },
        llm_client=FakeLLM(),
    )
    response = app.test_client().post(
        "/api/backup",
        base_url="http://jarvis-core:8000",
        headers={"X-Jarvis-Token": "test-internal-token"},
    )
    assert response.status_code == 201


def test_rate_limiter_removes_inactive_identity_keys(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(web_module.time, "monotonic", lambda: clock[0])
    limiter = web_module.RateLimiter(sweep_every=1)

    assert limiter.allow("chat", "old-client", 1, 60)
    clock[0] = 61.0
    assert limiter.allow("chat", "new-client", 1, 60)

    assert ("chat", "old-client") not in limiter._events


def test_trusted_proxy_hops_control_forwarded_client_identity(tmp_path):
    app = web_module.create_app(
        {
            "TESTING": True,
            "JARVIS_DB_PATH": tmp_path / "proxy.db",
            "JARVIS_TRUSTED_PROXY_HOPS": 1,
        },
        llm_client=FakeLLM(),
    )
    app.add_url_rule(
        "/identity", "identity", lambda: web_module._request_identity()
    )
    response = app.test_client().get(
        "/identity", headers={"X-Forwarded-For": "203.0.113.8"}
    )
    assert response.get_data(as_text=True) == "203.0.113.8"


def test_toggle_missing_skill_returns_not_found(client):
    response = client.post("/api/skills/toggle", json={
        "id": 999_999,
        "enabled": True,
        "reviewed": True,
    })
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "skill_not_found"


def test_skill_requires_explicit_review_before_enable(client):
    store = client.application.extensions["skill_store"]
    assert store.add(
        "review_me", "description", ["keyword"], None, "trusted prompt",
        enabled=False,
    )
    skill = store.get_all(enabled_only=False)[0]
    listed = client.get("/api/skills").get_json()["data"]["skills"][0]
    assert listed["prompt_template"] == "trusted prompt"
    assert listed["reviewed"] == 0

    rejected = client.post("/api/skills/toggle", json={
        "id": skill["id"], "enabled": True,
    })
    accepted = client.post("/api/skills/toggle", json={
        "id": skill["id"], "enabled": True, "reviewed": True,
    })
    assert rejected.status_code == 400
    assert rejected.get_json()["error"]["code"] == "review_required"
    assert accepted.status_code == 200


def test_skill_metric_failure_does_not_drop_generated_response(client, monkeypatch):
    store = client.application.extensions["skill_store"]
    assert store.add(
        "metric_failure", "description", ["special"], None,
        "trusted prompt", enabled=True,
    )
    client.application.extensions["skill_matcher"].invalidate_cache()

    def fail_metric(_skill_id):
        raise sqlite3.OperationalError("metric database unavailable")

    monkeypatch.setattr(store, "record_trigger", fail_metric)
    session_id = create_session(client)
    response = client.post("/api/chat", json={
        "session_id": session_id,
        "message": "special request",
    })

    assert response.status_code == 200
    assert response.get_json()["data"]["response"] == "skill response"
    with sqlite3.connect(client.application.config["JARVIS_DB_PATH"]) as connection:
        stored = connection.execute(
            "SELECT agent_response FROM interactions"
        ).fetchone()[0]
    assert stored == "skill response"


def test_index_uses_generated_segmented_mask_not_external_scan(client):
    html = client.get("/").get_data(as_text=True)
    face_script = client.get("/static/js/jarvis-face.js").get_data(as_text=True)

    assert 'id="face-canvas"' in html
    assert "jarvis-face.js" in html
    assert "GLTFLoader.js" not in html
    assert "LeePerrySmith.glb" not in html
    assert "audioWaveData" not in html
    assert "function makePlate(" in face_script
    assert "new THREE.ExtrudeGeometry" in face_script
    assert "new THREE.EdgesGeometry" in face_script
    assert 'faceDesign = "segmented-mask"' in face_script
    assert 'mouthMechanism = "articulated-plates"' in face_script
    assert "function addArticulatedPair(" in face_script


def test_face_animation_consumes_service_visemes_on_audio_clock(client):
    face_script = client.get("/static/js/jarvis-face.js").get_data(as_text=True)
    app_script = client.get("/static/js/app.js").get_data(as_text=True)

    assert "VISEME_SHAPES" in face_script
    assert "setViseme" in face_script
    assert "depthWrite: true" in face_script
    assert "dataset.mouthOpen" in face_script
    assert "upperMouthRigs.forEach" in face_script
    assert "lowerMouthRigs.forEach" in face_script
    assert "lowerJawRig.rotation.x" in face_script
    assert "mouthCavity" not in face_script
    assert "makeMouthRail" not in face_script
    assert "visemeForCharacter" not in face_script
    assert "setSpeechCharacter" not in face_script

    assert 'api.request("/api/speech"' in app_script
    assert "context.currentTime - startedAt" in app_script
    assert "face.setViseme" in app_script
    assert "face.startSpeaking()" in app_script
    assert "face.stopSpeaking()" in app_script
    assert "SpeechSynthesisUtterance" not in app_script
    assert "utterance.onboundary" not in app_script


def test_speech_endpoint_returns_audio_and_matching_visemes(client):
    synthesizer = FakeSpeechSynthesizer()
    client.application.extensions["speech_synthesizer"] = synthesizer
    session_id = create_session(client)

    response = client.post("/api/speech", json={
        "session_id": session_id,
        "text": "你好",
    })

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert base64.b64decode(data["audio_base64"]) == b"RIFFtest-wave"
    assert data["mime_type"] == "audio/wav"
    assert data["visemes"] == [
        {"offset_ms": 0.0, "id": 0},
        {"offset_ms": 82.5, "id": 6},
    ]
    assert data["provider"] == "test-speech"
    assert data["viseme_source"] == "provider"
    assert synthesizer.calls == ["你好"]


def test_speech_endpoint_validates_session_text_and_provider(client):
    missing_session = client.post("/api/speech", json={"text": "你好"})
    assert missing_session.status_code == 401
    assert missing_session.get_json()["error"]["code"] == "invalid_session"

    session_id = create_session(client)
    empty = client.post("/api/speech", json={
        "session_id": session_id,
        "text": "  ",
    })
    assert empty.status_code == 400
    assert empty.get_json()["error"]["code"] == "invalid_speech_text"

    too_long = client.post("/api/speech", json={
        "session_id": session_id,
        "text": "字" * 501,
    })
    assert too_long.status_code == 400
    assert too_long.get_json()["error"]["code"] == "speech_text_too_long"

    unavailable = client.post("/api/speech", json={
        "session_id": session_id,
        "text": "你好",
    })
    assert unavailable.status_code == 503
    assert unavailable.get_json()["error"]["code"] == "speech_unavailable"


def test_speech_provider_errors_are_classified(client):
    class FailingSpeech(FakeSpeechSynthesizer):
        def synthesize(self, _text):
            raise SpeechSynthesisError(
                "speech_provider_failed", "语音服务未能生成音频"
            )

    client.application.extensions["speech_synthesizer"] = FailingSpeech()
    session_id = create_session(client)
    response = client.post("/api/speech", json={
        "session_id": session_id,
        "text": "你好",
    })

    assert response.status_code == 502
    assert response.get_json()["error"] == {
        "code": "speech_provider_failed",
        "message": "语音服务未能生成音频",
    }


def test_frontend_streams_text_and_prefetches_speech_chunks(client):
    script = client.get("/static/js/app.js").get_data(as_text=True)
    api_script = client.get("/static/js/api-client.js").get_data(as_text=True)

    assert 'api.stream("/api/chat/stream"' in script
    assert "requestChatResponse" in script
    assert 'api.request("/api/chat"' in script
    assert 'error.status !== 404 || error.code !== "not_found"' in script
    assert "createSpeechStream" in script
    assert "speechChunkBoundary" in script
    assert "playSpeechSegment" in script
    assert "options.onProgress" in script
    assert 'payload.viseme_source === "audio-analysis"' in script
    assert "analyser.getByteFrequencyData" in script
    assert "splitSpeechText" not in script
    assert "typeResponse" not in script
    assert 'headers.set("Accept", "application/x-ndjson")' in api_script
    assert "response.body.getReader()" in api_script
    assert "text.slice(0, 3000)" not in script
    learning_request = script[script.index('api.request("/api/learn"'):]
    assert "timeout: 110000" in learning_request[:180]


def test_learning_endpoint_mines_real_interaction_intents(client):
    first = create_session(client)
    second = create_session(client)
    with sqlite3.connect(client.application.config["JARVIS_DB_PATH"]) as connection:
        for session_id, messages in (
            (first, ("你叫什么？", "请帮我总结", "打开终端")),
            (second, ("今天几号？", "麻烦整理一下", "运行测试")),
        ):
            for index, message in enumerate(messages):
                connection.execute("""
                    INSERT INTO interactions
                        (session_id, timestamp, interaction_type,
                         user_input, agent_response)
                    VALUES (?, ?, 'user_message', ?, 'done')
                """, (session_id, 2_000_000_000 + index, message))

    response = client.post("/api/learn")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["sample_sessions"] == 2
    assert data["sample_interactions"] == 6
    assert any(item["sequence"] == "提问 -> 请求 -> 命令" for item in data["patterns"])
    assert all("打开应用" not in item["sequence"] for item in data["patterns"])


def test_evolution_status_only_counts_labeled_cases(client):
    with sqlite3.connect(client.application.config["JARVIS_DB_PATH"]) as connection:
        connection.execute("""
            INSERT INTO eval_cases
                (user_input, agent_response, expected, created_at)
            VALUES ('unlabeled', 'old answer', NULL, 1)
        """)
        connection.execute("""
            INSERT INTO eval_cases
                (user_input, agent_response, expected, created_at)
            VALUES ('labeled', 'old answer', 'reviewed answer', 2)
        """)

    data = client.get("/api/evolve").get_json()["data"]
    assert data["available_cases"] == 1


def test_failed_evolution_uses_retry_backoff(client, monkeypatch):
    from jarvis.learning import evolution as evolution_module

    db_path = client.application.config["JARVIS_DB_PATH"]
    with sqlite3.connect(db_path) as connection:
        for index in range(6):
            connection.execute("""
                INSERT INTO eval_cases
                    (user_input, agent_response, expected, created_at)
                VALUES (?, 'answer', 'approved answer', ?)
            """, (f"case-{index}", index))

    attempts = []

    class FailingEvolver:
        def __init__(self, *args, **kwargs):
            pass

        def evolve(self, *args, **kwargs):
            attempts.append("attempt")
            raise RuntimeError("provider unavailable")

    class ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(evolution_module, "DarwinianEvolver", FailingEvolver)
    monkeypatch.setattr(web_module.threading, "Thread", ImmediateThread)
    key = str(db_path)
    web_module._evolution_last_run.pop(key, None)
    web_module._evolution_retry_after.pop(key, None)
    try:
        web_module._maybe_trigger_evolution(
            client.application, db_path, FakeLLM(),
            failure_backoff_seconds=3600,
        )
        web_module._maybe_trigger_evolution(
            client.application, db_path, FakeLLM(),
            failure_backoff_seconds=3600,
        )
        assert attempts == ["attempt"]
        assert web_module._evolution_retry_after[key] > web_module.time.time()
    finally:
        web_module._evolution_last_run.pop(key, None)
        web_module._evolution_retry_after.pop(key, None)


def test_evolved_prompt_requires_explicit_approval(client):
    with sqlite3.connect(client.application.config["JARVIS_DB_PATH"]) as connection:
        cursor = connection.execute("""
            INSERT INTO evolution_history
                (evolution_type, generation, fitness_score, mutation_details,
                 timestamp)
            VALUES ('prompt', 2, 0.9, '{"content":"candidate"}', 1)
        """)
        evolution_id = cursor.lastrowid

    listed = client.get("/api/evolve").get_json()["data"]["history"][0]
    assert listed["content"] == "candidate"
    assert listed["approved"] is False
    rejected = client.post("/api/evolve/approve", json={
        "id": evolution_id, "approved": True,
    })
    accepted = client.post("/api/evolve/approve", json={
        "id": evolution_id, "approved": True, "reviewed": True,
    })
    assert rejected.status_code == 400
    assert rejected.get_json()["error"]["code"] == "review_required"
    assert accepted.status_code == 200
    with sqlite3.connect(client.application.config["JARVIS_DB_PATH"]) as connection:
        approved = connection.execute(
            "SELECT approved FROM evolution_history WHERE id = ?",
            (evolution_id,),
        ).fetchone()[0]
    assert approved == 1
