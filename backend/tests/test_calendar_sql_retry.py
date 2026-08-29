"""Regression checks for SQL calendar snapshot retry boundaries."""

from datetime import date, datetime, timezone
from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.backtesting.calendar_axis import CalendarSnapshotRequest
from app.backtesting.data.calendar_sql import SqlCalendarAxisDataProvider
from app.backtesting.data.errors import (
    CalendarSnapshotRevisionChangedError,
    ProviderContractViolationError,
)
from app.backtesting.data.requests import QueryBoundary


class CalendarSqlSnapshotRetryTestCase(TestCase):
    def test_non_transient_dbapi_error_is_not_retried(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        boundary = QueryBoundary(
            data_cutoff=datetime(2026, 1, 2, tzinfo=timezone.utc),
            include_cutoff_day=True,
        )
        request = CalendarSnapshotRequest(
            calendar_ids=("SSE",),
            formal_start=date(2026, 1, 1),
            formal_end=date(2026, 1, 1),
            warmup_sessions=0,
            query_boundary=boundary,
        )
        failure = DBAPIError.instance(
            "SELECT broken",
            None,
            RuntimeError("syntax error"),
            Exception,
            dialect=engine.dialect,
        )
        with Session(engine) as session:
            provider = SqlCalendarAxisDataProvider(session)
            prepare_calls = 0
            load_calls = 0

            def prepare(_request: CalendarSnapshotRequest) -> object:
                nonlocal prepare_calls
                prepare_calls += 1
                raise failure

            def load(_plan: object) -> object:
                nonlocal load_calls
                load_calls += 1
                return object()

            provider.prepare_calendar_snapshot = prepare
            provider.load_calendar_snapshot = load

            with self.assertRaises(ProviderContractViolationError) as caught:
                provider.open_calendar_snapshot(request)

        self.assertEqual(caught.exception.code, "provider_contract_violation")
        self.assertEqual(prepare_calls, 1)
        self.assertEqual(load_calls, 0)

    def test_revision_mismatch_is_not_reprepared_or_reloaded(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        boundary = QueryBoundary(
            data_cutoff=datetime(2026, 1, 2, tzinfo=timezone.utc),
            include_cutoff_day=True,
        )
        request = CalendarSnapshotRequest(
            calendar_ids=("SSE",),
            formal_start=date(2026, 1, 1),
            formal_end=date(2026, 1, 1),
            warmup_sessions=0,
            query_boundary=boundary,
        )
        with Session(engine) as session:
            provider = SqlCalendarAxisDataProvider(session)
            plan = object()
            prepare_calls = 0
            load_calls = 0
            failure = CalendarSnapshotRevisionChangedError(
                "batch read selected fact differs from prepare"
            )

            def prepare(_request: CalendarSnapshotRequest) -> object:
                nonlocal prepare_calls
                prepare_calls += 1
                return plan

            def load(_plan: object) -> object:
                nonlocal load_calls
                load_calls += 1
                raise failure

            provider.prepare_calendar_snapshot = prepare
            provider.load_calendar_snapshot = load

            with self.assertRaises(CalendarSnapshotRevisionChangedError) as caught:
                provider.open_calendar_snapshot(request)

        self.assertIs(caught.exception, failure)
        self.assertEqual(prepare_calls, 1)
        self.assertEqual(load_calls, 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
