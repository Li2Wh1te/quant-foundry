# Private CI runner only. The production backend image is untouched.
FROM python:3.12.2-slim@sha256:5dc6f84b5e97bfb0c90abfb7c55f3cacc2cb6687c8f920b64a833a2219875997
COPY sdk/python/requirements-build.lock /install/requirements.lock
COPY dependencies/ /install/dependencies/
COPY backend.lock /install/backend.lock
COPY wheels/ /install/wheels/
RUN pip install --no-index --find-links=/install/dependencies --no-cache-dir --require-hashes -r /install/backend.lock && \
    pip install --no-index --find-links=/install/dependencies --no-cache-dir --require-hashes -r /install/requirements.lock && \
    pip install --no-index --no-cache-dir --no-deps /install/wheels/*.whl && rm -rf /install
COPY backend/app /app/app
COPY backend/app/runner /app/app/runner
COPY sdk/python/tests/support_sdk.py /app/support_sdk.py
COPY tests/s3_runtime_suite.py /app/s3_runtime_suite.py
COPY tests/s3_runservice_suite.py /app/s3_runservice_suite.py
COPY backend/alembic.ini /app/alembic.ini
COPY VERSION /app/VERSION
RUN chmod -R a+rX /app
WORKDIR /app
# This trusted test runner alone receives its authorized private daemon socket.
CMD ["python", "s3_runtime_suite.py"]
