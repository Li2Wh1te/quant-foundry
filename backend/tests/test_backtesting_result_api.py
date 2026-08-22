"""Tests for the read-only backtest result HTTP layer.

The project test suite calls route functions directly (no TestClient), so
these tests exercise the same functions FastAPI dispatches to and then
serialize the response models with ``model_dump(mode="json")`` to pin the
wire contract (Decimal as string, opaque cursor envelope).
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.accounting import OrderSide
from app.backtesting.domain import PositionSide
from app.backtesting.pagination import CursorQueryMismatchError
from app.backtesting.result_models import (
    BacktestMetricRecord,
    BacktestOrderRecord,
    BacktestPositionRecord,
    InstrumentDisplaySnapshot,
    ResultOrderStatus,
)
from app.backtesting.result_records import RESULT_TABLE_NAMES, Base
from app.backtesting.result_repository import BacktestResultRepository
from app.backtesting.result_router import list_metrics, list_orders, list_positions
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
