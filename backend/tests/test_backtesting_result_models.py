"""Tests for the immutable backtest result DTO contracts."""

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.backtesting.accounting import OrderSide
from app.backtesting.domain import DomainValidationError, PositionSide, ValuationStatus
from app.backtesting.result_models import (
    BacktestDataChunkRecord,
    BacktestEquityCurveRecord,
    BacktestFillRecord,
    BacktestMetricRecord,
    BacktestOrderRecord,
    BacktestPositionRecord,
    ChunkValidationStatus,
    DataPhase,
    DataQualityStatus,
    DecisionValidationStatus,
    InstrumentDisplaySnapshot,
    ResultOrderStatus,
    StepPhase,
)


RUN_ID = uuid4()
INSTRUMENT_ID = uuid4()
TS = datetime(2026, 3, 1, tzinfo=timezone.utc)
NAIVE_TS = datetime(2026, 3, 1)


def display(**overrides) -> InstrumentDisplaySnapshot:
    fields = {
        "instrument_id": INSTRUMENT_ID,
        "event_trading_code": "510300",
        "event_name": "沪深300ETF",
        "event_display_name": "沪深300ETF",
    }
    fields.update(overrides)
    return InstrumentDisplaySnapshot(**fields)


def order_dto(**overrides) -> BacktestOrderRecord:
    fields = {
        "run_id": RUN_ID,
        "order_id": uuid4(),
        "instrument_id": INSTRUMENT_ID,
        "display": display(),
        "side": OrderSide.BUY,
        "order_type": "market",
        "quantity": "100",
        "status": ResultOrderStatus.FILLED,
        "submitted_at": TS,
        "price": "3.85",
    }
    fields.update(overrides)
    return BacktestOrderRecord(**fields)


class IdentityContractTestCase(unittest.TestCase):
    def test_missing_or_non_uuid_instrument_identity_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            order_dto(instrument_id=None)
        with self.assertRaises(DomainValidationError):
            order_dto(instrument_id="510300")
        with self.assertRaises(DomainValidationError):
            order_dto(run_id="run-not-a-uuid")

    def test_display_snapshot_must_match_row_instrument(self) -> None:
        with self.assertRaises(DomainValidationError):
            order_dto(display=InstrumentDisplaySnapshot(instrument_id=uuid4()))

    def test_empty_display_fields_are_still_valid(self) -> None:
        record = order_dto(display=display(event_trading_code=None, event_name=None))
        self.assertIsNone(record.display.event_trading_code)
        self.assertIsNone(record.display.event_name)
        self.assertEqual(record.instrument_id, INSTRUMENT_ID)

    def test_frozen_snapshot_survives_later_renames(self) -> None:
        snapshot = display()
        renamed = InstrumentDisplaySnapshot(
            instrument_id=snapshot.instrument_id,
            event_trading_code="999999",
            event_name="新名称",
            event_display_name="新名称",
        )
        record = order_dto(display=snapshot)
        # Writing a differently named snapshot elsewhere cannot mutate history.
        self.assertEqual(record.display.event_name, "沪深300ETF")
        self.assertNotEqual(record.display, renamed)


class TypeContractTestCase(unittest.TestCase):
    def test_binary_floats_are_rejected(self) -> None:
        with self.assertRaises((DomainValidationError, TypeError)):
            order_dto(price=0.1)
        with self.assertRaises((DomainValidationError, TypeError)):
            BacktestFillRecord(
                run_id=RUN_ID,
                fill_id=uuid4(),
                order_id=uuid4(),
                instrument_id=INSTRUMENT_ID,
                display=display(),
                side=OrderSide.SELL,
                timestamp=TS,
                price="1.0",
                quantity="10",
                fees=0.01,
            )

    def test_naive_datetimes_are_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            order_dto(submitted_at=NAIVE_TS)

    def test_nested_json_targets_reject_floats_and_freeze(self) -> None:
        from app.backtesting.result_models import BacktestDecisionRecord

        decision = BacktestDecisionRecord(
            run_id=RUN_ID,
            decision_id=uuid4(),
            step_sequence=1,
            decision_time=TS,
            mode="target_weights",
            validation_status=DecisionValidationStatus.ACCEPTED,
            targets={"weights": {"etf-a": "0.6"}},
            duration_ms="12",
        )
        self.assertEqual(decision.targets["weights"]["etf-a"], "0.6")
        with self.assertRaises(DomainValidationError):
            BacktestDecisionRecord(
                run_id=RUN_ID,
                decision_id=uuid4(),
                step_sequence=1,
                decision_time=TS,
                mode="target_weights",
                validation_status=DecisionValidationStatus.ACCEPTED,
                targets={"weight": 0.6},
            )

    def test_sequence_numbers_must_be_integers(self) -> None:
        from app.backtesting.result_models import BacktestStepRecord

        with self.assertRaises(DomainValidationError):
            BacktestStepRecord(
                run_id=RUN_ID,
                step_sequence="1",
                time_start=TS,
                time_end=TS,
                data_cutoff_at=TS,
                phase=StepPhase.DATA,
                data_quality=DataQualityStatus.OK,
            )
        with self.assertRaises(DomainValidationError):
            BacktestStepRecord(
                run_id=RUN_ID,
                step_sequence=True,
                time_start=TS,
                time_end=TS,
                data_cutoff_at=TS,
                phase=StepPhase.DATA,
                data_quality=DataQualityStatus.OK,
            )


