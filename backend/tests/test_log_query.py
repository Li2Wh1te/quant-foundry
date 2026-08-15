import gzip
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.logging.query import LocalLogQuery, LogFilters


class LocalLogQueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.temporary_directory.name)
        self.query = LocalLogQuery(self.log_dir, max_files=32)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_searches_current_and_compressed_rotated_files(self) -> None:
        old_entry = {
            "timestamp": "2026-08-13T09:00:00Z",
            "level": "info",
            "event": "request_completed",
            "method": "GET",
            "path": "/health",
            "status_code": 200,
        }
        error_entry = {
            "timestamp": "2026-08-14T10:00:00Z",
            "level": "warning",
            "event": "request_completed",
            "method": "POST",
            "path": "/api/orders",
            "status_code": 403,
        }
        with gzip.open(
            self.log_dir / "app.jsonl.2026-08-13.gz", "wt", encoding="utf-8"
        ) as stream:
            stream.write(json.dumps(old_entry) + "\n")
        (self.log_dir / "app.jsonl").write_text(
            json.dumps(error_entry) + "\n{incomplete",
            encoding="utf-8",
        )

        result = self.query.search(
            LogFilters(keyword="orders", method="POST", status_class="4xx"),
            limit=200,
        )

        self.assertEqual(result["items"], [error_entry])
        self.assertEqual(result["matched_count"], 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["scanned_files"], 2)
        self.assertEqual(result["facets"]["levels"], {"WARNING": 1})
        self.assertEqual(result["facets"]["methods"], {"POST": 1})
        self.assertEqual(result["facets"]["status_classes"], {"4xx": 1})

    def test_returns_latest_entries_up_to_limit(self) -> None:
        entries = [
            {
                "timestamp": f"2026-08-14T10:00:0{index}Z",
                "level": "info",
                "event": f"event_{index}",
            }
            for index in range(3)
        ]
        (self.log_dir / "app.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries),
            encoding="utf-8",
        )

        result = self.query.search(LogFilters(), limit=2)

        self.assertEqual(
            [entry["event"] for entry in result["items"]],
            ["event_2", "event_1"],
        )
        self.assertEqual(result["matched_count"], 3)
        self.assertTrue(result["truncated"])

    def test_clear_hides_existing_entries_without_truncating_active_file(self) -> None:
        log_file = self.log_dir / "app.jsonl"
        old_entry = {
            "timestamp": "2000-01-01T00:00:00Z",
            "level": "info",
            "event": "old",
        }
        log_file.write_text(json.dumps(old_entry) + "\n", encoding="utf-8")

        visible_after = self.query.clear_visible_logs()
        new_entry = {
            "timestamp": "2099-08-14T10:00:00Z",
            "level": "info",
            "event": "new",
        }
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(new_entry) + "\n")

        result = self.query.search(LogFilters(), limit=200)

        self.assertEqual(result["items"], [new_entry])
        self.assertGreaterEqual(visible_after, datetime(2026, 8, 14, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
