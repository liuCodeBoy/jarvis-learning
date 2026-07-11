#!/usr/bin/env python3
"""Flask application for the J.A.R.V.I.S. browser interface."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit

import yaml
from flask import Flask, Response, current_app, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from jarvis.core.llm import LLMConfig, LLM_ERROR_PREFIX, get_llm
from jarvis.database.schema import LearningDatabaseSchema
from jarvis.learning.skills import SkillMatcher, SkillStore
from jarvis.memory.bridge import MemoryBridge, get_memory_bridge
from jarvis.tools.local_commands import LocalCommandExecutor


logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "jarvis_learning.db"
SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
MAX_MESSAGE_LENGTH = 8_000
MAX_MEMORY_VALUE_LENGTH = 20_000


try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest,
    )
    HTTP_REQUESTS = Counter(
        "jarvis_http_requests_total", "HTTP requests", ("method", "route", "status")
    )
    HTTP_LATENCY = Histogram(
        "jarvis_http_request_duration_seconds", "HTTP request latency",
        ("method", "route"),
    )
    LLM_CONFIGURED = Gauge(
        "jarvis_llm_configured", "Whether model credentials are configured"
    )
except ImportError:  # pragma: no cover - optional in minimal development setups
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
    REGISTRY = None
    generate_latest = None
    HTTP_REQUESTS = None
    HTTP_LATENCY = None
    LLM_CONFIGURED = None


class RateLimiter:
    """Small in-process sliding-window limiter for a single-node deployment."""

    def __init__(self, sweep_every: int = 256) -> None:
        self._events: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
        self._windows: Dict[Tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._request_count = 0
        self._sweep_every = max(1, int(sweep_every))

    def allow(self, bucket: str, identity: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        cutoff = now - window
        key = (bucket, identity)
        with self._lock:
            self._request_count += 1
            self._windows[key] = window
            if self._request_count % self._sweep_every == 0:
                self._remove_expired_keys(now)
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True

    def _remove_expired_keys(self, now: float) -> None:
        for key, events in list(self._events.items()):
            cutoff = now - self._windows.get(key, 0)
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._events.pop(key, None)
                self._windows.pop(key, None)


class SessionRequestGate:
    """Allow only one in-flight model request for each local session."""

    def __init__(self) -> None:
        self._locks: Dict[str, threading.Lock] = {}
        self._references: Dict[str, int] = {}
        self._guard = threading.Lock()

    @contextmanager
    def hold(self, session_id: str) -> Iterator[bool]:
        with self._guard:
            lock = self._locks.setdefault(session_id, threading.Lock())
            self._references[session_id] = self._references.get(session_id, 0) + 1

        acquired = lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
            with self._guard:
                remaining = self._references[session_id] - 1
                if remaining:
                    self._references[session_id] = remaining
                else:
                    self._references.pop(session_id, None)
                    self._locks.pop(session_id, None)


def _resolve_db_path(value: Optional[str]) -> Path:
    if not value:
        return DEFAULT_DB_PATH
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_DIR / path).resolve()


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        logger.warning("Unable to restrict directory permissions for %s", path)


def _private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("Unable to restrict file permissions for %s", path)


def _bool_setting(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        logger.warning("Ignoring invalid %s value", name)
        value = default
    return max(minimum, min(maximum, value))


def _load_project_config() -> Dict[str, Any]:
    configured = os.environ.get("JARVIS_CONFIG_PATH")
    path = Path(configured).expanduser() if configured else PROJECT_DIR / "config.yaml"
    if not path.is_absolute():
        path = (PROJECT_DIR / path).resolve()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, yaml.YAMLError):
        logger.exception("Unable to load project configuration from %s", path)
        return {}


def _db_connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _initialize_database(path: Path) -> SkillStore:
    """Create every table required by a fresh web deployment."""
    _private_directory(path.parent)
    LearningDatabaseSchema(str(path)).initialize_schema()
    skill_store = SkillStore(str(path))

    with _db_connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    _private_file(path)
    return skill_store


def _ok(data: Optional[Dict[str, Any]] = None, status: int = 200):
    return jsonify({"ok": True, "data": data or {}}), status


def _error(code: str, message: str, status: int):
    return jsonify({
        "ok": False,
        "error": {"code": code, "message": message},
    }), status


def _request_identity() -> str:
    # Do not trust a client-supplied X-Forwarded-For header unless the app is
    # explicitly wrapped with a trusted-proxy configuration.
    return request.remote_addr or "unknown"


def _is_loopback_host(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _api_host_allowed() -> bool:
    """Block DNS-rebinding access to a tokenless local API."""
    parsed_host = urlsplit(f"//{request.host}")
    if parsed_host.username is not None or parsed_host.password is not None:
        return False
    if _is_loopback_host(parsed_host.hostname):
        return True

    expected_token = current_app.config.get("JARVIS_API_TOKEN", "")
    # protect_api validates the supplied token before reaching this helper.
    # A valid bearer credential must also work over an internal Compose host.
    return bool(expected_token)


def rate_limited(bucket: str, limit: int, window: int):
    def decorator(function: Callable):
        @wraps(function)
        def wrapped(*args, **kwargs):
            limiter: RateLimiter = current_app.extensions["rate_limiter"]
            if not limiter.allow(bucket, _request_identity(), limit, window):
                response, status = _error(
                    "rate_limited", "请求过于频繁，请稍后再试", 429
                )
                response.headers["Retry-After"] = str(window)
                return response, status
            return function(*args, **kwargs)
        return wrapped
    return decorator


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _validate_session_id(value: Any) -> Optional[str]:
    session_id = str(value or "").strip().lower()
    return session_id if SESSION_ID_RE.fullmatch(session_id) else None


def _session_exists(path: Path, session_id: str) -> bool:
    return _session_user(path, session_id) is not None


def _session_user(path: Path, session_id: str) -> Optional[str]:
    with _db_connect(path) as connection:
        row = connection.execute(
            "SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    if row is None:
        return None
    return row["user_id"] or session_id


def _llm_available(client: Any) -> bool:
    available = getattr(client, "available", None)
    return bool(available if available is not None else getattr(client, "api_key", True))


def _model_error(response_text: Any) -> Tuple[str, str]:
    """Turn provider error markers into actionable, non-sensitive UI messages."""
    marker = str(response_text or "")
    if marker.startswith(LLM_ERROR_PREFIX):
        marker = marker[len(LLM_ERROR_PREFIX):].strip()
    if marker in {"http_401", "http_403"}:
        return (
            "model_auth_failed",
            "模型凭据无效或无权限，请检查 ANTHROPIC_API_KEY、代理地址和模型服务配置",
        )
    if marker in {"http_404", "retry_http_404"}:
        return (
            "model_endpoint_not_found",
            "模型服务地址或模型名称不存在，请检查 ANTHROPIC_BASE_URL 和 ANTHROPIC_MODEL",
        )
    if marker in {"http_429", "retry_http_429"}:
        return ("model_rate_limited", "模型服务当前限流，请稍后重试")
    if marker.startswith("network_") or marker.startswith("retries_exhausted"):
        return ("model_network_failed", "无法连接模型服务，请检查网络和代理地址")
    if marker == "total_timeout":
        return ("model_timeout", "模型服务响应超时，请稍后重试")
    return ("model_request_failed", "模型服务请求失败，请稍后重试")


def _classify_message(message: str) -> str:
    """Map a user message to a stable, low-cardinality interaction intent."""
    text = message.strip().lower()
    if any(marker in text for marker in (
        "打开", "关闭", "运行", "执行", "创建", "删除", "保存", "发送",
        "搜索", "查找", "提醒", "设置",
    )):
        return "命令"
    if any(marker in text for marker in ("请", "帮我", "麻烦", "能不能", "可以帮")):
        return "请求"
    if any(marker in text for marker in (
        "?", "？", "吗", "什么", "怎么", "为何", "为什么", "谁", "哪里",
        "哪", "几", "是否", "能否",
    )):
        return "提问"
    return "陈述"


def _process_chat(path: Path, session_id: str, user_namespace: str,
                  message: str, client: Any):
    """Run one serialized model turn and persist its completed response."""
    with _db_connect(path) as connection:
        rows = connection.execute("""
            SELECT user_input, agent_response FROM (
                SELECT id, user_input, agent_response FROM interactions
                WHERE session_id = ? AND user_input != '' AND agent_response != ''
                ORDER BY id DESC LIMIT 10
            ) ORDER BY id ASC
        """, (session_id,)).fetchall()
        history = [
            item
            for row in rows
            for item in (
                {"role": "user", "content": row["user_input"]},
                {"role": "assistant", "content": row["agent_response"]},
            )
        ]
        cursor = connection.execute("""
            INSERT INTO interactions
                (session_id, timestamp, interaction_type, user_input, agent_response)
            VALUES (?, ?, 'user_message', ?, '')
        """, (session_id, time.time(), message))
        interaction_id = cursor.lastrowid

    bridge: MemoryBridge = current_app.extensions["memory_bridge"]
    local_result = current_app.extensions["local_command_executor"].execute(message)
    if local_result is not None:
        with _db_connect(path) as connection:
            connection.execute(
                "UPDATE interactions SET agent_response = ? WHERE id = ?",
                (local_result.message, interaction_id),
            )
        return _ok({
            "response": local_result.message,
            "session_id": session_id,
            "interaction_id": interaction_id,
            "execution": {
                "operation": local_result.operation,
                "executed": local_result.executed,
            },
        })

    if not _llm_available(client):
        with _db_connect(path) as connection:
            connection.execute(
                "DELETE FROM interactions WHERE id = ?", (interaction_id,)
            )
        return _error(
            "llm_unavailable",
            "尚未配置模型凭据，请设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN",
            503,
        )

    memory_context = None
    try:
        relevant = bridge.retrieve_relevant(
            message, max_items=5, namespace=user_namespace
        )
        memory_context = bridge.format_for_prompt(relevant) if relevant else None
    except Exception:
        logger.exception("Memory retrieval failed")

    matcher: SkillMatcher = current_app.extensions["skill_matcher"]
    try:
        matched_skill = matcher.match(message)
    except Exception:
        logger.exception("Skill matching failed")
        matched_skill = None

    try:
        if matched_skill:
            response_text = client.chat_with_prompt(
                message,
                matched_skill["prompt_template"],
                history=history,
                context=memory_context,
            )
        else:
            response_text = client.chat_with_memory(
                message, history, memory_context, db_path=str(path)
            )
    except Exception:
        logger.exception("Model request raised an exception")
        with _db_connect(path) as connection:
            connection.execute(
                "DELETE FROM interactions WHERE id = ?", (interaction_id,)
            )
        return _error("model_request_failed", "模型服务请求失败，请稍后重试", 502)

    if not isinstance(response_text, str) or not response_text.strip():
        with _db_connect(path) as connection:
            connection.execute(
                "DELETE FROM interactions WHERE id = ?", (interaction_id,)
            )
        return _error("empty_model_response", "模型没有返回有效内容", 502)
    if getattr(client, "response_is_error", lambda value: False)(response_text):
        with _db_connect(path) as connection:
            connection.execute(
                "DELETE FROM interactions WHERE id = ?", (interaction_id,)
            )
        error_code, error_message = _model_error(response_text)
        return _error(error_code, error_message, 502)

    with _db_connect(path) as connection:
        connection.execute(
            "UPDATE interactions SET agent_response = ? WHERE id = ?",
            (response_text, interaction_id),
        )

    if matched_skill:
        try:
            matcher.store.record_trigger(matched_skill["id"])
        except Exception:
            logger.exception("Skill trigger metric update failed")

    if current_app.config["JARVIS_LEARNING_ENABLED"]:
        threading.Thread(
            target=_post_chat_tasks,
            args=(
                current_app._get_current_object(), bridge, client, path,
                user_namespace, message, response_text, history, interaction_id,
            ),
            daemon=True,
        ).start()
    return _ok({
        "response": response_text,
        "session_id": session_id,
        "interaction_id": interaction_id,
    })


def create_app(test_config: Optional[Dict[str, Any]] = None,
               llm_client: Optional[LLMConfig] = None) -> Flask:
    project_config = _load_project_config()
    learning_config = project_config.get("learning", {})
    knowledge_config = learning_config.get("knowledge_extraction", {})
    evolution_config = learning_config.get("evolution", {})
    skill_mining_config = learning_config.get("skill_mining", {})
    app = Flask(
        __name__,
        template_folder=str(PROJECT_DIR / "templates"),
        static_folder=str(PROJECT_DIR / "static"),
    )
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=64 * 1024,
        JARVIS_DB_PATH=_resolve_db_path(os.environ.get("JARVIS_DB_PATH")),
        JARVIS_BACKUP_DIR=_resolve_db_path(
            os.environ.get("JARVIS_BACKUP_DIR", str(PROJECT_DIR / "backups"))
        ),
        JARVIS_API_TOKEN=os.environ.get("JARVIS_API_TOKEN", ""),
        JARVIS_PUBLIC_ORIGIN=os.environ.get("JARVIS_PUBLIC_ORIGIN", "").rstrip("/"),
        JARVIS_TRUSTED_PROXY_HOPS=_int_setting(
            "JARVIS_TRUSTED_PROXY_HOPS", 0, 0, 3
        ),
        JARVIS_LEARNING_ENABLED=_bool_setting(
            "JARVIS_LEARNING_ENABLED", bool(learning_config.get("enabled", False))
        ),
        JARVIS_KNOWLEDGE_EXTRACTION_ENABLED=_bool_setting(
            "JARVIS_KNOWLEDGE_EXTRACTION_ENABLED",
            bool(knowledge_config.get("enabled", False)),
        ),
        JARVIS_EVOLUTION_ENABLED=_bool_setting(
            "JARVIS_EVOLUTION_ENABLED", bool(evolution_config.get("enabled", False))
        ),
        JARVIS_SKILL_MINING_ENABLED=_bool_setting(
            "JARVIS_SKILL_MINING_ENABLED",
            bool(skill_mining_config.get("enabled", False)),
        ),
    )
    if test_config:
        app.config.update(test_config)
        app.config["JARVIS_DB_PATH"] = _resolve_db_path(
            str(app.config["JARVIS_DB_PATH"])
        )
        app.config["JARVIS_BACKUP_DIR"] = _resolve_db_path(
            str(app.config["JARVIS_BACKUP_DIR"])
        )

    proxy_hops = int(app.config["JARVIS_TRUSTED_PROXY_HOPS"])
    if proxy_hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_hops,
            x_proto=proxy_hops,
            x_host=proxy_hops,
            x_port=proxy_hops,
        )

    db_path: Path = app.config["JARVIS_DB_PATH"]
    skill_store = _initialize_database(db_path)

    client = llm_client if llm_client is not None else get_llm()
    if LLM_CONFIGURED is not None:
        LLM_CONFIGURED.set(1 if _llm_available(client) else 0)
    app.extensions["llm"] = client
    app.extensions["memory_bridge"] = get_memory_bridge(str(db_path), llm=client)
    app.extensions["local_command_executor"] = LocalCommandExecutor()
    app.extensions["skill_store"] = skill_store
    app.extensions["skill_matcher"] = SkillMatcher(skill_store)
    app.extensions["rate_limiter"] = RateLimiter()
    app.extensions["session_request_gate"] = SessionRequestGate()

    register_hooks(app)
    register_routes(app)
    return app


def register_hooks(app: Flask) -> None:
    @app.before_request
    def protect_api():
        g.request_started = time.monotonic()
        if not request.path.startswith("/api/"):
            return None
        expected_token = current_app.config.get("JARVIS_API_TOKEN", "")
        if expected_token:
            supplied = (
                request.headers.get("X-Jarvis-Token")
                or request.headers.get("Authorization", "").removeprefix("Bearer ")
            )
            if not hmac.compare_digest(supplied, expected_token):
                return _error("unauthorized", "需要有效的访问令牌", 401)
        if not _api_host_allowed():
            return _error("invalid_host", "请求主机不受信任", 400)

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            expected_origin = (
                current_app.config.get("JARVIS_PUBLIC_ORIGIN")
                or request.host_url.rstrip("/")
            )
            if origin and origin.rstrip("/") != expected_origin:
                return _error("invalid_origin", "请求来源不受信任", 403)
        return None

    @app.after_request
    def secure_response(response: Response):
        if HTTP_REQUESTS is not None:
            route = request.url_rule.rule if request.url_rule else "unmatched"
            HTTP_REQUESTS.labels(request.method, route, response.status_code).inc()
            started = getattr(g, "request_started", None)
            if started is not None:
                HTTP_LATENCY.labels(request.method, route).observe(
                    time.monotonic() - started
                )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(413)
    def payload_too_large(_error_value):
        return _error("payload_too_large", "请求内容过大", 413)

    @app.errorhandler(404)
    def route_not_found(_error_value):
        if request.path.startswith("/api/"):
            return _error("not_found", "接口不存在", 404)
        return "Not found", 404

    @app.errorhandler(Exception)
    def unexpected_error(error_value):
        if isinstance(error_value, HTTPException):
            if request.path.startswith("/api/"):
                return _error(
                    "http_error", error_value.description, error_value.code or 500
                )
            return error_value
        logger.error(
            "Unhandled request error",
            exc_info=(type(error_value), error_value, error_value.__traceback__),
        )
        if request.path.startswith("/api/"):
            return _error("internal_error", "服务器处理请求时发生错误", 500)
        return "Internal server error", 500


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        client = current_app.extensions["llm"]
        return render_template(
            "index.html",
            model_name=getattr(client, "model", "unconfigured"),
            token_required=bool(current_app.config.get("JARVIS_API_TOKEN")),
        )

    @app.get("/health")
    def health():
        try:
            with _db_connect(current_app.config["JARVIS_DB_PATH"]) as connection:
                connection.execute("SELECT 1").fetchone()
            return jsonify({"status": "healthy"})
        except sqlite3.Error:
            return jsonify({"status": "unhealthy"}), 503

    @app.get("/metrics")
    def metrics():
        if generate_latest is None or REGISTRY is None:
            return Response("# prometheus_client unavailable\n", mimetype="text/plain")
        return Response(generate_latest(REGISTRY), content_type=CONTENT_TYPE_LATEST)

    @app.post("/api/session")
    @rate_limited("session", 20, 60)
    def create_session():
        data = _json_body()
        requested_user = _validate_session_id(data.get("user_id"))
        session_id = secrets.token_hex(16)
        with _db_connect(current_app.config["JARVIS_DB_PATH"]) as connection:
            user_id = None
            if requested_user:
                existing_user = connection.execute(
                    "SELECT 1 FROM users WHERE id = ?", (requested_user,)
                ).fetchone()
                if existing_user:
                    user_id = requested_user
            if user_id is None:
                user_id = secrets.token_hex(16)
            connection.execute(
                "INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,)
            )
            connection.execute("""
                INSERT INTO sessions (session_id, user_id, platform, started_at)
                VALUES (?, ?, 'web', ?)
            """, (session_id, user_id, time.time()))
        return _ok({"session_id": session_id, "user_id": user_id}, 201)

    @app.get("/api/status")
    def status():
        path: Path = current_app.config["JARVIS_DB_PATH"]
        with _db_connect(path) as connection:
            episode_count = connection.execute(
                "SELECT COUNT(*) FROM episodes"
            ).fetchone()[0]
            interaction_count = connection.execute(
                "SELECT COUNT(*) FROM interactions"
            ).fetchone()[0]
        client = current_app.extensions["llm"]
        return _ok({
            "status": "running",
            "episodes": episode_count,
            "interactions": interaction_count,
            "db_size_kb": round(path.stat().st_size / 1024, 1),
            "model": getattr(client, "model", "unconfigured"),
            "llm_available": _llm_available(client),
            "learning_enabled": current_app.config["JARVIS_LEARNING_ENABLED"],
        })

    @app.post("/api/chat")
    @rate_limited("chat", 12, 60)
    def chat():
        data = _json_body()
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            return _error("invalid_message", "请输入消息", 400)
        message = message.strip()
        if len(message) > MAX_MESSAGE_LENGTH:
            return _error("message_too_long", "消息长度不能超过 8000 个字符", 400)

        session_id = _validate_session_id(data.get("session_id"))
        path: Path = current_app.config["JARVIS_DB_PATH"]
        user_namespace = _session_user(path, session_id) if session_id else None
        if not session_id or not user_namespace:
            return _error("invalid_session", "会话已失效，请刷新后重试", 401)

        client = current_app.extensions["llm"]

        gate: SessionRequestGate = current_app.extensions["session_request_gate"]
        with gate.hold(session_id) as acquired:
            if not acquired:
                return _error(
                    "session_busy",
                    "该会话正在处理另一条消息，请稍后重试",
                    409,
                )
            return _process_chat(
                path, session_id, user_namespace, message, client
            )

    @app.get("/api/chat/history")
    def chat_history():
        session_id = _validate_session_id(request.args.get("session_id"))
        path: Path = current_app.config["JARVIS_DB_PATH"]
        if not session_id or not _session_exists(path, session_id):
            return _error("invalid_session", "会话不存在", 404)
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            return _error("invalid_limit", "limit 必须是整数", 400)
        limit = max(1, min(limit, 200))

        with _db_connect(path) as connection:
            rows = connection.execute("""
                SELECT id, user_input, agent_response, feedback_value FROM (
                    SELECT i.id, i.user_input, i.agent_response,
                           (SELECT f.feedback_value FROM user_feedback AS f
                            WHERE f.interaction_id = i.id
                              AND f.feedback_type = 'explicit'
                            ORDER BY f.id DESC LIMIT 1) AS feedback_value
                    FROM interactions AS i
                    WHERE i.session_id = ? AND i.user_input != ''
                      AND i.agent_response != ''
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
            """, (session_id, limit)).fetchall()
        history = [
            item
            for row in rows
            for item in (
                {
                    "role": "user", "content": row["user_input"],
                    "interaction_id": row["id"],
                },
                {
                    "role": "assistant", "content": row["agent_response"],
                    "interaction_id": row["id"],
                    "helpful": (
                        None if row["feedback_value"] is None
                        else bool(row["feedback_value"] > 0)
                    ),
                },
            )
        ]
        return _ok({"history": history, "session_id": session_id})

    @app.post("/api/feedback")
    @rate_limited("feedback", 30, 60)
    def submit_feedback():
        data = _json_body()
        session_id = _validate_session_id(data.get("session_id"))
        helpful = data.get("helpful")
        try:
            interaction_id = int(data.get("interaction_id"))
        except (TypeError, ValueError):
            return _error("invalid_interaction", "交互记录无效", 400)
        path: Path = current_app.config["JARVIS_DB_PATH"]
        if not session_id or not _session_exists(path, session_id):
            return _error("invalid_session", "会话不存在", 401)
        if not isinstance(helpful, bool):
            return _error("invalid_feedback", "helpful 必须是布尔值", 400)

        with _db_connect(path) as connection:
            interaction = connection.execute("""
                SELECT user_input, agent_response FROM interactions
                WHERE id = ? AND session_id = ? AND agent_response != ''
            """, (interaction_id, session_id)).fetchone()
            if interaction is None:
                return _error("interaction_not_found", "交互记录不存在", 404)
            score = 1.0 if helpful else 0.0
            feedback_cursor = connection.execute("""
                INSERT INTO user_feedback
                    (session_id, interaction_id, feedback_type,
                     feedback_value, timestamp)
                SELECT ?, ?, 'explicit', ?, ?
                WHERE COALESCE((
                    SELECT feedback_value FROM user_feedback
                    WHERE interaction_id = ? AND feedback_type = 'explicit'
                    ORDER BY id DESC LIMIT 1
                ), -1) != ?
            """, (
                session_id, interaction_id, score, time.time(),
                interaction_id, score,
            ))
            expected = interaction["agent_response"] if helpful else None
            source = "user_approved" if helpful else "user_rejected"
            connection.execute("""
                INSERT INTO eval_cases
                    (interaction_id, user_input, agent_response, expected,
                     feedback_score, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interaction_id) WHERE interaction_id IS NOT NULL
                DO UPDATE SET
                    user_input = excluded.user_input,
                    agent_response = excluded.agent_response,
                    expected = excluded.expected,
                    feedback_score = excluded.feedback_score,
                    source = excluded.source,
                    used_in_evolution = CASE
                        WHEN eval_cases.feedback_score IS excluded.feedback_score
                         AND eval_cases.expected IS excluded.expected
                        THEN eval_cases.used_in_evolution
                        ELSE 0
                    END
            """, (
                interaction_id, interaction["user_input"],
                interaction["agent_response"], expected, score, source,
                time.time(),
            ))
        return _ok({
            "interaction_id": interaction_id,
            "helpful": helpful,
            "eligible_for_evolution": helpful,
            "changed": feedback_cursor.rowcount > 0,
        })

    @app.post("/api/learn")
    @rate_limited("learning", 6, 60)
    def learning_patterns():
        from jarvis.learning.habits import PrefixSpan

        cutoff = time.time() - 30 * 24 * 3600
        with _db_connect(current_app.config["JARVIS_DB_PATH"]) as connection:
            rows = connection.execute("""
                SELECT session_id, user_input FROM (
                    SELECT id, session_id, user_input FROM interactions
                    WHERE timestamp >= ? AND user_input IS NOT NULL
                      AND user_input != ''
                    ORDER BY id DESC LIMIT 2000
                ) ORDER BY id ASC
            """, (cutoff,)).fetchall()
        by_session: Dict[str, list] = defaultdict(list)
        for row in rows:
            by_session[row["session_id"]].append(
                _classify_message(row["user_input"])
            )
        sequences = [sequence[-50:] for sequence in by_session.values()]
        patterns = PrefixSpan(
            min_support=0.3, min_confidence=0.5, max_pattern_length=5
        ).mine_patterns(sequences)
        result = [
            {
                "sequence": " -> ".join(pattern),
                "support": round(support, 2),
                "confidence": round(confidence, 2),
            }
            for pattern, support, confidence in patterns[:10]
        ]
        client = current_app.extensions["llm"]
        analysis = client.analyze_patterns(result) if result and _llm_available(client) else None
        return _ok({
            "patterns": result,
            "ai_analysis": analysis,
            "sample_sessions": len(sequences),
            "sample_interactions": len(rows),
            "window_days": 30,
        })

    @app.get("/api/evolve")
    def evolution_status():
        path: Path = current_app.config["JARVIS_DB_PATH"]
        with _db_connect(path) as connection:
            rows = connection.execute("""
                SELECT id, generation, fitness_score, train_score, holdout_score,
                       mutation_details, approved, created_at
                FROM evolution_history
                WHERE evolution_type = 'prompt'
                ORDER BY id DESC LIMIT 10
            """).fetchall()
            unused = connection.execute(
                """SELECT COUNT(*) FROM eval_cases
                   WHERE used_in_evolution = 0
                     AND expected IS NOT NULL AND TRIM(expected) != ''"""
            ).fetchone()[0]
        history = []
        for row in rows:
            item = dict(row)
            details = item.pop("mutation_details", None)
            try:
                parsed = json.loads(details) if isinstance(details, str) else details
            except json.JSONDecodeError:
                parsed = {}
            item["content"] = parsed.get("content", "") if isinstance(parsed, dict) else ""
            item["approved"] = bool(item["approved"])
            history.append(item)
        return _ok({
            "history": history,
            "available_cases": unused,
        })

    @app.post("/api/evolve/approve")
    @rate_limited("evolution_approval", 20, 60)
    def approve_evolution():
        data = _json_body()
        try:
            evolution_id = int(data.get("id"))
        except (TypeError, ValueError):
            return _error("invalid_evolution", "进化记录无效", 400)
        approved = data.get("approved")
        if not isinstance(approved, bool):
            return _error("invalid_approved", "approved 必须是布尔值", 400)
        if approved and data.get("reviewed") is not True:
            return _error("review_required", "批准前必须确认已审核 Prompt", 400)

        path: Path = current_app.config["JARVIS_DB_PATH"]
        with _db_connect(path) as connection:
            exists = connection.execute("""
                SELECT 1 FROM evolution_history
                WHERE id = ? AND evolution_type = 'prompt'
            """, (evolution_id,)).fetchone()
            if not exists:
                return _error("evolution_not_found", "进化记录不存在", 404)
            if approved:
                connection.execute("""
                    UPDATE evolution_history SET approved = 0
                    WHERE evolution_type = 'prompt'
                """)
            connection.execute(
                "UPDATE evolution_history SET approved = ? WHERE id = ?",
                (1 if approved else 0, evolution_id),
            )
        invalidate_cache = getattr(
            current_app.extensions["llm"], "invalidate_prompt_cache", None
        )
        if invalidate_cache:
            invalidate_cache(str(path))
        return _ok({"id": evolution_id, "approved": approved})

    @app.post("/api/memory/store")
    @rate_limited("memory_write", 20, 60)
    def store_memory():
        data = _json_body()
        session_id = _validate_session_id(data.get("session_id"))
        path: Path = current_app.config["JARVIS_DB_PATH"]
        user_namespace = _session_user(path, session_id) if session_id else None
        if not session_id or not user_namespace:
            return _error("invalid_session", "会话不存在", 401)
        key = data.get("key")
        value = data.get("value")
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > 128:
            return _error("invalid_key", "记忆键必须为 1 到 128 个字符", 400)
        if not isinstance(value, str) or len(value) > MAX_MEMORY_VALUE_LENGTH:
            return _error("invalid_value", "记忆内容格式或长度无效", 400)

        bridge: MemoryBridge = current_app.extensions["memory_bridge"]
        stored_at = time.time()
        bridge.memory.store(
            bridge.scoped_key(user_namespace, f"manual:{key.strip()}"),
            {"data": value, "timestamp": stored_at},
            memory_tier="long_term",
            importance=0.8,
            metadata={"type": "manual"},
        )
        return _ok({"key": key.strip(), "timestamp": stored_at}, 201)

    @app.get("/api/memory/retrieve")
    def retrieve_memory():
        session_id = _validate_session_id(request.args.get("session_id"))
        path: Path = current_app.config["JARVIS_DB_PATH"]
        user_namespace = _session_user(path, session_id) if session_id else None
        if not session_id or not user_namespace:
            return _error("invalid_session", "会话不存在", 401)
        key = (request.args.get("key") or "").strip()
        if not key or len(key) > 128:
            return _error("invalid_key", "记忆键格式无效", 400)
        bridge: MemoryBridge = current_app.extensions["memory_bridge"]
        result = bridge.memory.long_term.retrieve(
            bridge.scoped_key(user_namespace, f"manual:{key}")
        )
        if result is None:
            return _ok({"found": False, "key": key})
        return _ok({
            "found": True,
            "key": key,
            "value": result.get("data", "") if isinstance(result, dict) else result,
            "timestamp": result.get("timestamp") if isinstance(result, dict) else None,
        })

    @app.get("/api/skills")
    def list_skills():
        skills = current_app.extensions["skill_store"].get_all(enabled_only=False)
        public_fields = (
            "id", "name", "description", "trigger_keywords", "trigger_count",
            "last_triggered_at", "enabled", "reviewed", "prompt_template",
        )
        return _ok({
            "skills": [
                {
                    **{field: skill.get(field) for field in public_fields},
                    "enabled": bool(skill.get("enabled")),
                }
                for skill in skills
            ]
        })

    @app.post("/api/skills/mine")
    @rate_limited("skill_mining", 2, 3600)
    def manual_mine_skills():
        client = current_app.extensions["llm"]
        if not _llm_available(client):
            return _error("llm_unavailable", "模型服务未配置", 503)
        from jarvis.learning.skill_miner import SkillMiner

        miner = SkillMiner(str(current_app.config["JARVIS_DB_PATH"]), llm=client)
        new_skills = miner.mine()
        current_app.extensions["skill_matcher"].invalidate_cache()
        return _ok({
            "new_skills": [item.get("name") for item in new_skills],
            "count": len(new_skills),
        })

    @app.post("/api/skills/toggle")
    @rate_limited("skill_toggle", 30, 60)
    def toggle_skill():
        data = _json_body()
        try:
            skill_id = int(data.get("id"))
        except (TypeError, ValueError):
            return _error("invalid_skill", "Skill ID 无效", 400)
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return _error("invalid_enabled", "enabled 必须是布尔值", 400)
        if enabled and data.get("reviewed") is not True:
            return _error("review_required", "启用前必须确认已审核 Skill", 400)
        if not current_app.extensions["skill_store"].toggle(skill_id, enabled):
            return _error("skill_not_found", "Skill 不存在", 404)
        current_app.extensions["skill_matcher"].invalidate_cache()
        return _ok({"id": skill_id, "enabled": enabled})

    @app.get("/api/database/stats")
    def database_stats():
        table_info = (
            ("episodes", "学习经验"),
            ("patterns", "行为模式"),
            ("interactions", "对话交互"),
            ("sessions", "会话"),
            ("user_feedback", "用户反馈"),
            ("learning_state", "学习状态"),
            ("evolution_history", "进化历史"),
            ("error_records", "错误记录"),
            ("knowledge_nodes", "知识节点"),
            ("knowledge_edges", "知识关系"),
        )
        tables = []
        with _db_connect(current_app.config["JARVIS_DB_PATH"]) as connection:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for name, description in table_info:
                if name in existing:
                    count = connection.execute(
                        f'SELECT COUNT(*) FROM "{name}"'
                    ).fetchone()[0]
                    tables.append({
                        "name": name,
                        "count": count,
                        "description": description,
                    })
        return _ok({"tables": tables})

    @app.post("/api/backup")
    @rate_limited("backup", 2, 3600)
    def backup_database():
        source_path: Path = current_app.config["JARVIS_DB_PATH"]
        backup_dir: Path = current_app.config["JARVIS_BACKUP_DIR"]
        _private_directory(backup_dir)
        filename = f"jarvis_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.db"
        target_path = backup_dir / filename
        source = _db_connect(source_path)
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        _private_file(target_path)
        return _ok({"filename": filename}, 201)


def _post_chat_tasks(app: Flask, bridge: MemoryBridge, client: LLMConfig,
                     path: Path, user_namespace: str, message: str,
                     response_text: str, history: list,
                     interaction_id: Optional[int] = None) -> None:
    with app.app_context():
        if not app.config["JARVIS_LEARNING_ENABLED"]:
            return
        context = "\n".join(
            f"用户: {item['content']}" if item["role"] == "user"
            else f"贾维斯: {item['content']}"
            for item in history[-4:]
        )
        if app.config["JARVIS_KNOWLEDGE_EXTRACTION_ENABLED"]:
            try:
                facts = bridge.extract_facts(message, context)
                if facts:
                    bridge.store_facts(facts, namespace=user_namespace)
            except Exception:
                logger.exception("Background fact extraction failed")
        try:
            client.record_eval_case(
                message, response_text, interaction_id=interaction_id,
                db_path=str(path),
            )
        except Exception:
            logger.exception("Eval case persistence failed")
        if app.config["JARVIS_EVOLUTION_ENABLED"]:
            _maybe_trigger_evolution(app, path, client)
        if app.config["JARVIS_SKILL_MINING_ENABLED"]:
            _maybe_trigger_skill_mining(app, path, client)


_evolution_lock = threading.Lock()
_evolution_last_run: Dict[str, float] = {}
_evolution_retry_after: Dict[str, float] = {}


def _maybe_trigger_evolution(app: Flask, path: Path, client: LLMConfig,
                             min_cases: int = 6,
                             cooldown_hours: int = 24,
                             max_cases: int = 6,
                             failure_backoff_seconds: int = 3600) -> None:
    key = str(path)
    now = time.time()
    with _evolution_lock:
        if now < _evolution_retry_after.get(key, 0):
            return
        if now - _evolution_last_run.get(key, 0) < cooldown_hours * 3600:
            return
        with _db_connect(path) as connection:
            rows = connection.execute("""
                SELECT id, user_input, expected
                FROM eval_cases
                WHERE used_in_evolution = 0
                  AND expected IS NOT NULL AND TRIM(expected) != ''
                ORDER BY id ASC LIMIT ?
            """, (max_cases,)).fetchall()
        if len(rows) < min_cases:
            return
        _evolution_last_run[key] = now

    def evolve() -> None:
        try:
            from jarvis.learning.evolution import DarwinianEvolver, Organism

            split = max(1, int(len(rows) * 0.7))
            cases = [
                {
                    "id": row["id"],
                    "input": row["user_input"],
                    "expected_output": row["expected"],
                }
                for row in rows
            ]
            seed = Organism(
                id="evolve_seed",
                organism_type="prompt",
                content=client.get_current_best_prompt(str(path)),
                generation=0,
            )
            best = DarwinianEvolver(
                db_path=str(path), max_generations=2, population_size=2,
                target_fitness=0.85, max_mutations=1, max_llm_calls=25,
                max_duration_seconds=300, llm=client,
            ).evolve([seed], cases[:split], cases[split:], "prompt")
            if best is None:
                raise RuntimeError("evolution returned no result")
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            with _db_connect(path) as connection:
                connection.execute(
                    f"UPDATE eval_cases SET used_in_evolution = 1 WHERE id IN ({placeholders})",
                    ids,
                )
            invalidate_cache = getattr(client, "invalidate_prompt_cache", None)
            if invalidate_cache:
                invalidate_cache(str(path))
            with _evolution_lock:
                _evolution_retry_after.pop(key, None)
        except Exception:
            logger.exception("Background evolution failed")
            with _evolution_lock:
                _evolution_last_run.pop(key, None)
                _evolution_retry_after[key] = (
                    time.time() + max(60, int(failure_backoff_seconds))
                )

    threading.Thread(target=evolve, daemon=True).start()


_skill_lock = threading.Lock()
_skill_last_run: Dict[str, float] = {}


def _maybe_trigger_skill_mining(app: Flask, path: Path, client: LLMConfig,
                                min_interactions: int = 50,
                                cooldown_hours: int = 12) -> None:
    key = str(path)
    now = time.time()
    with _skill_lock:
        if now - _skill_last_run.get(key, 0) < cooldown_hours * 3600:
            return
        with _db_connect(path) as connection:
            count = connection.execute("""
                SELECT COUNT(*) FROM interactions
                WHERE user_input != '' AND agent_response != ''
            """).fetchone()[0]
        if count < min_interactions:
            return
        _skill_last_run[key] = now

    def mine() -> None:
        try:
            from jarvis.learning.skill_miner import SkillMiner

            SkillMiner(str(path), llm=client).mine()
            with app.app_context():
                app.extensions["skill_matcher"].invalidate_cache()
        except Exception:
            logger.exception("Background skill mining failed")
            with _skill_lock:
                _skill_last_run.pop(key, None)

    threading.Thread(target=mine, daemon=True).start()


def main() -> None:
    host = os.environ.get("JARVIS_HOST", "127.0.0.1")
    port = int(os.environ.get("JARVIS_PORT", "8000"))
    app = create_app()
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
