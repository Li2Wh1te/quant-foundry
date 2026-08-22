"""Repository tests for result writes and cursor-paginated reads.

The tests run against an in-memory SQLite database so keyset pagination,
uniqueness constraints, and snapshot upper bounds are exercised through a
real SQL engine.  PostgreSQL remains the production dialect; the JSON
columns carry a SQLite variant purely for this test suite.
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.accounting import OrderSide
from app.backtesting.domain import PositionSide, ValuationStatus
from app.backtesting.pagination import CursorQueryMismatchError
from app.backtesting.result_models import (
    BacktestDataChunkRecord,
    BacktestDataPreflightRecord,
    BacktestEquityCurveRecord,
    BacktestFillRecord,
    BacktestMetricRecord,
    BacktestOrderRecord,
    BacktestPositionRecord,
    ChunkValidationStatus,
    DataPhase,
    InstrumentDisplaySnapshot,
    ResultOrderStatus,
)
from app.backtesting.result_records import Base
from app.backtesting.result_repository import (
    BacktestResultRepository,
    ResultFilterError,
    ResultRecordConflictError,
)


UTC = timezone.utc


def ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 1, hour, minute, tzinfo=UTC)


def make_order(
    run_id,
    *,
    submitted_at=None,
    instrument_id=None,
    side=OrderSide.BUY,
    status=ResultOrderStatus.FILLED,
    order_id=None,
) -> BacktestOrderRecord:
    instrument_id = instrument_id or uuid4()
    return BacktestOrderRecord(
        run_id=run_id,
        order_id=order_id or uuid4(),
        instrument_id=instrument_id,
        display=InstrumentDisplaySnapshot(
            instrument_id=instrument_id,
            event_trading_code="510300",
            event_name="沪深300ETF",
            event_display_name="沪深300ETF",
        ),
        side=side,
        order_type="market",
        quantity="100",
        status=status,
        submitted_at=submitted_at or ts(9),
        price="3.85",
    )


RESULT_TABLE_NAMES = [
    "backtest_steps",
    "backtest_decisions",
    "backtest_orders",
    "backtest_order_updates",
    "backtest_fills",
    "backtest_positions",
    "backtest_equity_curve",
    "backtest_metrics",
    "backtest_data_preflight",
    "backtest_data_chunks",
]


class ResultRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        # Exact names: the shared Base.metadata also holds unrelated
        # backtest_* tables (e.g. account profiles with JSONB columns that
        # the SQLite test dialect cannot compile).
        result_tables = [
            Base.metadata.tables[name] for name in RESULT_TABLE_NAMES
        ]
        Base.metadata.create_all(self.engine, tables=result_tables)
        self.session = Session(self.engine)
        self.repo = BacktestResultRepository(self.session)
        self.run_id = uuid4()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    # -- helpers -----------------------------------------------------------

    def seed_orders(self, count: int, *, start_hour: int = 9) -> list[UUID]:
        orders = [
            make_order(self.run_id, submitted_at=ts(start_hour) + timedelta(minutes=index))
            for index in range(count)
        ]
        self.repo.append("orders", *orders)
        return [order.order_id for order in orders]

    def walk_pages(self, kind: str, *, limit: int, **filters):
        """Collect a full cursor walk and return (ids, page_count)."""

        collected = []
        cursor = None
        pages = 0
        while True:
            page = self.repo.read_page(
                kind, run_id=self.run_id, limit=limit, cursor=cursor, **filters
            )
            pages += 1
            collected.extend(page.items)
            if not page.has_more:
                self.assertIsNone(page.next_cursor)
                break
            cursor = page.next_cursor
            self.assertIsNotNone(cursor)
        return collected, pages


class WriteContractTestCase(ResultRepositoryTestCase):
    def test_round_trips_orders_and_preserves_display_fields(self) -> None:
        order_id = self.seed_orders(1)[0]
        page = self.repo.read_page("orders", run_id=self.run_id)
        self.assertEqual(len(page.items), 1)
        row = page.items[0]
        self.assertEqual(row.order_id, order_id)
        self.assertEqual(row.event_trading_code, "510300")
        self.assertEqual(row.event_name, "沪深300ETF")
        self.assertEqual(row.status, "filled")

    def test_duplicate_business_key_across_batches_is_rejected(self) -> None:
        order = make_order(self.run_id)
        self.repo.append("orders", order)
        duplicate = make_order(self.run_id, order_id=order.order_id)
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append("orders", duplicate)

    def test_duplicate_identity_within_one_batch_is_rejected(self) -> None:
        order = make_order(self.run_id)
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append("orders", order, order)

    def test_positions_unique_per_valuation_point_and_side(self) -> None:
        as_of = ts(15)
        instrument = uuid4()
        snapshot = InstrumentDisplaySnapshot(instrument_id=instrument)
        position = BacktestPositionRecord(
            run_id=self.run_id,
            as_of=as_of,
            instrument_id=instrument,
            display=snapshot,
            side=PositionSide.LONG,
            quantity="10",
            available_quantity="10",
            average_price="2",
            realized_pnl="0",
            unrealized_pnl="1",
        )
        duplicate = BacktestPositionRecord(
            run_id=self.run_id,
            as_of=as_of,
            instrument_id=instrument,
            display=snapshot,
            side=PositionSide.LONG,
            quantity="10",
            available_quantity="10",
            average_price="2",
            realized_pnl="0",
            unrealized_pnl="1",
        )
        self.repo.append("positions", position)
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append("positions", duplicate)

    def test_wrong_dto_type_for_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.append("orders", BacktestMetricRecord(
                run_id=self.run_id,
                metric_key="k",
                formula_version="v1",
                value="1",
            ))

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.append("no_such_kind")

    def test_metrics_keep_unavailable_reason_instead_of_zero(self) -> None:
        self.repo.append(
            "metrics",
            BacktestMetricRecord(
                run_id=self.run_id,
                metric_key="sharpe_ratio",
                formula_version="v1",
                value=None,
                unavailable_reason="样本数不足",
                sample_count=3,
            ),
        )
        row = self.repo.read_page("metrics", run_id=self.run_id).items[0]
        self.assertIsNone(row.value)
        self.assertEqual(row.unavailable_reason, "样本数不足")

    def test_preflight_unique_per_phase(self) -> None:
        admission = BacktestDataPreflightRecord(
            run_id=self.run_id,
            phase=DataPhase.ADMISSION,
            status="passed",
            report_hash="hash-a",
        )
        session_report = BacktestDataPreflightRecord(
            run_id=self.run_id,
            phase=DataPhase.SESSION,
            status="passed",
            report_hash="hash-b",
        )
        self.repo.append("data_preflight", admission, session_report)
        with self.assertRaises(ResultRecordConflictError):
            self.repo.append(
                "data_preflight",
                BacktestDataPreflightRecord(
                    run_id=self.run_id,
                    phase=DataPhase.ADMISSION,
                    status="failed",
                    report_hash="hash-c",
                ),
            )

    def test_data_chunks_sort_by_phase_then_sequence(self) -> None:
        self.repo.append(
            "data_chunks",
            BacktestDataChunkRecord(
                run_id=self.run_id,
                phase=DataPhase.SESSION,
                chunk_sequence=1,
                time_start=ts(10),
                time_end=ts(11),
                chunk_strategy_version="bounded_blocks@1",
                token_digest="digest-1",
                validation_status=ChunkValidationStatus.PASSED,
            ),
            BacktestDataChunkRecord(
                run_id=self.run_id,
                phase=DataPhase.ADMISSION,
                chunk_sequence=5,
                time_start=ts(9),
                time_end=ts(10),
                chunk_strategy_version="bounded_blocks@1",
                token_digest="digest-0",
                validation_status=ChunkValidationStatus.FAILED,
                failure_reason="校验失败",
            ),
        )
        rows, _ = self.walk_pages("data_chunks", limit=10)
        self.assertEqual(
            [(row.phase, row.chunk_sequence) for row in rows],
            [("admission", 5), ("session", 1)],
        )


class PaginationContractTestCase(ResultRepositoryTestCase):
    def test_empty_run_returns_empty_envelope(self) -> None:
        page = self.repo.read_page("orders", run_id=self.run_id)
        self.assertEqual(page.items, ())
        self.assertIsNone(page.next_cursor)
        self.assertFalse(page.has_more)

    def test_invalid_limit_is_rejected(self) -> None:
        for bad in (0, 501, -3):
            with self.assertRaises(ValueError):
                self.repo.read_page("orders", run_id=self.run_id, limit=bad)

    def test_multi_page_walk_is_complete_ordered_and_duplicate_free(self) -> None:
        expected = self.seed_orders(25)
        collected, pages = self.walk_pages("orders", limit=10)
        self.assertEqual([row.order_id for row in collected], expected)
        self.assertEqual(pages, 3)
        times = [row.submitted_at for row in collected]
        self.assertEqual(times, sorted(times))

    def test_identical_queries_produce_identical_cursors(self) -> None:
        self.seed_orders(15)
        first = self.repo.read_page("orders", run_id=self.run_id, limit=10)
        second = self.repo.read_page("orders", run_id=self.run_id, limit=10)
        self.assertEqual(first.next_cursor, second.next_cursor)

    def test_equal_timestamps_are_disambiguated_by_order_id(self) -> None:
        same_time = ts(9, 30)
        order_a = make_order(self.run_id, submitted_at=same_time)
        order_b = make_order(self.run_id, submitted_at=same_time)
        ordered_pair = sorted([order_a, order_b], key=lambda o: o.order_id)
        self.repo.append("orders", *ordered_pair)

        page_one = self.repo.read_page("orders", run_id=self.run_id, limit=1)
        self.assertEqual(page_one.items[0].order_id, ordered_pair[0].order_id)
        self.assertTrue(page_one.has_more)
        page_two = self.repo.read_page(
            "orders", run_id=self.run_id, limit=1, cursor=page_one.next_cursor
        )
        self.assertEqual(page_two.items[0].order_id, ordered_pair[1].order_id)
        self.assertFalse(page_two.has_more)

    def test_tampered_cursor_is_rejected(self) -> None:
        self.seed_orders(15)
        page = self.repo.read_page("orders", run_id=self.run_id, limit=10)
        token = page.next_cursor
        tampered = (
            token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        )
        with self.assertRaises(ValueError):
            self.repo.read_page(
                "orders", run_id=self.run_id, limit=10, cursor=tampered
            )
        self.assertTrue(token)  # original token remains usable
        self.repo.read_page("orders", run_id=self.run_id, limit=10, cursor=token)

    def test_limit_change_invalidates_old_cursor(self) -> None:
        self.seed_orders(15)
        page = self.repo.read_page("orders", run_id=self.run_id, limit=10)
        with self.assertRaises(CursorQueryMismatchError):
            self.repo.read_page(
                "orders", run_id=self.run_id, limit=5, cursor=page.next_cursor
            )

    def test_filter_change_invalidates_old_cursor(self) -> None:
        self.seed_orders(15)
        page = self.repo.read_page("orders", run_id=self.run_id, limit=10)
        with self.assertRaises(CursorQueryMismatchError):
            self.repo.read_page(
                "orders",
                run_id=self.run_id,
                limit=10,
                cursor=page.next_cursor,
                side="sell",
            )

    def test_other_run_invalidates_old_cursor(self) -> None:
        self.seed_orders(15)
        page = self.repo.read_page("orders", run_id=self.run_id, limit=10)
        with self.assertRaises(CursorQueryMismatchError):
            self.repo.read_page(
                "orders", run_id=uuid4(), limit=10, cursor=page.next_cursor
            )

    def test_appended_rows_stay_outside_an_existing_cursor(self) -> None:
        original = self.seed_orders(30)
        first_page = self.repo.read_page("orders", run_id=self.run_id, limit=10)

        # Simulate a running run appending results after pagination began.
        late_orders = [
            make_order(self.run_id, submitted_at=ts(23) + timedelta(minutes=index))
            for index in range(10)
        ]
        self.repo.append("orders", *late_orders)

        collected = list(first_page.items)
        cursor = first_page.next_cursor
        while cursor is not None:
            page = self.repo.read_page(
                "orders", run_id=self.run_id, limit=10, cursor=cursor
            )
            collected.extend(page.items)
            cursor = page.next_cursor

        seen_ids = [row.order_id for row in collected]
        self.assertEqual(len(seen_ids), len(set(seen_ids)))
        self.assertEqual(set(seen_ids), set(original))
        late_ids = {order.order_id for order in late_orders}
        self.assertEqual(set(seen_ids) & late_ids, set())

    def test_decisions_tie_break_by_decision_id(self) -> None:
        from app.backtesting.result_models import (
            BacktestDecisionRecord,
            DecisionValidationStatus,
        )

        decision_time = ts(10)
        first = BacktestDecisionRecord(
            run_id=self.run_id,
            decision_id=uuid4(),
            step_sequence=1,
            decision_time=decision_time,
            mode="hold",
            validation_status=DecisionValidationStatus.ACCEPTED,
        )
        second = BacktestDecisionRecord(
            run_id=self.run_id,
            decision_id=uuid4(),
            step_sequence=1,
            decision_time=decision_time,
            mode="hold",
            validation_status=DecisionValidationStatus.ACCEPTED,
        )
        pair = sorted([first, second], key=lambda d: d.decision_id)
        self.repo.append("decisions", *pair)
        rows, _ = self.walk_pages("decisions", limit=1)
        self.assertEqual([row.decision_id for row in rows], [d.decision_id for d in pair])


class FilterContractTestCase(ResultRepositoryTestCase):
    def test_instrument_filter_uses_identity_only(self) -> None:
        instrument_a = uuid4()
        instrument_b = uuid4()
        orders = [
            make_order(self.run_id, instrument_id=instrument_a),
            make_order(self.run_id, instrument_id=instrument_b),
        ]
        self.repo.append("orders", *orders)
        page = self.repo.read_page(
            "orders", run_id=self.run_id, instrument_id=instrument_b
        )
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].instrument_id, instrument_b)

    def test_time_bounds_are_inclusive(self) -> None:
        self.seed_orders(5)  # 09:00 .. 09:04
        page = self.repo.read_page(
            "orders",
            run_id=self.run_id,
            start_time=ts(9, 1),
            end_time=ts(9, 3),
        )
        self.assertEqual(len(page.items), 3)

    def test_naive_time_bounds_are_rejected(self) -> None:
        with self.assertRaises(ResultFilterError):
            self.repo.read_page(
                "orders",
                run_id=self.run_id,
                start_time=datetime(2026, 6, 1, 9, 0),
            )

    def test_enum_filters_on_orders(self) -> None:
        buy = make_order(self.run_id, side=OrderSide.BUY)
        sell = make_order(self.run_id, side=OrderSide.SELL)
        rejected = make_order(
            self.run_id, status=ResultOrderStatus.REJECTED
        )
        self.repo.append("orders", buy, sell, rejected)
        sells = self.repo.read_page(
            "orders", run_id=self.run_id, side="sell"
        )
        self.assertEqual([row.order_id for row in sells.items], [sell.order_id])
        rejected_page = self.repo.read_page(
            "orders", run_id=self.run_id, status="rejected"
        )
        self.assertEqual(
            [row.order_id for row in rejected_page.items], [rejected.order_id]
        )

    def test_unsupported_filter_is_rejected(self) -> None:
        with self.assertRaises(ResultFilterError):
            self.repo.read_page("metrics", run_id=self.run_id, side="buy")

    def test_positions_return_raw_non_zero_snapshots(self) -> None:
        as_of_one = ts(15)
        as_of_two = ts(16)
        instrument = uuid4()
        rows = [
            BacktestPositionRecord(
                run_id=self.run_id,
                as_of=as_of,
                instrument_id=instrument,
                display=InstrumentDisplaySnapshot(instrument_id=instrument),
                side=PositionSide.LONG,
                quantity=quantity,
                available_quantity=quantity,
                average_price="2",
                realized_pnl="0",
                unrealized_pnl="1",
            )
            for as_of, quantity in ((as_of_one, "10"), (as_of_two, "8"))
        ]
        self.repo.append("positions", *rows)
        page = self.repo.read_page(
            "positions", run_id=self.run_id, instrument_id=instrument
        )
        # Both raw valuation snapshots are returned; nothing collapses to the
        # latest row, and zero rows never exist.  (SQLite returns naive
        # datetimes, so comparisons normalize back to UTC.)
        returned_times = [
            row.as_of.replace(tzinfo=UTC) if row.as_of.tzinfo is None else row.as_of
            for row in page.items
        ]
        self.assertEqual(returned_times, [as_of_one, as_of_two])
        self.assertEqual([row.quantity for row in page.items], [Decimal("10"), Decimal("8")])

    def test_equity_curve_sort_key_uses_time_then_sequence(self) -> None:
        rows = [
            BacktestEquityCurveRecord(
                run_id=self.run_id,
                sequence=sequence,
                as_of=as_of,
                valuation_status=ValuationStatus.COMPLETE,
                cash="100",
                market_value="50",
                equity="150",
                cumulative_return="0.5",
                drawdown="-0.1",
                cumulative_fees="3",
            )
            for sequence, as_of in ((0, ts(9)), (1, ts(10)), (2, ts(10)))
        ]
        self.repo.append("equity_curve", *rows)
        page_rows, _ = self.walk_pages("equity_curve", limit=2)
        normalized = [
            (
                row.as_of.replace(tzinfo=UTC) if row.as_of.tzinfo is None else row.as_of,
                row.sequence,
            )
            for row in page_rows
        ]
        self.assertEqual(normalized, [(ts(9), 0), (ts(10), 1), (ts(10), 2)])


if __name__ == "__main__":
    unittest.main()
