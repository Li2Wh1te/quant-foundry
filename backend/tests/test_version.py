from pathlib import Path
import tempfile
import unittest

from app.core.version import VersionError, read_release_version


class VersionTestCase(unittest.TestCase):
    def test_reads_stable_semver_from_canonical_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            version_file = Path(temporary_directory) / "VERSION"
            version_file.write_text("0.1.0\n", encoding="utf-8")

            self.assertEqual(read_release_version(version_file), "0.1.0")

    def test_rejects_noncanonical_version_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            version_file = Path(temporary_directory) / "VERSION"
            version_file.write_text("v0.1.0\n", encoding="utf-8")

            with self.assertRaisesRegex(VersionError, "stable SemVer"):
                read_release_version(version_file)
