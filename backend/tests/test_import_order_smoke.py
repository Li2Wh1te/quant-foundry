"""Fresh-process imports for shared identity/calendar, API and new runner."""
from __future__ import annotations

import subprocess
import sys
import unittest

_ENTRY_POINTS = (
    "from app.instruments.domain import VersionedReference",
    "import app.instruments",
    "from app.instruments.references import VersionedReference",
    "from app.calendars.contracts import QueryBoundary",
    "from app.data_contracts.errors import freeze_json",
    "import app.calendars.sql",
    "import app.main",
    "import app.models",
    "import app.runner.__main__",
)


class TestImportOrderIndependence(unittest.TestCase):
    def test_every_entry_point_imports_cleanly(self) -> None:
        for statement in _ENTRY_POINTS:
            with self.subTest(entry=statement):
                result = subprocess.run(
                    [sys.executable, "-c", statement],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{statement!r} failed:\n{result.stderr}",
                )



if __name__ == "__main__":
    unittest.main()
