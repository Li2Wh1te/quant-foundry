import json
from pathlib import Path
import tempfile
import unittest

from scripts.release_version import VersionError, check_version_consistency, set_version


class ReleaseVersionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.version_file = self.root / "VERSION"
        self.pyproject_file = self.root / "pyproject.toml"
        self.package_json_file = self.root / "package.json"
        self.version_file.write_text("0.1.0\n", encoding="utf-8")
        self.pyproject_file.write_text(
            "[project]\nname = \"quant-foundry\"\nversion = \"0.1.0\"\n",
            encoding="utf-8",
        )
        self.package_json_file.write_text(
            json.dumps({"name": "quant-foundry-frontend", "version": "0.1.0"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_check_accepts_matching_version_and_tag(self) -> None:
        version = check_version_consistency(
            tag="v0.1.0",
            version_file=self.version_file,
            pyproject_file=self.pyproject_file,
            package_json_file=self.package_json_file,
        )

        self.assertEqual(version, "0.1.0")

    def test_check_rejects_a_mismatched_package_version(self) -> None:
        self.package_json_file.write_text(
            json.dumps({"name": "quant-foundry-frontend", "version": "0.1.1"}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(VersionError, "frontend/package.json"):
            check_version_consistency(
                version_file=self.version_file,
                pyproject_file=self.pyproject_file,
                package_json_file=self.package_json_file,
            )

    def test_set_updates_all_version_copies(self) -> None:
        version = set_version(
            "0.2.0",
            version_file=self.version_file,
            pyproject_file=self.pyproject_file,
            package_json_file=self.package_json_file,
        )

        self.assertEqual(version, "0.2.0")
        self.assertEqual(self.version_file.read_text(encoding="utf-8"), "0.2.0\n")
        self.assertIn('version = "0.2.0"', self.pyproject_file.read_text(encoding="utf-8"))
        package_json = json.loads(self.package_json_file.read_text(encoding="utf-8"))
        self.assertEqual(package_json["version"], "0.2.0")

    def test_set_rejects_prerelease_values_until_metadata_mapping_exists(self) -> None:
        with self.assertRaisesRegex(VersionError, "stable SemVer"):
            set_version(
                "0.2.0-rc.1",
                version_file=self.version_file,
                pyproject_file=self.pyproject_file,
                package_json_file=self.package_json_file,
            )


if __name__ == "__main__":
    unittest.main()
