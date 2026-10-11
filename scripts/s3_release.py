"""One build's source/wheel evidence, not a deployment history or market copy."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sources(native=True):
    paths = {ROOT / "VERSION"}
    for base, extensions in (
        ("engine/crates", (".rs", ".toml")),
        ("sdk/python/quantfoundry", (".py", ".pyi")),
    ):
        paths.update(p for p in (ROOT / base).rglob("*") if p.suffix in extensions)
    paths.update((ROOT / "engine").glob("*.toml"))
    paths.add(ROOT / "engine/Cargo.lock")
    paths.add(ROOT / "sdk/python/pyproject.toml")
    paths.update((ROOT / "sdk/python").glob("requirements-*.lock"))
    if not native:
        for base in ("backend/app", "ops/s3-d18"):
            paths.update((ROOT / base).rglob("*.py"))
        paths.update((ROOT / "contracts").glob("*.json"))
        paths.update((ROOT / "ops/s3-d18").glob("*.Dockerfile"))
        paths.add(ROOT / "ops/s3-d18/compose.s3.yaml")
        paths.update(
            p
            for p in (ROOT / "frontend/src").rglob("*")
            if p.suffix in (".ts", ".tsx", ".css")
        )
        paths.update(
            ROOT / "frontend" / name for name in ("package.json", "pnpm-lock.yaml")
        )
        paths.add(ROOT / "backend/uv.lock")
        paths.add(ROOT / "backend/pyproject.toml")
        for base in ("ops/s3-d12",):
            paths.update(
                p
                for p in (ROOT / base).rglob("*")
                if p.suffix == ".py" or p.name.endswith(".Dockerfile")
            )
        for name in (
            "scripts/s3_release.py",
            "scripts/test_s3_runtime.sh",
            "scripts/build_s3_images.sh",
            "scripts/test_s3_images.py",
            "scripts/test_s3_startup.py",
            "tests/s3_final_suite.py",
            "sdk/python/tests/support_sdk.py",
            ".github/workflows/s3-final.yml",
        ):
            paths.add(ROOT / name)
    values = {
        str(p.relative_to(ROOT)): digest(p.read_bytes())
        for p in sorted(paths)
        if p.is_file() and not p.is_symlink()
    }
    return dict(
        files=values, sha256=digest(json.dumps(values, sort_keys=True).encode())
    )


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def verify_full_record(record, context):
    """Reuse only a complete, native, identical-source measurement, never smoke."""
    assert record.get("mode") == "full" and record.get("passed") is True
    assert record.get("full_B1_B4") is True
    assert not record.get("filesystem_probe_substitute")
    assert record["topology"] == "runner_container_to_sibling_workers"
    assert record["resource_budget"] == dict(cpu=4, memory_bytes=8 * 1024**3)
    assert record["source"]["measurement_source"] == context["measurement_source"]
    old, new = record["source"]["build"], context["build"]
    for key in ("native_source", "build_architecture", "python", "rust_compiler"):
        assert old[key] == new[key], "build/source differs from full measurement"
    assert (
        old["tracked_worktree_dirty"] is False
        and new["tracked_worktree_dirty"] is False
    )
    assert old["wheel"]["native_sha256"] == new["wheel"]["native_sha256"]
    expected = {
        ("B1", "1d"): (12600000, 12600000, 2520, 1),
        ("B2", "1m"): (12096000, 12096000, 60480, 1),
        ("B2", "60m"): (12096000, 201600, 1008, 1),
        ("B3-trade", "tick"): (10000000, 10000000, 10000000, 1),
        ("B3-quote", "tick"): (10000000, 10000000, 10000000, 1),
        ("B4", "1d"): (37800, 37800, 1260, 16),
    }
    assert len(record["cases"]) == len(expected)
    actual = {(c["load"]["name"], c["frequency"]) for c in record["cases"]}
    assert actual == set(expected)
    for case in record["cases"]:
        name = case["load"]["name"]
        rows, events, callbacks, runs = expected[name, case["frequency"]]
        assert case["passed"] is True and int(case["fixture"]["rows"]) == rows
        assert len(case["results"]) == runs
        for result in case["results"]:
            assert (
                int(result["source_rows"]) == rows
                and int(result["completed_events"]) == events
            )
            kind = "handle_tick" if name.startswith("B3") else "handle_data"
            assert (
                result["telemetry"]["python_callbacks"]["by_kind"][kind]["count"]
                == callbacks
            )
        if name.startswith("B3"):
            correction = case["hot_correction"]
            assert (
                correction["probe_status"] == "failed"
                and correction["probe_error"] == "DATA_CHANGED"
            )
            assert (
                correction["probe_python_callbacks"] == 100
                and correction["corrected_read_verified"] is True
            )
        if name == "B4":
            assert (
                len(case["first"]["states"]) == 16
                and case["first"]["peak_workers"] <= 2
            )
            assert (
                sum(s["status"] == "succeeded" for s in case["first"]["states"]) == 15
            )
            assert (
                len(case["retry"]["states"]) == 1
                and case["retry"]["states"][0]["status"] == "succeeded"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("snapshot", "seal", "context", "reuse"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    if args.operation == "snapshot":
        save(args.out, sources())
        return
    manifest = json.loads(args.manifest.read_text())
    if args.operation == "reuse":
        record = json.loads(args.record.read_text())
        verify_full_record(record, manifest)
        save(
            args.out,
            dict(
                reused=True,
                original_measurement_commit=record["source"]["build"][
                    "checkout_commit"
                ],
                original_measurement_run=record.get("execution", {}).get("ci_run_url"),
                verified_checkout_commit=manifest["build"]["checkout_commit"],
                measurement_source_sha256=manifest["measurement_source"]["sha256"],
                record_sha256=digest(args.record.read_bytes()),
                production_executed=False,
            ),
        )
        return
    if args.operation == "seal":
        assert manifest == sources(), "native/SDK source changed during build"
        with zipfile.ZipFile(args.wheel) as archive:
            binaries = [name for name in archive.namelist() if name.endswith(".so")]
            assert len(binaries) == 1
            native_sha = digest(archive.read(binaries[0]))
        save(
            args.out,
            dict(
                native_source=manifest,
                wheel=dict(
                    name=args.wheel.name,
                    sha256=digest(args.wheel.read_bytes()),
                    native_sha256=native_sha,
                ),
                build_architecture=platform.machine(),
                python=sys.version.split()[0],
                rust_compiler=subprocess.check_output(
                    ["rustc", "--version"], cwd=ROOT, text=True
                ).strip(),
                production_executed=False,
                checkout_commit=subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                tracked_worktree_dirty=bool(
                    subprocess.check_output(
                        ["git", "status", "--porcelain", "--untracked-files=no"],
                        cwd=ROOT,
                        text=True,
                    )
                ),
            ),
        )
    else:
        assert (
            manifest["native_source"] == sources()
        ), "wheel source differs from measurement source"
        assert (
            digest(args.wheel.read_bytes()) == manifest["wheel"]["sha256"]
        ), "wheel changed after build"
        save(args.out, dict(build=manifest, measurement_source=sources(native=False)))


if __name__ == "__main__":
    main()
