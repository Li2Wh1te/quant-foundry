FROM ghcr.io/astral-sh/uv:0.11.7 AS uv

FROM python:3.12.2-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

COPY --from=uv /uv /uvx /bin/

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY VERSION ./VERSION
COPY backend/alembic.ini ./
COPY backend/app ./app

RUN mkdir -p /app/data/logs && chown -R app:app /app

USER app

CMD ["python", "-m", "app"]
