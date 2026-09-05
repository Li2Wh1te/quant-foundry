"""Import-order smoke tests for the instruments <-> backtesting edge.

The generic data contract references versioned policies through
``app.instruments.references.VersionedReference``, while instrument domain
objects build on ``app.backtesting.domain`` helpers.  Parent-package
initialization therefore crosses this boundary in both directions, and an
eagerly importing package would close a cycle that only fails for *some*
entry modules.  Every case here runs in a fresh interpreter so the import
order is genuinely independent.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

_ENTRY_POINTS = (
    # Instruments first: used to fail with "cannot import name
    # 'VersionedReference' from partially initialized module".
    "from app.instruments.domain import VersionedReference",
    "import app.instruments",
    "from app.instruments.references import VersionedReference",
    # Backtesting data first.
    "import app.backtesting.data.requests",
    "from app.backtesting.data.errors import freeze_json",
    "from app.backtesting.data.memory import MemoryDataProvider",
    "from app.backtesting.data.views import ChunkStrategyDataView",
    # Whole packages.
    "import app.backtesting",
    "import app.backtesting.data",
    "from app.backtesting.data import ConsistencyTokenScope",
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

    def test_lazy_package_exports_stay_resolvable(self) -> None:
        # PEP 562 lazy exports must behave like plain attributes for both
        # ``from package import name`` and attribute access.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import app.backtesting.data as d; "
                    "assert d.DATA_CONTRACT_VERSION == 1; "
                    "assert d.MAX_LOOKBACK_SESSIONS == 512; "
                    "assert 'CoverageEnvelope' in dir(d); "
                    "assert d.ConsistencyTokenScope.__name__ == 'ConsistencyTokenScope'; "
                    "print('ok')"
                ),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
