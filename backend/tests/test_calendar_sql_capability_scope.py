"""Regression checks for bounded canonical capability scope predicates."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.calendar_axis import CalendarPITContext, CalendarSnapshotRequest
from app.backtesting.data.calendar_sql import SqlCalendarAxisDataProvider
from app.backtesting.data.requests import QueryBoundary


class CalendarSqlCapabilityScopeTestCase(TestCase):
    def test_batch_capability_query_uses_only_frozen_canonical_scopes(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with Session(engine) as session:
            provider = SqlCalendarAxisDataProvider(session, provider_key="sql-calendar")
            captured = {}

            def capture(statement, *, limit, resource):
                captured[resource] = statement
                return []

            provider._bounded_rows = capture
            boundary = QueryBoundary(
                data_cutoff=datetime(2026, 1, 10, tzinfo=timezone.utc),
                include_cutoff_day=True,
            )
            request = CalendarSnapshotRequest(
                calendar_ids=("SSE", "SZSE"),
                formal_start=date(2026, 1, 2),
                formal_end=date(2026, 1, 2),
                warmup_sessions=0,
                query_boundary=boundary,
                instrument_ids=(
                    UUID("01234567-89ab-cdef-0123-456789abcdef"),
                    UUID("11234567-89ab-cdef-0123-456789abcdef"),
                ),
                provider_key="sql-calendar",
                package_key="china_listed_etf_rules",
                package_version=1,
            )
            plan = SimpleNamespace(
                envelope_start=request.formal_start,
                envelope_end_exclusive=date(2026, 1, 3),
                context=CalendarPITContext.from_query_boundary(boundary),
            )
            provider._load_batch(request, plan)

            statement = captured["capability"]
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            for scope_key in (
                "provider:sql-calendar",
                "rule_package:china_listed_etf_rules@1",
                "calendar:SSE",
                "calendar:SZSE",
                "instrument:01234567-89ab-cdef-0123-456789abcdef",
                "instrument:11234567-89ab-cdef-0123-456789abcdef",
            ):
                self.assertIn(scope_key, sql)
            self.assertIn("scope_kind", sql)
            self.assertIn("scope_key", sql)


if __name__ == "__main__":
    import unittest

    unittest.main()
