#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/compose.yaml"
ENV_FILE="${PROJECT_ROOT}/.env"
PYTHON_IMAGE="python:3.12.2-slim"

usage() {
    cat <<'EOF'
Usage: scripts/selfhost.sh <command>

Commands:
  up                Build and start PostgreSQL, Backend, and Frontend (default)
  deploy-frontend   Build and recreate only the Frontend container
  deploy-backend    Build, migrate, and recreate only the Backend container
  restart-postgres  Restart PostgreSQL and wait until it is healthy
  down              Stop all containers while preserving data
  logs              Follow PostgreSQL, Backend, and Frontend logs
  migrate           Apply pending Alembic migrations
  psql              Open a psql shell in the PostgreSQL container
  reset             Delete local data and deploy a clean stack
  status            Show container status
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
        python scripts/selfhost_env.py --env .env --template backend/.env.example
}

run_migrations() {
    echo "Applying database migrations..."
    compose run --rm --no-deps backend alembic upgrade head
}

deploy() {
    ensure_environment

    echo "Building the Backend and Frontend images..."
    compose build backend frontend

    compose stop frontend backend >/dev/null 2>&1 || true

    echo "Starting PostgreSQL..."
    compose up -d --wait --wait-timeout 60 postgres

    run_migrations

    echo "Starting Backend and Frontend..."
    compose up -d --no-build --wait --wait-timeout 120 backend frontend

    echo "Quant Foundry is ready at http://$(compose port frontend 80)"
}

deploy_frontend() {
    echo "Building the Frontend image..."
    compose build frontend

    echo "Recreating Frontend..."
    compose up -d --no-deps --no-build --wait --wait-timeout 60 frontend

    echo "Frontend is ready at http://$(compose port frontend 80)"
}

deploy_backend() {
    echo "Building the Backend image..."
    compose build backend

    echo "Ensuring PostgreSQL is healthy..."
    compose up -d --wait --wait-timeout 60 postgres

    echo "Stopping the current Backend..."
    compose stop backend >/dev/null 2>&1 || true

    run_migrations

    echo "Starting Backend..."
    compose up -d --no-deps --no-build --wait --wait-timeout 120 backend
}

restart_postgres() {
    echo "Restarting PostgreSQL..."
    compose restart postgres
    compose up -d --no-deps --wait --wait-timeout 60 postgres
    echo "PostgreSQL is healthy."
}

reset_stack() {
    local answer
    read -r -p "Delete PostgreSQL data and Backend logs? [y/N] " answer
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
        up|deploy-frontend|deploy-backend|restart-postgres|down|logs|migrate|psql|reset|status)
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
        deploy-frontend)
            deploy_frontend
            ;;
        deploy-backend)
            deploy_backend
            ;;
        restart-postgres)
            restart_postgres
            ;;
        down)
            compose down --remove-orphans
            ;;
        logs)
            compose logs --follow postgres backend frontend
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