class PositionContractTestCase(unittest.TestCase):
    def position_dto(self, **overrides) -> BacktestPositionRecord:
        fields = {
            "run_id": RUN_ID,
            "as_of": TS,
            "instrument_id": INSTRUMENT_ID,
            "display": display(),
            "side": PositionSide.LONG,
            "quantity": "100",
            "available_quantity": "100",
            "average_price": "3.80",
            "mark_price": "3.90",
            "realized_pnl": "0",
            "unrealized_pnl": "10",
        }
        fields.update(overrides)
        return BacktestPositionRecord(**fields)

    def test_zero_quantity_position_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.position_dto(quantity="0")

    def test_negative_quantity_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.position_dto(quantity="-5")

    def test_available_quantity_cannot_exceed_quantity(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.position_dto(quantity="10", available_quantity="11")


class EquityAndMetricContractTestCase(unittest.TestCase):
    def test_blocked_points_cannot_carry_equity_values(self) -> None:
        with self.assertRaises(DomainValidationError):
            BacktestEquityCurveRecord(
                run_id=RUN_ID,
                sequence=1,
                as_of=TS,
                valuation_status=ValuationStatus.BLOCKED,
                cash="100",
                equity="100",
                cumulative_fees="0",
            )

    def test_valid_points_require_equity_fields(self) -> None:
        with self.assertRaises(DomainValidationError):
            BacktestEquityCurveRecord(
                run_id=RUN_ID,
                sequence=1,
                as_of=TS,
                valuation_status=ValuationStatus.COMPLETE,
                cash="100",
                cumulative_fees="0",
            )

    def test_metrics_need_value_xor_reason(self) -> None:
        unavailable = BacktestMetricRecord(
            run_id=RUN_ID,
            metric_key="sharpe_ratio",
            formula_version="v1",
            value=None,
            unavailable_reason="样本数不足",
        )
        self.assertIsNone(unavailable.value)
        with self.assertRaises(DomainValidationError):
            BacktestMetricRecord(
                run_id=RUN_ID,
                metric_key="sharpe_ratio",
                formula_version="v1",
                value=None,
            )
        with self.assertRaises(DomainValidationError):
            BacktestMetricRecord(
                run_id=RUN_ID,
                metric_key="sharpe_ratio",
                formula_version="v1",
                value="1.5",
                unavailable_reason="样本数不足",
            )


class GeneralityContractTestCase(unittest.TestCase):
    def test_fictional_asset_uses_the_same_dtos(self) -> None:
        fictional = InstrumentDisplaySnapshot(
            instrument_id=uuid4(),
            event_trading_code=None,
            event_name="虚构资产",
        )
        record = order_dto(
            instrument_id=fictional.instrument_id,
            display=fictional,
        )
        self.assertIsInstance(record, BacktestOrderRecord)
        self.assertEqual(record.display.event_name, "虚构资产")

    def test_result_models_expose_no_etf_specific_fields(self) -> None:
        import dataclasses
        import inspect

        import app.backtesting.result_models as module
        import app.backtesting.result_records as records_module

        etf_terms = ("ts_code", "fund_type", "purchase_status", "redemption_status")
        dto_field_names: set[str] = set()
        for name, member in inspect.getmembers(module, dataclasses.is_dataclass):
            if isinstance(member, type):
                dto_field_names.update(field.name for field in dataclasses.fields(member))
        orm_column_names: set[str] = set()
        for name, member in inspect.getmembers(records_module, inspect.isclass):
            # Only tables owned by this module; Base.metadata is shared with
            # unrelated models once the whole app has been imported.
            if issubclass(member, records_module.Base) and hasattr(member, "__tablename__"):
                orm_column_names.update(member.__table__.columns.keys())
        for term in etf_terms:
            self.assertNotIn(term, dto_field_names, f"result DTOs expose ETF field {term}")
            self.assertNotIn(term, orm_column_names, f"result tables expose ETF column {term}")


class ChunkContractTestCase(unittest.TestCase):
    def chunk_dto(self, **overrides) -> BacktestDataChunkRecord:
        fields = {
            "run_id": RUN_ID,
            "phase": DataPhase.SESSION,
            "chunk_sequence": 0,
            "time_start": TS,
            "time_end": TS,
            "chunk_strategy_version": "bounded_blocks@1",
            "token_digest": "sha256:abc",
            "validation_status": ChunkValidationStatus.PASSED,
        }
        fields.update(overrides)
        return BacktestDataChunkRecord(**fields)

    def test_failed_chunks_require_reason_and_passed_forbid_it(self) -> None:
        failed = self.chunk_dto(
            validation_status=ChunkValidationStatus.FAILED,
            failure_reason="校验和不一致",
        )
        self.assertEqual(failed.failure_reason, "校验和不一致")
        with self.assertRaises(DomainValidationError):
            self.chunk_dto(validation_status=ChunkValidationStatus.FAILED)
        with self.assertRaises(DomainValidationError):
            self.chunk_dto(failure_reason="多余的原因")


if __name__ == "__main__":
    unittest.main()
