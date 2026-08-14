#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/compose.postgres.yaml"
ENV_FILE="${PROJECT_ROOT}/.env.postgres"
ENV_EXAMPLE="${PROJECT_ROOT}/.env.postgres.example"

usage() {
    cat <<'EOF'
Usage: scripts/postgres.sh <command>

Commands:
  up       Start PostgreSQL, wait until healthy, and run migrations (default)
  migrate  Run all pending Alembic migrations
  status   Show the PostgreSQL container status
  logs     Follow PostgreSQL logs
  psql     Open a psql shell in the PostgreSQL container
  down     Stop and remove the container, preserving database data
  reset    Delete all local database data and initialize a clean database
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

ensure_environment() {
    if [[ ! -f "${ENV_FILE}" ]]; then
        cp "${ENV_EXAMPLE}" "${ENV_FILE}"
        echo "Created ${ENV_FILE} from .env.postgres.example"
    fi

    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a

    : "${POSTGRES_USER:?POSTGRES_USER is required in .env.postgres}"
    : "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required in .env.postgres}"
    : "${POSTGRES_DB:?POSTGRES_DB is required in .env.postgres}"
    : "${POSTGRES_PORT:?POSTGRES_PORT is required in .env.postgres}"
}

compose() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

resolve_python_commands() {
    if [[ -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
        PYTHON_COMMAND=("${PROJECT_ROOT}/.venv/bin/python")
        ALEMBIC_COMMAND=("${PROJECT_ROOT}/.venv/bin/alembic")
        return
    fi

    require_command uv
    PYTHON_COMMAND=(uv run python)
    ALEMBIC_COMMAND=(uv run alembic)
}

database_url() {
    "${PYTHON_COMMAND[@]}" -c '
import sys
from sqlalchemy import URL

url = URL.create(
    "postgresql+psycopg",
    username=sys.argv[1],
    password=sys.argv[2],
    host="127.0.0.1",
    port=int(sys.argv[3]),
    database=sys.argv[4],
)
print(url.render_as_string(hide_password=False))
' "${POSTGRES_USER}" "${POSTGRES_PASSWORD}" "${POSTGRES_PORT}" "${POSTGRES_DB}"
}

run_migrations() {
    resolve_python_commands
    local url
    url="$(database_url)"

    echo "Applying database migrations..."
    (
        cd "${PROJECT_ROOT}"
        QF_DATABASE_URL="${url}" "${ALEMBIC_COMMAND[@]}" upgrade head
    )
}

start_database() {
    echo "Starting PostgreSQL..."
    compose up -d --wait --wait-timeout 60
    run_migrations
    echo "PostgreSQL is ready on 127.0.0.1:${POSTGRES_PORT}/${POSTGRES_DB}"
}

reset_database() {
    local answer
    read -r -p "Delete the local PostgreSQL data volume? [y/N] " answer
    if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
        echo "Reset cancelled."
        return
    fi

    compose down --volumes --remove-orphans
    start_database
}

main() {
    local command="${1:-up}"

    case "${command}" in
        help|-h|--help)
            usage
            return
            ;;
        up|migrate|status|logs|psql|down|reset)
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac

    require_command docker
    docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
    ensure_environment

    case "${command}" in
        up)
            start_database
            ;;
        migrate)
            run_migrations
            ;;
        status)
            compose ps
            ;;
        logs)
            compose logs --follow postgres
            ;;
        psql)
            compose exec postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"
            ;;
        down)
            compose down --remove-orphans
            ;;
        reset)
            reset_database
            ;;
    esac
}

main "$@"
