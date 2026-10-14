#!/usr/bin/env bash
# Build a paired market release only. No start/stop, deploy, push, or data writes.
set -euo pipefail
qf_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
qf_build=${QF_S2_BUILD_ROOT:?Provide the sealed native wheel build directory}
qf_output=${QF_S2_RELEASE_ROOT:?Provide a private release output directory}
qf_python=${QF_S2_BUILD_PYTHON:?Provide the locked build-environment Python}
qf_context=$(mktemp -d "${TMPDIR:-/tmp}/qf-s2-build.XXXXXX")
trap 'rm -rf -- "$qf_context"' EXIT
mkdir -p "$qf_output" "$qf_build/runtime-deps/wheels"
export DOCKER_CONFIG=${QF_S2_DOCKER_CONFIG:-$qf_build/docker-config}
mkdir -p "$DOCKER_CONFIG"
python3 "$qf_root/scripts/s3_release.py" context --manifest "$qf_build/release.json" \
  --wheel "$qf_build"/wheels/quantfoundry_sdk-*.whl --out "$qf_context/s3-source.json"
uv export -q --project "$qf_root/backend" --locked --no-dev --no-emit-project \
  --format requirements-txt -o "$qf_build/runtime-deps/backend.lock"
"$qf_python" -m pip download --require-hashes --only-binary=:all: \
  -r "$qf_build/runtime-deps/backend.lock" -d "$qf_build/runtime-deps/wheels"
"$qf_python" -m pip download --require-hashes --only-binary=:all: \
  -r "$qf_root/sdk/python/requirements-runtime.lock" -d "$qf_build/runtime-deps/wheels"
python3 - "$qf_root" "$qf_build" "$qf_context" <<'PY'
from pathlib import Path
import shutil,sys
repo,build,context=map(Path,sys.argv[1:])
for name in ('VERSION','backend/app','backend/alembic.ini','sdk/python/requirements-runtime.lock','ops/s3-d18/release.Dockerfile'):
    source=repo/name; target=context/name; target.parent.mkdir(parents=True,exist_ok=True)
    if source.is_dir(): shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    else: shutil.copy2(source,target)
shutil.copytree(build/'runtime-deps/wheels',context/'dependencies')
shutil.copy2(build/'runtime-deps/backend.lock',context/'backend.lock')
(context/'wheels').mkdir()
wheels=list((build/'wheels').glob('quantfoundry_sdk-*.whl'))
assert len(wheels)==1
shutil.copy2(wheels[0],context/'wheels'/wheels[0].name)
PY
qf_suffix=$(git -C "$qf_root" rev-parse --short=12 HEAD)-$(uname -m)
qf_api=quant-foundry-s2-api:$qf_suffix
qf_ui=quant-foundry-s2-web:$qf_suffix
# The existing S3 API recipe installs the tested SDK and locked dependencies
# offline. It creates no worker and changes no worker/admission configuration.
docker build --network none -f "$qf_context/ops/s3-d18/release.Dockerfile" -t "$qf_api" "$qf_context"
# Build the frozen frontend on the build host. The runtime image contains only
# static assets; it does not need registry access or package managers.
python3 - "$qf_root" "$qf_context" <<'PY'
from pathlib import Path
import shutil,sys
repo,context=map(Path,sys.argv[1:])
shutil.copytree(repo/'frontend',context/'frontend',ignore=shutil.ignore_patterns('node_modules','dist','*.tsbuildinfo'))
target=context/'examples/strategies/etf_rotation.py'; target.parent.mkdir(parents=True)
shutil.copy2(repo/'examples/strategies/etf_rotation.py',target)
shutil.copy2(repo/'ops/s2-batch2/frontend.release.Dockerfile',context/'frontend.release.Dockerfile')
PY
pnpm --dir "$qf_context/frontend" install --frozen-lockfile ${QF_S2_PNPM_OFFLINE:+--offline}
pnpm --dir "$qf_context/frontend" build
docker build --network none -f "$qf_context/frontend.release.Dockerfile" -t "$qf_ui" "$qf_context"
python3 - "$qf_context/s3-source.json" "$qf_api" "$qf_ui" "$qf_output/images.json" <<'PY'
from pathlib import Path
import json,subprocess,sys
source,api,web,out=sys.argv[1:]
images={kind:json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]
        for kind,tag in [('backend',api),('frontend',web)]}
assert all(value['Os']=='linux' for value in images.values())
assert images['backend']['Architecture']==images['frontend']['Architecture']
assert images['backend']['Config']['User']=='65532:65532'
Path(out).write_text(json.dumps(dict(source=json.loads(Path(source).read_text()),
    architecture=images['backend']['Architecture'],
    images={key:dict(id=value['Id'],tag=tag) for key,tag,value in
        [('backend',api,images['backend']),('frontend',web,images['frontend'])]},
    production_executed=False),indent=2)+'\n')
print(out)
PY
