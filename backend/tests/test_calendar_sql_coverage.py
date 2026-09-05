"""Regression tests for SQL calendar index coverage intervals."""

from datetime import date
from unittest import TestCase
from uuid import UUID

from app.backtesting.data.calendar_sql import _SnapshotIndexCell, SqlCalendarAxisDataProvider


CALENDAR_ID = "SSE"


def _cell(day: date) -> _SnapshotIndexCell:
    return _SnapshotIndexCell(
        calendar_id=CALENDAR_ID,
        session_date=day,
        is_open=True,
        selected_fact_id=UUID("01234567-89ab-cdef-0123-456789abcdef"),
        fact_version=1,
        revision_watermark="revision",
    )


class CalendarSqlCoverageTestCase(TestCase):
    def test_consecutive_dates_have_no_gap(self) -> None:
        coverage = SqlCalendarAxisDataProvider._index_coverage(
            tuple(_cell(day) for day in (
                date(2026, 1, 1),
                date(2026, 1, 2),
                date(2026, 1, 3),
            )),
            (CALENDAR_ID,),
        )

        self.assertEqual(coverage["by_calendar"][CALENDAR_ID]["gaps"], [])
        self.assertEqual(
            coverage["common"],
            {
                "floor": "2026-01-01",
                "ceiling": "2026-01-04",
                "gaps": [],
                "segments": [("2026-01-01", "2026-01-04")],
            },
        )

    def test_single_missing_date_is_a_half_open_gap(self) -> None:
        coverage = SqlCalendarAxisDataProvider._index_coverage(
            tuple(_cell(day) for day in (
                date(2026, 1, 1),
                date(2026, 1, 2),
                date(2026, 1, 4),
            )),
            (CALENDAR_ID,),
        )

        expected_gap = ("2026-01-03", "2026-01-04")
        self.assertEqual(coverage["by_calendar"][CALENDAR_ID]["gaps"], [expected_gap])
        self.assertEqual(coverage["common"]["gaps"], [expected_gap])


if __name__ == "__main__":
    import unittest

    unittest.main()
