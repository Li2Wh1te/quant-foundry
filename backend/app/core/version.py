"""Read the canonical project version packaged with the running application."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re


VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class VersionError(RuntimeError):
    """Raised when the packaged canonical version is unavailable or invalid."""


def _find_version_file(source_file: Path) -> Path:
    """Find VERSION above this module in source and production image layouts.

    Source execution keeps VERSION at the repository root, while the production
    image copies it to /app. Walking upward supports both layouts without an
    environment override that could make frontend and backend drift.
    """

    for directory in source_file.resolve().parents:
        candidate = directory / "VERSION"
        if candidate.is_file():
            return candidate
    raise VersionError("canonical VERSION file is missing from the application")


def read_release_version(version_file: Path) -> str:
    """Return the stable SemVer value from one canonical VERSION file."""

    lines = version_file.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or not VERSION_PATTERN.fullmatch(lines[0]):
        raise VersionError(
            f"{version_file} must contain one stable SemVer version such as 0.1.0"
        )
    return lines[0]


@lru_cache
def get_release_version() -> str:
    """Return the immutable version embedded in this backend build."""

    return read_release_version(_find_version_file(Path(__file__)))
