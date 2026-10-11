#!/usr/bin/env bash
# Private test resources only. No prune, production daemon or permission changes.
set -euo pipefail
qf_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
qf_build=${QF_S3_BUILD_ROOT:?Build and test the wheel first}
qf_final=${QF_S3_FINAL:-0}
qf_runner_memory=1g
qf_database_memory=512m
qf_command=(python s3_runtime_suite.py)
if [[ $qf_final == 1 ]]; then
    qf_runner_memory=2g
    qf_database_memory=1g
    qf_command=(python s3_final_suite.py)
fi
qf_test_root=$(mktemp -d "${TMPDIR:-/tmp}/qf-s3-d12.XXXXXX")
export DOCKER_CONFIG="$qf_test_root/docker-config"
mkdir -p "$DOCKER_CONFIG"
qf_suffix=${qf_test_root##*.}
qf_runner=qf-s3-d12-runner-${qf_suffix,,}
qf_database=qf-s3-d13-db-${qf_suffix,,}
qf_worker_tag=qf-s3-d12-worker:${qf_suffix,,}
qf_runner_tag=qf-s3-d12-runner:${qf_suffix,,}
qf_worker_id=''
qf_runner_id=''
cleanup() {
    docker rm -f "$qf_runner" >/dev/null 2>&1 || true
    # PG makes this private socket directory sticky and owns its two files.
    # Remove only those exact test endpoints as their container owner, before
    # removing our private DB. Never broaden this to a host path/volume cleanup.
    docker exec --user postgres "$qf_database" rm -f -- \
        /var/run/postgresql/.s.PGSQL.5432 /var/run/postgresql/.s.PGSQL.5432.lock >/dev/null 2>&1 || true
    docker rm -fv "$qf_database" >/dev/null 2>&1 || true
    # Recover interrupted tests by exact claim AND this newly allocated mount
    # root. No label/name alone authorizes touching any other test/production.
    PYTHONPATH="$qf_root/backend" python3 - "$qf_test_root/work" <<'PY'
import pathlib,sys
from app.backtest_service.process import DockerDaemon, LABEL
from app.backtest_service.runtime_contracts import Claim
daemon= DockerDaemon(); root=pathlib.Path(sys.argv[1])
for row in daemon.request('GET','/containers/json?all=1'):
    item=daemon.inspect(row['Id'])
    if item is None: continue
    labels=item['Config'].get('Labels') or {}
    if labels.get('io.quantfoundry.s3.private-test-root')==str(root):
        daemon.request('POST','/containers/'+item['Id']+'/kill?signal=KILL',missing=True,stopped=True)
        daemon.request('DELETE','/containers/'+item['Id'],missing=True)
        continue
    mounts=item.get('Mounts',[])
    if labels.get(LABEL)!='1' or len(mounts)!=2: continue
    if {m['Destination'] for m in mounts}!={'/run/qf/code','/run/qf/ipc'}: continue
    if not all(not m['RW'] and pathlib.Path(m['Source']).is_relative_to(root) for m in mounts): continue
    daemon.stop_remove(item['Id'],Claim(labels[LABEL+'.run'],labels[LABEL+'.token'],int(labels[LABEL+'.fence'])))
PY
    # Only our own temporary test image tags.
    [[ -z $qf_runner_id ]] || docker image rm "$qf_runner_tag" >/dev/null 2>&1 || true
    [[ -z $qf_worker_id ]] || docker image rm "$qf_worker_tag" >/dev/null 2>&1 || true
    if [[ -f $qf_test_root/work/isolation-evidence.json ]]; then
        mkdir -p "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}"
        cp "$qf_test_root/work/isolation-evidence.json" "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}/"
    fi
    if [[ -f $qf_test_root/work/final/result.json ]]; then
        mkdir -p "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}"
        cp "$qf_test_root/work/final/result.json" "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}/final-result.json"
    fi
    # A failed test can leave its own immutable code directory mode 0555.
    python3 - "$qf_test_root" <<'PY'
import os,pathlib,sys
root=pathlib.Path(sys.argv[1])
for directory,children,files in os.walk(root,followlinks=False):
    path=pathlib.Path(directory)
    if not path.is_symlink() and path.stat().st_uid==os.getuid():os.chmod(path,0o700)
PY
    rm -rf -- "$qf_test_root"
}
trap cleanup EXIT
mkdir -p "$qf_test_root/build" "$qf_test_root/work" "$qf_test_root/db-socket" "$qf_build/runtime-deps"
if [[ $qf_final == 1 ]]; then
    python3 "$qf_root/scripts/s3_release.py" context --manifest "$qf_build/release.json" \
        --wheel "$qf_build"/wheels/quantfoundry_sdk-*.whl --out "$qf_test_root/build/s3-source.json"
else
    printf '{}\n' > "$qf_test_root/build/s3-source.json"
fi
chmod 777 "$qf_test_root/db-socket"
# Resolve/download on the build host from existing hashes. Both Docker builds
# run with network=none; runtime dependency installation never needs networking.
UV_CACHE_DIR=${UV_CACHE_DIR:-$qf_build/uv-cache} uv export -q --project "$qf_root/backend" --locked --no-dev --no-emit-project \
    --format requirements-txt -o "$qf_build/runtime-deps/backend.lock"
"$qf_build/build/bin/python" -m pip download --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
    -r "$qf_build/runtime-deps/backend.lock" -d "$qf_build/runtime-deps/wheels"
"$qf_build/build/bin/python" -m pip download --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
    -r "$qf_root/sdk/python/requirements-build.lock" -d "$qf_build/runtime-deps/wheels"
cp "$qf_build/runtime-deps/backend.lock" "$qf_test_root/build/backend.lock"
cp -r "$qf_build/runtime-deps/wheels" "$qf_test_root/build/dependencies"
# Build contexts contain repository code and this tested wheel only, no .env/git.
python3 - "$qf_root" "$qf_build" "$qf_test_root/build" <<'PY'
import pathlib,shutil,sys
repo,build,target=map(pathlib.Path,sys.argv[1:])
for p in ['VERSION','backend/app','backend/pyproject.toml','backend/uv.lock','backend/alembic.ini','sdk/python/requirements-build.lock','sdk/python/requirements-runtime.lock',
          'sdk/python/tests/support_sdk.py','ops/s3-d12','tests/s3_runtime_suite.py','tests/s3_runservice_suite.py',
          'tests/s3_cli_suite.py','tests/s3_cli_fixtures.py','tests/s3_final_suite.py','ops/s3-d18','examples/strategies','examples/http_workflow.py','docs/backtesting']:
    dest=target/p;dest.parent.mkdir(parents=True,exist_ok=True)
    if (repo/p).is_dir():shutil.copytree(repo/p,dest,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    else:shutil.copyfile(repo/p,dest)
shutil.copytree(build/'wheels',target/'wheels')
PY
docker build --network none -f "$qf_test_root/build/ops/s3-d12/worker.Dockerfile" -t "$qf_worker_tag" "$qf_test_root/build"
qf_worker_id=$(docker image inspect --format '{{.Id}}' "$qf_worker_tag")
docker build --network none -f "$qf_test_root/build/ops/s3-d12/runner-test.Dockerfile" -t "$qf_runner_tag" "$qf_test_root/build"
qf_runner_id=$(docker image inspect --format '{{.Id}}' "$qf_runner_tag")
# A freshly allocated private PG has no network; its own anonymous data volume
# is removed with this exact container in cleanup. Only the
# trusted test runner mounts its socket; sibling strategy workers never do.
docker pull postgres:17
docker run -d --name "$qf_database" --network none --cpus 1 --memory "$qf_database_memory" --memory-swap "$qf_database_memory" --pids-limit 32 \
    --label "io.quantfoundry.s3.private-test-root=$qf_test_root/work" \
    --mount "type=bind,src=$qf_test_root/db-socket,dst=/var/run/postgresql" \
    --env POSTGRES_DB=qf_d13_test --env POSTGRES_PASSWORD=private-d13-test postgres:17 >/dev/null
qf_pg_ready=0
for qf_try in $(seq 1 60); do
    if docker exec "$qf_database" pg_isready -U postgres -d qf_d13_test >/dev/null 2>&1; then
        qf_pg_ready=1
        break
    fi
    sleep 1
done
[[ $qf_pg_ready == 1 ]]
# This root is a newly created test-only daemon path. The runner sees /qf-work,
# and its sibling workers bind daemon-host paths beneath this exact directory.
docker run --name "$qf_runner" --network none --cpus 1 --memory "$qf_runner_memory" --memory-swap "$qf_runner_memory" --pids-limit 64 \
    --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)" \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
    --mount "type=bind,src=$qf_test_root/work,dst=/qf-work" \
    --mount "type=bind,src=$qf_test_root/db-socket,dst=/qf-db,readonly" \
    --env "QF_TEST_DAEMON_ROOT=$qf_test_root/work" --env "QF_TEST_WORKER_IMAGE=$qf_worker_id" \
    --env "QF_TEST_RUNNER_NAME=$qf_runner" \
    --env "QF_TEST_DATABASE_NAME=$qf_database" \
    --env "QF_S3_FINAL_SMOKE=${QF_S3_FINAL_SMOKE:-0}" \
    --env "QF_S3_FINAL_CASES=${QF_S3_FINAL_CASES:-}" \
    --env "QF_S3_CI_RUN_URL=${QF_S3_CI_RUN_URL:-}" \
    --env "LF_D01_ALLOW_OVERLAY_TEST=${LF_D01_ALLOW_OVERLAY_TEST:-0}" \
    --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1 --env MKL_NUM_THREADS=1 --env NUMEXPR_NUM_THREADS=1 \
    --env QF_ENVIRONMENT=test --env QF_DATABASE_HOST=/qf-db --env QF_DATABASE_USER=postgres \
    --env QF_DATABASE_PASSWORD=private-d13-test --env QF_DATABASE_NAME=qf_d13_test \
    --env QF_API_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    --env QF_CURSOR_SIGNING_KEY=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
    --env QF_D13_REAL_WORKER_REQUIRED=1 --env QF_D14_REAL_WORKER_REQUIRED=1 --env QF_D13_TEST_ROOT=/qf-work/d13-service \
    "$qf_runner_id" "${qf_command[@]}"
if [[ $qf_final == 1 ]]; then
    mkdir -p "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}"
    cp "$qf_test_root/work/final/result.json" "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}/final-result.json"
    exit 0
fi
mkdir -p "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}"
cp "$qf_test_root/work/isolation-evidence.json" "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}/"
cp "$qf_test_root/work/d14-service/d14-examples.json" "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}/"
cp "$qf_test_root/work/d13-service/retired-execution-writes.json" "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}/"
