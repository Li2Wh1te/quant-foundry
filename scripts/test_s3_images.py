"""Actual immutable release-image installation/import checks; no service start."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("manifest", type=Path)
args = parser.parse_args()
manifest = json.loads(args.manifest.read_text())
native_sha = manifest["source"]["build"]["wheel"]["native_sha256"]
samples = {}
for kind, image in manifest["images"].items():
    code = """import hashlib,importlib.util,json,platform
from pathlib import Path
from quantfoundry import _native,__version__
assert importlib.util.find_spec('app.backtesting') is None if KIND=='backend' else importlib.util.find_spec('app') is None
if KIND=='backend':
    import app.main,app.runner.__main__,app.strategies.router
    assert app.main.app.version==__version__
assert hashlib.sha256(Path(_native.__file__).read_bytes()).hexdigest()==NATIVE_SHA
print(json.dumps(dict(architecture=platform.machine(),version=__version__,capabilities=json.loads(_native.capabilities_json()))))
""".replace("KIND", repr(kind)).replace("NATIVE_SHA", repr(native_sha))
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "1",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "32",
        "--entrypoint",
        "python",
    ]
    for key, value in dict(
        QF_ENVIRONMENT="test",
        QF_API_TOKEN="a" * 64,
        QF_CURSOR_SIGNING_KEY="b" * 64,
        QF_DATABASE_PASSWORD="private-image-test",
        QF_DATABASE_NAME="qf_release_test",
    ).items():
        command += ["--env", key + "=" + value]
    result = subprocess.run(
        command + [image["id"], *(["-I"] if kind == "worker" else []), "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(
            kind + " release image installation check failed: " + result.stderr[-4000:]
        )
    samples[kind] = json.loads(result.stdout)
manifest["installation_smoke"] = samples
manifest["images_installed_verified"] = True
args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps(dict(verified=True, images=manifest["images"], samples=samples)))
