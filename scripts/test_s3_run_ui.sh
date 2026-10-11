#!/usr/bin/env bash
# D15 browser -> real private HTTP/PG/CurrentStore -> runner -> sibling worker.
# No production access, supplier calls, deploys or daemon-wide cleanup.
set -euo pipefail
qf_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
qf_build=${QF_S3_BUILD_ROOT:?Build the locked SDK wheel first}
qf_evidence=${QF_D15_EVIDENCE:?Choose a browser evidence directory}
qf_d16=${QF_D16_UI:-0}
qf_test_root=$(mktemp -d "${TMPDIR:-/tmp}/q15-ui.XXXXXX")
export DOCKER_CONFIG="$qf_test_root/docker-config"
qf_suffix=${qf_test_root##*.}
qf_runner=qf-d15-runner-${qf_suffix,,}
qf_database=qf-d15-pg-${qf_suffix,,}
qf_worker_tag=qf-d15-worker:${qf_suffix,,}
qf_runner_tag=qf-d15-runner:${qf_suffix,,}
qf_vite_pid=''
mkdir -p "$qf_evidence" "$DOCKER_CONFIG" "$qf_test_root/build" "$qf_test_root/work" "$qf_test_root/db-socket" "$qf_build/ui-deps"
cleanup() {
    [[ -z $qf_vite_pid ]] || kill "$qf_vite_pid" >/dev/null 2>&1 || true
    docker logs "$qf_runner" > "$qf_evidence/service.log" 2>&1 || true
    docker stop -t 15 "$qf_runner" >/dev/null 2>&1 || true
    docker rm "$qf_runner" >/dev/null 2>&1 || true
    # Only fenced workers whose two immutable mounts are inside this fresh root.
    PYTHONPATH="$qf_root/backend" python3 - "$qf_test_root/work" <<'PY'
from pathlib import Path
import sys
from app.backtest_service.process import DockerDaemon, LABEL
from app.backtest_service.runtime_contracts import Claim
daemon= DockerDaemon(); root=Path(sys.argv[1])
for row in daemon.request('GET','/containers/json?all=1'):
    item=daemon.inspect(row['Id'])
    if item is None: continue
    labels=item['Config'].get('Labels') or {}; mounts=item.get('Mounts',[])
    if labels.get(LABEL)!='1' or len(mounts)!=2: continue
    if {m['Destination'] for m in mounts}!={'/run/qf/code','/run/qf/ipc'}: continue
    if not all(not m['RW'] and Path(m['Source']).is_relative_to(root) for m in mounts): continue
    daemon.stop_remove(item['Id'],Claim(labels[LABEL+'.run'],labels[LABEL+'.token'],int(labels[LABEL+'.fence'])))
PY
    docker exec --user postgres "$qf_database" rm -f -- /var/run/postgresql/.s.PGSQL.5432 /var/run/postgresql/.s.PGSQL.5432.lock >/dev/null 2>&1 || true
    docker rm -fv "$qf_database" >/dev/null 2>&1 || true
    docker image rm "$qf_runner_tag" "$qf_worker_tag" >/dev/null 2>&1 || true
    python3 - "$qf_test_root" <<'PY'
import os,pathlib,sys
for directory,_,_ in os.walk(sys.argv[1],followlinks=False):
    path=pathlib.Path(directory)
    if not path.is_symlink() and path.stat().st_uid==os.getuid():os.chmod(path,0o700)
PY
    rm -rf -- "$qf_test_root"
}
trap cleanup EXIT
UV_CACHE_DIR=${UV_CACHE_DIR:-$qf_build/uv-cache} uv export -q --project "$qf_root/backend" --locked --no-dev --no-emit-project --format requirements-txt -o "$qf_build/ui-deps/backend.lock"
for qf_lock in "$qf_build/ui-deps/backend.lock" "$qf_root/sdk/python/requirements-build.lock"; do
    "$qf_build/build/bin/python" -m pip download --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r "$qf_lock" -d "$qf_build/ui-deps/wheels" > "$qf_evidence/dependency-download.log" 2>&1
done
cp "$qf_build/ui-deps/backend.lock" "$qf_test_root/build/backend.lock"
cp -r "$qf_build/ui-deps/wheels" "$qf_test_root/build/dependencies"
python3 - "$qf_root" "$qf_build" "$qf_test_root/build" <<'PY'
import pathlib,shutil,sys
repo,build,target=map(pathlib.Path,sys.argv[1:])
for p in ['VERSION','backend/app','backend/alembic.ini','sdk/python/requirements-build.lock','sdk/python/requirements-runtime.lock',
          'sdk/python/tests/support_sdk.py','ops/s3-d12','ops/s3-d15/isolated_server.py','tests/s3_runtime_suite.py',
          'ops/s3-d16', 'tests/s3_runservice_suite.py','tests/s3_cli_suite.py','tests/s3_cli_fixtures.py','examples/strategies','examples/http_workflow.py','docs/backtesting']:
    dest=target/p;dest.parent.mkdir(parents=True,exist_ok=True)
    if (repo/p).is_dir():shutil.copytree(repo/p,dest,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    else:shutil.copyfile(repo/p,dest)
wheels=list((build/'wheels').glob('quantfoundry_sdk-*.whl'))
assert len(wheels)==1, 'Use one exact installed SDK wheel'
(target/'wheels').mkdir();shutil.copyfile(wheels[0],target/'wheels'/wheels[0].name)
# Reuse D12's trusted test image; append only D15's executable fixture.
dockerfile=(target/'ops/s3-d12/runner-test.Dockerfile').read_text()
(target/'runner.Dockerfile').write_text(dockerfile+'\nCOPY ops/s3-d16 /app/ops/s3-d16\nCOPY ops/s3-d15/isolated_server.py /app/ops/s3-d15/isolated_server.py\nRUN chmod -R a+rX /app/ops\nCMD ["python", "ops/s3-d15/isolated_server.py"]\n')
PY
docker build --network none -f "$qf_test_root/build/ops/s3-d12/worker.Dockerfile" -t "$qf_worker_tag" "$qf_test_root/build" > "$qf_evidence/worker-build.log" 2>&1
qf_worker_id=$(docker image inspect --format '{{.Id}}' "$qf_worker_tag")
docker build --network none -f "$qf_test_root/build/runner.Dockerfile" -t "$qf_runner_tag" "$qf_test_root/build" > "$qf_evidence/runner-build.log" 2>&1
qf_runner_id=$(docker image inspect --format '{{.Id}}' "$qf_runner_tag")
chmod 777 "$qf_test_root/db-socket"
docker run -d --name "$qf_database" --network none --cpus 1 --memory 512m --memory-swap 512m \
    --label "io.quantfoundry.s3.private-test-root=$qf_test_root/work" \
    --mount "type=bind,src=$qf_test_root/db-socket,dst=/var/run/postgresql" \
    --env POSTGRES_DB=qf_d15_test --env POSTGRES_PASSWORD=private-d15-test postgres:17 >/dev/null
qf_pg_ready=0
for qf_try in $(seq 1 60); do
    if docker exec "$qf_database" pg_isready -U postgres -d qf_d15_test >/dev/null 2>&1; then qf_pg_ready=1; break; fi
    sleep 1
done
[[ $qf_pg_ready == 1 ]]
# Only the trusted runner gets its test DB/socket. Strategy workers retain D12
# network=none, read-only root, caps/pids/CPU/memory budgets and two run mounts.
docker run -d --name "$qf_runner" --publish 127.0.0.1::8080 --cpus 1 --memory 1g --memory-swap 1g --pids-limit 64 \
    --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)" \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
    --mount "type=bind,src=$qf_test_root/work,dst=/qf-work" \
    --mount "type=bind,src=$qf_test_root/db-socket,dst=/qf-db,readonly" \
    --env "QF_TEST_DAEMON_ROOT=$qf_test_root/work" --env "QF_D15_WORKER_IMAGE=$qf_worker_id" \
    --env "QF_D16_UI=$qf_d16" --env QF_D15_ROOT=/qf-work --env QF_D15_BIND_PORT=8080 --env QF_D15_MANIFEST=/qf-work/d15-manifest.json \
    --env "LF_D01_ALLOW_OVERLAY_TEST=${LF_D01_ALLOW_OVERLAY_TEST:-0}" \
    --env QF_ENVIRONMENT=test --env QF_DATABASE_HOST=/qf-db --env QF_DATABASE_USER=postgres \
    --env QF_DATABASE_PASSWORD=private-d15-test --env QF_DATABASE_NAME=qf_d15_test \
    --env QF_API_TOKEN=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    --env QF_CURSOR_SIGNING_KEY=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "$qf_runner_id" >/dev/null
qf_service_ready=0
for qf_try in $(seq 1 120); do
    if [[ -f $qf_test_root/work/d15-manifest.json ]]; then qf_service_ready=1; break; fi
    [[ $(docker inspect --format '{{.State.Running}}' "$qf_runner") == true ]] || break
    sleep 1
done
[[ $qf_service_ready == 1 ]]
qf_backend=http://$(docker port "$qf_runner" 8080/tcp)
python3 - "$qf_test_root/work/d15-manifest.json" "$qf_backend" <<'PY'
import json,pathlib,sys
path=pathlib.Path(sys.argv[1]);data=json.loads(path.read_text());data['url']=sys.argv[2];path.write_text(json.dumps(data))
PY
qf_front_port=$(python3 - <<'PY'
import socket
with socket.socket() as s:s.bind(('127.0.0.1',0));print(s.getsockname()[1])
PY
)
QF_DEV_BACKEND_URL="$qf_backend" pnpm -C "$qf_root/frontend" dev --port "$qf_front_port" --strictPort > "$qf_evidence/vite.log" 2>&1 &
qf_vite_pid=$!
export FRONTEND_URL="http://127.0.0.1:$qf_front_port"
for qf_try in $(seq 1 60); do
    if curl --fail --silent "$FRONTEND_URL" >/dev/null; then break; fi
    sleep 1
done
QF_D15_NATIVE=0 QF_D15_EVIDENCE="$qf_evidence/interface-substitute" node "$qf_root/frontend/tests/s3-run-ui.e2e.cjs"
QF_D15_NATIVE=1 QF_D15_MANIFEST="$qf_test_root/work/d15-manifest.json" QF_D15_EVIDENCE="$qf_evidence/real-isolated-service" node "$qf_root/frontend/tests/s3-run-ui.e2e.cjs"

if [[ $qf_d16 == 1 ]]; then
    QF_D16_NATIVE=0 QF_D16_EVIDENCE="$qf_evidence/d16-interface-substitute" node "$qf_root/frontend/tests/s3-results-ui.e2e.cjs"
    QF_D16_NATIVE=1 QF_D16_MANIFEST="$qf_test_root/work/d15-manifest.json" QF_D16_EVIDENCE="$qf_evidence/d16-real-isolated-service" node "$qf_root/frontend/tests/s3-results-ui.e2e.cjs"
fi
