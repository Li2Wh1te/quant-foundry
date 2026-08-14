#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/compose.yaml"
ENV_FILE="${PROJECT_ROOT}/.env"
ENV_EXAMPLE="${PROJECT_ROOT}/.env.example"
PYTHON_IMAGE="python:3.12.2-slim"

usage() {
    cat <<'EOF'
Usage: scripts/selfhost.sh <command>

Commands:
  up       Build and start PostgreSQL and Server (default)
  down     Stop both containers while preserving data
  logs     Follow PostgreSQL and Server logs
  migrate  Apply pending Alembic migrations
  psql     Open a psql shell in the PostgreSQL container
  reset    Delete local data and deploy a clean stack
  status   Show container status
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

require_docker() {
    command -v docker >/dev/null 2>&1 || fail "Docker is required"
    docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
    docker info >/dev/null 2>&1 || fail "Docker daemon is not running"
}

compose() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

ensure_environment() {
    local user_spec
    user_spec="$(id -u):$(id -g)"

    docker run --rm \
        --user "${user_spec}" \
        --volume "${PROJECT_ROOT}:/workspace" \
        --workdir /workspace \
        "${PYTHON_IMAGE}" \
        python scripts/selfhost_env.py --env .env --template .env.example
}

run_migrations() {
    echo "Applying database migrations..."
    compose run --rm --no-deps server alembic upgrade head
}

deploy() {
    ensure_environment

    echo "Building the Server image..."
    compose build server

    compose stop server >/dev/null 2>&1 || true

    echo "Starting PostgreSQL..."
    compose up -d --wait --wait-timeout 60 postgres

    run_migrations

    echo "Starting Server..."
    compose up -d --no-build --wait --wait-timeout 120 server

    echo "Quant Foundry is ready at http://$(compose port server 8000)"
}

reset_stack() {
    local answer
    read -r -p "Delete PostgreSQL data and Server logs? [y/N] " answer
    if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
        echo "Reset cancelled."
        return
    fi

    compose down --volumes --remove-orphans
    deploy
}

main() {
    local command="${1:-up}"

    case "${command}" in
        help|-h|--help)
            usage
            return
            ;;
        up|down|logs|migrate|psql|reset|status)
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac

    require_docker

    if [[ "${command}" != "up" && ! -f "${ENV_FILE}" ]]; then
        fail ".env does not exist; run make selfhost first"
    fi

    case "${command}" in
        up)
            deploy
            ;;
        down)
            compose down --remove-orphans
            ;;
        logs)
            compose logs --follow postgres server
            ;;
        migrate)
            run_migrations
            ;;
        psql)
            compose exec postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
            ;;
        reset)
            reset_stack
            ;;
        status)
            compose ps
            ;;
    esac
}

main "$@"
