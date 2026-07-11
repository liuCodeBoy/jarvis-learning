#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if ! command -v docker >/dev/null 2>&1; then
    printf 'Docker is required.\n' >&2
    exit 1
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    printf 'Docker Compose is required.\n' >&2
    exit 1
fi

mkdir -p data logs backups

# Match the host owner for bind-mounted runtime directories while keeping the
# application processes non-root inside the containers.
export JARVIS_UID="${JARVIS_UID:-$(id -u)}"
export JARVIS_GID="${JARVIS_GID:-$(id -g)}"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    printf 'Warning: no model credential is configured; chat will remain offline.\n' >&2
fi
if [[ -z "${JARVIS_API_TOKEN:-}" ]]; then
    printf 'Warning: JARVIS_API_TOKEN is empty. Keep JARVIS_BIND_HOST on 127.0.0.1.\n' >&2
fi

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" up --build --detach

port="${JARVIS_PORT:-8000}"
for _ in $(seq 1 30); do
    if curl --fail --silent "http://127.0.0.1:${port}/health" >/dev/null; then
        printf 'J.A.R.V.I.S. is healthy at http://127.0.0.1:%s\n' "$port"
        exit 0
    fi
    sleep 2
done

"${COMPOSE[@]}" ps
printf 'Deployment did not become healthy within 60 seconds.\n' >&2
exit 1
