#!/usr/bin/env python3
"""Manage the repository's single canonical release version.

The VERSION file is the only value that maintainers edit directly. Package
metadata is a derived copy required by the Python and Node build tools. This
script updates those copies together and rejects a repository where they drift.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tempfile
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = PROJECT_ROOT / "VERSION"
PYPROJECT_FILE = PROJECT_ROOT / "backend" / "pyproject.toml"
PACKAGE_JSON_FILE = PROJECT_ROOT / "frontend" / "package.json"
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
)
PYPROJECT_VERSION_PATTERN = re.compile(
    r'^(version\s*=\s*)"[^"]*"\s*$', re.MULTILINE
)


class VersionError(ValueError):
    """Raised when release-version inputs or derived metadata are invalid."""


def validate_version(version: str) -> str:
    """Return a normalized stable SemVer version or raise a clear error.

    Initial release automation intentionally supports stable releases only.
    Adding prerelease syntax later requires an explicit mapping for Python's
    PEP 440 package metadata, instead of silently creating two version forms.
    """

    normalized = version.strip()
    if not SEMVER_PATTERN.fullmatch(normalized):
        raise VersionError(
            "version must use stable SemVer MAJOR.MINOR.PATCH, for example 0.1.0"
        )
    return normalized


def read_canonical_version(version_file: Path = VERSION_FILE) -> str:
    """Read exactly one canonical version value from VERSION."""

    lines = version_file.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise VersionError(f"{version_file} must contain exactly one version line")
    return validate_version(lines[0])


def read_package_versions(
    pyproject_file: Path = PYPROJECT_FILE,
    package_json_file: Path = PACKAGE_JSON_FILE,
) -> tuple[str | None, str | None]:
    """Read the version declarations required by the backend and frontend tools."""

    with pyproject_file.open("rb") as handle:
        pyproject = tomllib.load(handle)
    backend_version = pyproject.get("project", {}).get("version")

    package_json = json.loads(package_json_file.read_text(encoding="utf-8"))
    frontend_version = package_json.get("version")
    return backend_version, frontend_version


def check_version_consistency(
    *,
    tag: str | None = None,
    version_file: Path = VERSION_FILE,
    pyproject_file: Path = PYPROJECT_FILE,
    package_json_file: Path = PACKAGE_JSON_FILE,
) -> str:
    """Validate that every derived value and an optional release tag agree."""

    canonical_version = read_canonical_version(version_file)
    backend_version, frontend_version = read_package_versions(
        pyproject_file, package_json_file
    )
    errors: list[str] = []

    if backend_version != canonical_version:
        errors.append(
            f"backend/pyproject.toml version is {backend_version!r}; "
            f"expected {canonical_version!r} from VERSION"
        )
    if frontend_version != canonical_version:
        errors.append(
            f"frontend/package.json version is {frontend_version!r}; "
            f"expected {canonical_version!r} from VERSION"
        )
    if tag is not None and tag != f"v{canonical_version}":
        errors.append(
            f"release tag is {tag!r}; expected 'v{canonical_version}' from VERSION"
        )

    if errors:
        raise VersionError("\n".join(errors))
    return canonical_version


def set_version(
    version: str,
    *,
    version_file: Path = VERSION_FILE,
    pyproject_file: Path = PYPROJECT_FILE,
    package_json_file: Path = PACKAGE_JSON_FILE,
) -> str:
    """Set the canonical version and regenerate its two package metadata copies."""

    normalized = validate_version(version)
    pyproject_text = pyproject_file.read_text(encoding="utf-8")
    updated_pyproject, replacement_count = PYPROJECT_VERSION_PATTERN.subn(
        rf'\g<1>"{normalized}"', pyproject_text
    )
    if replacement_count != 1:
        raise VersionError(
            "expected exactly one top-level version declaration in backend/pyproject.toml"
        )

    package_json = json.loads(package_json_file.read_text(encoding="utf-8"))
    package_json["version"] = normalized
    updated_package_json = json.dumps(package_json, indent=2, ensure_ascii=False) + "\n"

    # Prepare every output before mutating a file so malformed metadata cannot
    # leave the repository with a partially updated logical version.
    updates = {
        version_file: f"{normalized}\n",
        pyproject_file: updated_pyproject,
        package_json_file: updated_package_json,
    }
    for path, content in updates.items():
        atomic_write(path, content)

    check_version_consistency(
        version_file=version_file,
        pyproject_file=pyproject_file,
        package_json_file=package_json_file,
    )
    return normalized


def atomic_write(path: Path, content: str) -> None:
    """Replace a text file atomically within its existing directory."""

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary_file:
        temporary_file.write(content)
        temporary_path = Path(temporary_file.name)
    temporary_path.replace(path)


def main() -> None:
    """Run the release-version command selected by the maintainer or CI."""

    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    set_parser = subcommands.add_parser("set", help="set VERSION and package metadata")
    set_parser.add_argument("version", help="stable SemVer value, for example 0.1.0")

    check_parser = subcommands.add_parser("check", help="validate version consistency")
    check_parser.add_argument(
        "--tag", help="optional release tag that must equal v<VERSION>"
    )

    arguments = parser.parse_args()
    try:
        if arguments.command == "set":
            version = set_version(arguments.version)
            print(f"Synchronized release version {version}")
        else:
            version = check_version_consistency(tag=arguments.tag)
            print(f"Release version {version} is consistent")
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError, VersionError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
