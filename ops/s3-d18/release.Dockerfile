# Build host supplies locked dependency wheels and the already tested SDK wheel.
# No engine compilation or network package installation occurs in this image.
FROM scratch AS release-source
COPY backend/app /app/app
COPY backend/alembic.ini /app/alembic.ini
COPY VERSION /app/VERSION
COPY s3-source.json /app/s3-source.json

FROM python:3.12.2-slim@sha256:5dc6f84b5e97bfb0c90abfb7c55f3cacc2cb6687c8f920b64a833a2219875997
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY dependencies/ /install/dependencies/
COPY backend.lock /install/backend.lock
COPY sdk/python/requirements-runtime.lock /install/sdk.lock
COPY wheels/ /install/wheels/
RUN pip install --no-index --find-links=/install/dependencies --no-cache-dir --require-hashes -r /install/backend.lock && \
    pip install --no-index --find-links=/install/dependencies --no-cache-dir --require-hashes -r /install/sdk.lock && \
    pip install --no-index --no-cache-dir --no-deps /install/wheels/*.whl && rm -rf /install
COPY --from=release-source /app /app
RUN chmod -R a+rX /app && mkdir -p /app/data/logs /app/data/current-store /app/data/s3-results /qf-work && \
    chown 65532:65532 /app/data/logs /app/data/current-store /app/data/s3-results /qf-work
WORKDIR /app
USER 65532:65532
CMD ["python", "-m", "app"]
