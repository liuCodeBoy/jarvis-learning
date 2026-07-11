FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim

LABEL maintainer="J.A.R.V.I.S. team" \
      version="4.1.0" \
      description="J.A.R.V.I.S. self-learning assistant"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system jarvis \
    && useradd --system --gid jarvis --home-dir /app jarvis

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JARVIS_DB_PATH=/app/data/jarvis_learning.db

WORKDIR /app
COPY --chown=jarvis:jarvis . /app/
RUN mkdir -p /app/data /app/logs /app/backups && chown -R jarvis:jarvis /app

USER jarvis
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "130", "--access-logfile", "-", "--error-logfile", "-", "jarvis.api.wsgi:app"]
