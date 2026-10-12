#!/usr/bin/env bash
# Build only; never starts services, changes permissions or publishes images.
set -euo pipefail
qf_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
qf_build=${QF_S3_BUILD_ROOT:?Provide the tested wheel build directory}
qf_output=${QF_S3_RELEASE_ROOT:?Provide a private release output directory}
mkdir -p "$qf_output" "$qf_build/runtime-deps"
qf_context=$(mktemp -d "${TMPDIR:-/tmp}/qf-s3-release.XXXXXX")
trap 'rm -rf -- "$qf_context"' EXIT
qf_arch=$(uname -m)
case "$qf_arch" in x86_64|aarch64) ;; *) exit 2 ;; esac
# Hash checks happen before the image build, not on production at startup.
python3 "$qf_root/scripts/s3_release.py" context --manifest "$qf_build/release.json" \
    --wheel "$qf_build"/wheels/quantfoundry_sdk-*.whl --out "$qf_context/s3-source.json"
UV_CACHE_DIR=${UV_CACHE_DIR:-$qf_build/uv-cache} uv export -q --project "$qf_root/backend" --locked --no-dev --no-emit-project \
    --format requirements-txt -o "$qf_build/runtime-deps/backend.lock"
"$qf_build/build/bin/python" -m pip download --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
    -r "$qf_build/runtime-deps/backend.lock" -d "$qf_build/runtime-deps/wheels"
"$qf_build/build/bin/python" -m pip download --index-url https://pypi.org/simple --require-hashes --only-binary=:all: \
    -r "$qf_root/sdk/python/requirements-runtime.lock" -d "$qf_build/runtime-deps/wheels"
python3 - "$qf_root" "$qf_build" "$qf_context" <<'PY'
from pathlib import Path
import shutil,sys
root,build,target=map(Path,sys.argv[1:])
for name in ('VERSION','backend/app','backend/alembic.ini','sdk/python/requirements-runtime.lock',
             'ops/s3-d18/release.Dockerfile','ops/s3-d12/worker.Dockerfile'):
    source=root/name;dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True)
    if source.is_dir():shutil.copytree(source,dest,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    else:shutil.copyfile(source,dest)
shutil.copytree(build/'runtime-deps/wheels',target/'dependencies')
shutil.copyfile(build/'runtime-deps/backend.lock',target/'backend.lock')
(target/'wheels').mkdir()
wheels=list((build/'wheels').glob('quantfoundry_sdk-*.whl'))
assert len(wheels)==1,'exactly one native release wheel is required'
shutil.copyfile(wheels[0],target/'wheels'/wheels[0].name)
PY
qf_source=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["native_source"]["sha256"][:16])' "$qf_context/s3-source.json")
qf_backend_tag=quant-foundry-s3-backend:${qf_source}-${qf_arch}
qf_worker_tag=quant-foundry-s3-worker:${qf_source}-${qf_arch}
docker build --network none -f "$qf_context/ops/s3-d18/release.Dockerfile" -t "$qf_backend_tag" "$qf_context"
docker build --network none -f "$qf_context/ops/s3-d12/worker.Dockerfile" -t "$qf_worker_tag" "$qf_context"
python3 - "$qf_context/s3-source.json" "$qf_backend_tag" "$qf_worker_tag" "$qf_output/images.json" <<'PY'
import json,subprocess,sys
from pathlib import Path
source,backend,worker,out=sys.argv[1:]
images={kind:json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]
    for kind,tag in [('backend',backend),('worker',worker)]}
assert all(x['Os']=='linux' and x['Config']['User']=='65532:65532' for x in images.values())
assert images['backend']['Architecture']==images['worker']['Architecture']
Path(out).write_text(json.dumps(dict(source=json.loads(Path(source).read_text()),
    architecture=images['backend']['Architecture'],
    images={k:dict(id=x['Id'],tag=t) for k,t,x in [('backend',backend,images['backend']),('worker',worker,images['worker'])]},
    production_executed=False),indent=2)+'\n')
print(out)
PY
