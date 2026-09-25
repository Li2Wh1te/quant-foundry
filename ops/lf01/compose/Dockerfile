FROM ghcr.io/astral-sh/uv:0.11.7 AS uv
FROM python:3.12.2-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-install-project
COPY VERSION /app/VERSION
COPY backend/app ./app
COPY backend/tests ./tests
COPY backend/alembic.ini ./
COPY ops/lf01/container_probe.py /app/container_probe.py
COPY scripts/benchmark_current_store.py /app/benchmark_current_store.py
ENV PATH="/app/backend/.venv/bin:$PATH" PYTHONPATH="/app/backend"
CMD ["python", "/app/container_probe.py", "inspect"]
