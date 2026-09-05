"""Tests for the read-only backtest result HTTP layer.

The project test suite calls route functions directly (no TestClient), so
these tests exercise the same functions FastAPI dispatches to and then
serialize the response models with ``model_dump(mode="json")`` to pin the
wire contract (Decimal as string, opaque cursor envelope).
"""

import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.accounting import OrderSide
from app.backtesting.domain import PositionSide
from app.backtesting.pagination import (
    CursorQueryMismatchError,
    compute_query_digest,
    encode_sort_element,
    parse_cursor,
)
from app.backtesting.result_models import (
    BacktestDataPreflightRecord,
    BacktestMetricRecord,
    BacktestOrderRecord,
    BacktestPositionRecord,
    DataPhase,
    InstrumentDisplaySnapshot,
    ResultOrderStatus,
)
from app.backtesting.result_records import RESULT_TABLE_NAMES, Base
from app.backtesting.result_repository import BacktestResultRepository
from app.backtesting.result_repository import build_query_payload, get_result_kind_spec
from app.backtesting.result_router import (
    legacy_data_preflight_method_not_allowed,
    legacy_data_preflight_redirect,
    list_data_preflight,
    list_metrics,
    list_orders,
    list_positions,
)
from app.backtesting.result_schemas import ResultCursorPage, BacktestOrderItem


UTC = timezone.utc
SIGNING_KEY = "unit-test-signing-key"


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


class ResultApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        result_tables = [Base.metadata.tables[name] for name in RESULT_TABLE_NAMES]
        Base.metadata.create_all(self.engine, tables=result_tables)
        self.session = Session(self.engine)
        self.repo = BacktestResultRepository(
            self.session, cursor_signing_key=SIGNING_KEY
        )
        self.run_id = uuid4()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def seed_orders(self, count: int) -> list[BacktestOrderRecord]:
        orders = []
        for index in range(count):
            instrument_id = uuid4()
            orders.append(
                BacktestOrderRecord(
                    run_id=self.run_id,
                    order_id=uuid4(),
                    instrument_id=instrument_id,
                    display=InstrumentDisplaySnapshot(
                        instrument_id=instrument_id,
                        event_trading_code=None,
                        event_name="虚构资产",
                    ),
                    side=OrderSide.BUY,
                    order_type="market",
                    quantity="100",
                    status=ResultOrderStatus.FILLED,
                    submitted_at=ts(9) + timedelta(minutes=index),
                    # Binary-float-exact so the SQLite test dialect round-trips
                    # it losslessly; PostgreSQL NUMERIC stays exact regardless.
                    price=Decimal("3.5"),
                )
            )
        self.repo.append("orders", *orders)
        return orders

    def serialize_orders_page(self, **kwargs) -> dict:
        page = list_orders(run_id=self.run_id, limit=10, cursor=None, session=self.session,
            signing_key=SIGNING_KEY, **kwargs)
        model = ResultCursorPage[BacktestOrderItem].model_validate(page)
        return model.model_dump(mode="json")

    def test_orders_envelope_and_decimal_string_serialization(self) -> None:
        orders = self.seed_orders(3)
        payload = self.serialize_orders_page()
        self.assertEqual(len(payload["items"]), 3)
        self.assertFalse(payload["has_more"])
        self.assertIsNone(payload["next_cursor"])
        item = payload["items"][0]
        self.assertEqual(item["order_id"], str(orders[0].order_id))
        # Decimal fields must be strings on the wire, never JSON numbers.
        self.assertEqual(item["price"], "3.5")
        self.assertEqual(item["quantity"], "100")
        self.assertIsInstance(item["price"], str)

    def test_orders_multi_page_cursor_flow(self) -> None:
        orders = self.seed_orders(12)
        first = list_orders(run_id=self.run_id, limit=5, cursor=None, session=self.session,
            signing_key=SIGNING_KEY)
        self.assertTrue(first["has_more"])
        second = list_orders(
            run_id=self.run_id,
            limit=5,
            cursor=first["next_cursor"],
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        third = list_orders(
            run_id=self.run_id,
            limit=5,
            cursor=second["next_cursor"],
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        collected = [row.order_id for row in (*first["items"], *second["items"], *third["items"])]
        self.assertEqual(collected, [order.order_id for order in orders])
        self.assertFalse(third["has_more"])
        self.assertIsNone(third["next_cursor"])

    def test_invalid_limit_maps_to_validation_error(self) -> None:
        from fastapi import HTTPException

        for bad in (0, 501):
            with self.assertRaises(HTTPException) as caught:
                list_orders(run_id=self.run_id, limit=bad, cursor=None, session=self.session,
            signing_key=SIGNING_KEY)
            self.assertEqual(caught.exception.status_code, 422)

    def test_mismatched_cursor_maps_to_400(self) -> None:
        from fastapi import HTTPException

        self.seed_orders(12)
        page = list_orders(run_id=self.run_id, limit=10, cursor=None, session=self.session,
            signing_key=SIGNING_KEY)
        with self.assertRaises(HTTPException) as caught:
            list_orders(
                run_id=self.run_id,
                limit=5,
                cursor=page["next_cursor"],
                session=self.session,
                signing_key=SIGNING_KEY,
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_naive_time_bound_maps_to_422(self) -> None:
        from fastapi import HTTPException

        self.seed_orders(1)
        with self.assertRaises(HTTPException) as caught:
            list_orders(
                run_id=self.run_id,
                limit=10,
                cursor=None,
                session=self.session,
            signing_key=SIGNING_KEY,
                start_time=datetime(2026, 6, 1, 9, 0),
            )
        self.assertEqual(caught.exception.status_code, 422)

    def test_positions_return_raw_snapshots_with_identity_only_filter(self) -> None:
        instrument = uuid4()
        rows = [
            BacktestPositionRecord(
                run_id=self.run_id,
                as_of=ts(hour),
                instrument_id=instrument,
                display=InstrumentDisplaySnapshot(instrument_id=instrument),
                side=PositionSide.LONG,
                quantity=str(quantity),
                available_quantity=str(quantity),
                average_price="2",
                realized_pnl="0",
                unrealized_pnl="1",
            )
            for hour, quantity in ((15, "10"), (16, "8"))
        ]
        self.repo.append("positions", *rows)
        page = list_positions(
            run_id=self.run_id,
            limit=10,
            cursor=None,
            session=self.session,
            signing_key=SIGNING_KEY,
            instrument_id=instrument,
        )
        self.assertEqual(len(page["items"]), 2)
        quantities = {row.quantity for row in page["items"]}
        self.assertEqual(quantities, {Decimal("10"), Decimal("8")})

    def test_preflight_calendar_details_page_inside_one_report(self) -> None:
        differences = [
            {"date": f"2026-01-{(index % 28) + 1:02d}", "field": "is_open", "index": index}
            for index in range(101)
        ]
        self.repo.append(
            "data_preflight",
            BacktestDataPreflightRecord(
                run_id=self.run_id,
                phase=DataPhase.SESSION,
                status="blocked",
                report_hash="detail-hash",
                hash_schema_version=2,
                calendar_summary={"differences": differences},
            ),
        )
        first = list_data_preflight(
            run_id=self.run_id,
            limit=100,
            cursor=None,
            section="calendar",
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        self.assertEqual(len(first["items"]), 100)
        self.assertTrue(first["truncated"])
        self.assertEqual(
            [item["calendar_summary"]["detail"]["value"]["index"] for item in first["items"]],
            list(range(100)),
        )
        second = list_data_preflight(
            run_id=self.run_id,
            limit=100,
            cursor=first["next_cursor"],
            section="calendar",
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["calendar_summary"]["detail"]["value"]["index"], 100)
        self.assertFalse(second["has_more"])

    def test_preflight_projection_keeps_calendar_and_session_summaries_separate(self) -> None:
        pit_context = {
            "data_cutoff": "2026-01-02T00:00:00+00:00",
            "cutoff_local_date": "2026-01-02",
            "include_cutoff_day": False,
            "knowledge_as_of": None,
            "pit_profile": "strict_calendar_cutoff",
            "profile_version": "calendar_pit_profile@1:H",
        }
        self.repo.append(
            "data_preflight",
            BacktestDataPreflightRecord(
                run_id=self.run_id,
                phase=DataPhase.SESSION,
                status="ready",
                report_hash="separate-summary",
                calendar_summary={},
                session_summary={
                    "pit_context": pit_context,
                    "formal_sessions": [{"date": "2026-01-02"}],
                },
            ),
        )
        page = list_data_preflight(
            run_id=self.run_id,
            limit=10,
            cursor=None,
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        item = page["items"][0]
        self.assertNotIn("formal_sessions", item["calendar_summary"])
        self.assertEqual(item["session_summary"]["formal_sessions"], [{"date": "2026-01-02"}])
        self.assertEqual(item["data_cutoff"], pit_context["data_cutoff"])
        self.assertEqual(item["cutoff_local_date"], pit_context["cutoff_local_date"])
        self.assertEqual(item["pit_profile"], pit_context["pit_profile"])
        self.assertEqual(item["profile_version"], pit_context["profile_version"])

    def test_preflight_calendar_section_paginates_calendar_ids(self) -> None:
        self.repo.append(
            "data_preflight",
            BacktestDataPreflightRecord(
                run_id=self.run_id,
                phase=DataPhase.SESSION,
                status="ready",
                report_hash="calendar-ids",
                calendar_summary={"calendar_ids": ["SSE", "SZSE"]},
            ),
        )
        first = list_data_preflight(
            run_id=self.run_id,
            limit=1,
            cursor=None,
            section="calendar",
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        self.assertEqual(first["items"][0]["calendar_summary"]["detail"]["kind"], "calendar_id")
        self.assertTrue(first["has_more"])
        second = list_data_preflight(
            run_id=self.run_id,
            limit=1,
            cursor=first["next_cursor"],
            section="calendar",
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        self.assertEqual(second["items"][0]["calendar_summary"]["detail"]["value"], "SZSE")
        self.assertFalse(second["has_more"])

    def test_preflight_cursor_uses_canonical_resource_identifier(self) -> None:
        for phase in (DataPhase.ADMISSION, DataPhase.SESSION):
            self.repo.append(
                "data_preflight",
                BacktestDataPreflightRecord(
                    run_id=self.run_id,
                    phase=phase,
                    status="ready",
                    report_hash=f"cursor-{phase.value}",
                ),
            )
        page = list_data_preflight(
            run_id=self.run_id,
            limit=1,
            cursor=None,
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        parsed = parse_cursor(
            page["next_cursor"],
            signing_key=SIGNING_KEY,
            key_kinds=("str",),
            upper_bound_columns={"phase": "str"},
        )
        spec = get_result_kind_spec("data_preflight")
        payload = build_query_payload(
            spec,
            run_id=self.run_id,
            limit=1,
            filters={},
        )
        bound = {"phase": encode_sort_element("str", "session")}
        self.assertEqual(
            parsed.query_digest,
            compute_query_digest({**payload, "query_upper_bound": bound}),
        )

    def test_preflight_section_is_bound_into_signed_cursor(self) -> None:
        from fastapi import HTTPException

        for phase in (DataPhase.ADMISSION, DataPhase.SESSION):
            self.repo.append(
                "data_preflight",
                BacktestDataPreflightRecord(
                    run_id=self.run_id,
                    phase=phase,
                    status="ready",
                    report_hash=f"hash-{phase.value}",
                    hash_schema_version=2,
                    calendar_summary={"calendar_ids": ["SSE"]},
                    session_summary={"formal_session_count": 1},
                ),
            )
        first = list_data_preflight(
            run_id=self.run_id,
            limit=1,
            cursor=None,
            section="calendar",
            session=self.session,
            signing_key=SIGNING_KEY,
        )
        self.assertTrue(first["has_more"])
        self.assertIsNotNone(first["next_cursor"])
        with self.assertRaises(HTTPException) as caught:
            list_data_preflight(
                run_id=self.run_id,
                limit=1,
                cursor=first["next_cursor"],
                section="sessions",
                session=self.session,
                signing_key=SIGNING_KEY,
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_preflight_section_rejects_non_positive_limit(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            list_data_preflight(
                run_id=self.run_id,
                limit=0,
                cursor=None,
                section="calendar",
                session=self.session,
                signing_key=SIGNING_KEY,
            )
        self.assertEqual(caught.exception.status_code, 422)

    def test_legacy_preflight_non_get_is_always_method_not_allowed(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            legacy_data_preflight_method_not_allowed()
        self.assertEqual(caught.exception.status_code, 405)

    def test_legacy_preflight_api_v4_returns_stable_gone_code(self) -> None:
        from fastapi import HTTPException
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
                "root_path": "",
                "path": f"/api/admin/backtests/{self.run_id}/data-preflight",
                "raw_path": b"/api/admin/backtests/data-preflight",
                "query_string": b"limit=1&section=calendar",
                "headers": [],
            }
        )
        with patch("app.backtesting.result_router.DATA_PREFLIGHT_API_VERSION", 4):
            with self.assertRaises(HTTPException) as caught:
                legacy_data_preflight_redirect(run_id=self.run_id, request=request)
        self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(
            caught.exception.detail["reason_code"],
            "calendar_preflight_redirect_sunset",
        )

    def test_legacy_preflight_api_v3_redirect_preserves_query(self) -> None:
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 50000),
                "root_path": "",
                "path": f"/api/admin/backtests/{self.run_id}/data-preflight",
                "raw_path": b"/api/admin/backtests/data-preflight",
                "query_string": b"limit=1&cursor=A%2BB",
                "headers": [],
            }
        )
        with patch("app.backtesting.result_router.DATA_PREFLIGHT_API_VERSION", 3):
            response = legacy_data_preflight_redirect(
                run_id=self.run_id,
                request=request,
            )
        self.assertEqual(response.status_code, 308)
        self.assertEqual(
            response.headers["location"],
            f"/api/admin/backtest-runs/{self.run_id}/results/data-preflight?limit=1&cursor=A%2BB",
        )

    def test_unavailable_metrics_expose_reason_not_zero(self) -> None:
        self.repo.append(
            "metrics",
            BacktestMetricRecord(
                run_id=self.run_id,
                metric_key="max_drawdown",
                formula_version="v1",
                value=None,
                unavailable_reason="无有效估值点",
            ),
        )
        page = list_metrics(run_id=self.run_id, limit=10, cursor=None, session=self.session,
            signing_key=SIGNING_KEY)
        self.assertIsNone(page["items"][0].value)
        self.assertEqual(page["items"][0].unavailable_reason, "无有效估值点")


if __name__ == "__main__":
    unittest.main()
