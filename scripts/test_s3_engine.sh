#!/usr/bin/env bash
# Run from any directory; no backend dependency updates or production activation.
set -euo pipefail
qf_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
qf_python=${QF_S3_PYTHON:-python3}
qf_python=$("$qf_python" -c 'import sys; print(sys.executable)')
export PYO3_PYTHON="$qf_python"
qf_build_root=${QF_S3_BUILD_ROOT:-$(mktemp -d "${TMPDIR:-/tmp}/qf-s3-d01.XXXXXX")}
if [[ -z ${QF_S3_BUILD_ROOT:-} ]]; then
    trap 'rm -rf -- "$qf_build_root"' EXIT
fi
mkdir -p "$qf_build_root/wheels"
cd "$qf_root/engine"
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo run --locked -p qf-core --example public_contract
cd "$qf_root"
"$qf_python" scripts/generate_s3_contracts.py --check
"$qf_python" scripts/release_version.py check
"$qf_python" -m unittest discover -s tests -v
"$qf_python" -m venv "$qf_build_root/build"
"$qf_build_root/build/bin/python" -m pip install --index-url https://pypi.org/simple --require-hashes -r sdk/python/requirements-build.lock
cd "$qf_root/sdk/python"
"$qf_build_root/build/bin/maturin" build --release --locked --interpreter "$qf_build_root/build/bin/python" --out "$qf_build_root/wheels"
"$qf_python" -m venv "$qf_build_root/consumer"
"$qf_build_root/consumer/bin/python" -m pip install --force-reinstall --no-deps "$qf_build_root"/wheels/quantfoundry_sdk-*.whl
"$qf_build_root/consumer/bin/python" -m pip install --index-url https://pypi.org/simple --require-hashes -r requirements-build.lock
cd "$qf_root"
"$qf_build_root/consumer/bin/python" -I -m unittest discover -s sdk/python/tests -v
