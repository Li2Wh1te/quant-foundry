#!/usr/bin/env bash
# Private test resources only. No prune, production daemon or permission changes.
set -euo pipefail
qf_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
qf_build=${QF_S3_BUILD_ROOT:?Build and test the wheel first}
qf_test_root=$(mktemp -d "${TMPDIR:-/tmp}/qf-s3-d12.XXXXXX")
export DOCKER_CONFIG="$qf_test_root/docker-config"
mkdir -p "$DOCKER_CONFIG"
qf_suffix=${qf_test_root##*.}
qf_runner=qf-s3-d12-runner-${qf_suffix,,}
qf_worker_tag=qf-s3-d12-worker:${qf_suffix,,}
qf_runner_tag=qf-s3-d12-runner:${qf_suffix,,}
qf_worker_id=''
qf_runner_id=''
cleanup() {
    docker rm -f "$qf_runner" >/dev/null 2>&1 || true
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
    # A failed test can leave its own immutable code directory mode 0555.
    python3 - "$qf_test_root" <<'PY'
import os,pathlib,sys
root=pathlib.Path(sys.argv[1])
for directory,children,files in os.walk(root,followlinks=False):
    path=pathlib.Path(directory)
    if not path.is_symlink():os.chmod(path,0o700)
PY
    rm -rf -- "$qf_test_root"
}
trap cleanup EXIT
mkdir -p "$qf_test_root/build" "$qf_test_root/work" "$qf_build/runtime-deps"
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
for p in ['backend/app','backend/pyproject.toml','backend/uv.lock','sdk/python/requirements-build.lock','sdk/python/requirements-runtime.lock',
          'sdk/python/tests/support_sdk.py','ops/s3-d12','tests/s3_runtime_suite.py']:
    dest=target/p;dest.parent.mkdir(parents=True,exist_ok=True)
    if (repo/p).is_dir():shutil.copytree(repo/p,dest,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    else:shutil.copyfile(repo/p,dest)
shutil.copytree(build/'wheels',target/'wheels')
PY
docker build --network none -f "$qf_test_root/build/ops/s3-d12/worker.Dockerfile" -t "$qf_worker_tag" "$qf_test_root/build"
qf_worker_id=$(docker image inspect --format '{{.Id}}' "$qf_worker_tag")
docker build --network none -f "$qf_test_root/build/ops/s3-d12/runner-test.Dockerfile" -t "$qf_runner_tag" "$qf_test_root/build"
qf_runner_id=$(docker image inspect --format '{{.Id}}' "$qf_runner_tag")
# This root is a newly created test-only daemon path. The runner sees /qf-work,
# and its sibling workers bind daemon-host paths beneath this exact directory.
docker run --name "$qf_runner" --network none --cpus 1 --memory 1g --memory-swap 1g --pids-limit 64 \
    --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)" \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
    --mount "type=bind,src=$qf_test_root/work,dst=/qf-work" \
    --env "QF_TEST_DAEMON_ROOT=$qf_test_root/work" --env "QF_TEST_WORKER_IMAGE=$qf_worker_id" \
    --env "QF_TEST_RUNNER_NAME=$qf_runner" \
    "$qf_runner_id"
mkdir -p "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}"
cp "$qf_test_root/work/isolation-evidence.json" "${QF_S3_RUNTIME_EVIDENCE:-$qf_build/runtime-evidence}/"
