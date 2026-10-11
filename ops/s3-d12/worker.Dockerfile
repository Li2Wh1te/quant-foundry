# Prebuilt wheel; no backend source, DB client, credentials or source data.
FROM python:3.12.2-slim@sha256:5dc6f84b5e97bfb0c90abfb7c55f3cacc2cb6687c8f920b64a833a2219875997
COPY sdk/python/requirements-build.lock /install/requirements.lock
COPY dependencies/ /install/dependencies/
COPY wheels/ /install/wheels/
RUN pip install --no-index --find-links=/install/dependencies --no-cache-dir --require-hashes -r /install/requirements.lock && \
    pip install --no-index --no-cache-dir --no-deps /install/wheels/*.whl && \
    rm -rf /install && mkdir -p /run/qf/code /run/qf/ipc
USER 65532:65532
ENTRYPOINT ["python", "-I", "-m", "quantfoundry._worker"]
CMD ["--supervised"]
