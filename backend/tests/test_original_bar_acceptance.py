"""Acceptance checks for raw ETF Bar facts and decimal protocol values.

These tests intentionally exercise the adapter boundary instead of the
ingestion client.  The raw row is allowed to be malformed; the adapter must
classify it, retain the source values, and leave formal-consumption filtering
to the quality gate.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from uuid import uuid4

from app.backtesting.data.adapters.etf import EtfFactsAdapter
from app.backtesting.data.facts import Bar, BarFact
from app.backtesting.data.requests import PriceBasis, QualityStatus
from app.backtesting.data.reports import canonical_json
from app.strategy_protocol.data_view import BarDTO, StrategyDataDTO
from app.strategy_protocol.contract import InvalidProviderResultError


INSTRUMENT_ID = uuid4()
OBSERVED_AT = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)


def make_row(**overrides):
    values = dict(
        ts_code="510300.SH",
        trade_date=date(2026, 8, 17),
        open=Decimal("3.75"),
        high=Decimal("3.80"),
        low=Decimal("3.70"),
        close=Decimal("3.75"),
        vol=Decimal("100"),
        amount=Decimal("375"),
        updated_at=OBSERVED_AT,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_adapter() -> EtfFactsAdapter:
    """Build the read-only adapter with inert ports for projection tests."""

    return EtfFactsAdapter(
        code_mappings=lambda *args, **kwargs: (),
        daily_bars=lambda *args, **kwargs: (),
        adjustment_factors=lambda *args, **kwargs: (),
        trading_days=lambda *args, **kwargs: [],
    )


class EtfRawBarAcceptanceTests(TestCase):
    def test_all_illegal_ohlc_rules_preserve_source_values(self):
        """A10-A15: every ETF v1 invalid rule is classified without repair."""

        cases = (
            {"open": Decimal("0")},
            {"high": Decimal("0")},
            {"low": Decimal("-1")},
            {"close": Decimal("-1")},
            {"high": Decimal("3.60"), "low": Decimal("3.70"), "open": Decimal("3.65"), "close": Decimal("3.65")},
            {"open": Decimal("3.90")},
            {"close": Decimal("3.60")},
            {"open": None},
            {"high": None},
            {"low": None},
            {"close": None},
        )
        adapter = make_adapter()
        for overrides in cases:
            with self.subTest(overrides=overrides):
                row = make_row(**overrides)
                bar = adapter.project_bar(row, INSTRUMENT_ID)
                self.assertIsInstance(bar, Bar)
                self.assertIs(BarFact, Bar)
                self.assertIs(bar.evidence.quality_status, QualityStatus.INVALID)
                for field in ("open", "high", "low", "close"):
                    expected = getattr(row, field)
                    actual = getattr(bar, field)
                    self.assertEqual(actual, expected)

    def test_missing_fields_keep_none_and_expose_audit_metadata(self):
        """A15-A16: missing source fields stay absent with a reason/unit map."""

        bar = make_adapter().project_bar(
            make_row(open=None, vol=None, amount=None), INSTRUMENT_ID
        )
        self.assertIsNone(bar.open)
        self.assertIsNone(bar.volume)
        self.assertIsNone(bar.amount)
        self.assertEqual(
            bar.metadata["missing_reasons"]["open"], "source_field_missing"
        )
        self.assertEqual(bar.metadata["field_units"]["open"], "CNY")

    def test_invalid_bar_carries_versioned_validation_rule(self):
        """A23: projected facts identify the ETF validation rule version."""

        bar = make_adapter().project_bar(make_row(open=Decimal("0")), INSTRUMENT_ID)
        self.assertEqual(str(bar.validation_rule_version), "etf_raw_bar_validation@1")
        self.assertEqual(bar.metadata["adapter_key"], "etf_raw_bar_adapter")
        self.assertEqual(bar.metadata["adapter_version"], "etf_raw_bar_adapter@1")


class DecimalProtocolAcceptanceTests(TestCase):
    def test_strategy_bar_dto_rejects_binary_float_values(self):
        """A20: the strategy JSON boundary never accepts a binary float."""

        for field in ("open", "high", "low", "close", "volume", "amount"):
            values = {name: Decimal("1") for name in ("open", "high", "low", "close", "volume", "amount")}
            values[field] = 1.25
            with self.subTest(field=field), self.assertRaises(ValueError):
                BarDTO(INSTRUMENT_ID, date(2026, 8, 17), values)

    def test_decimal_is_serialized_as_a_decimal_string(self):
        """A20: canonical report JSON does not turn Decimal into a float."""

        self.assertEqual(canonical_json({"price": Decimal("1.230")}), '{"price":"1.230"}')

    def test_strategy_boundary_blocks_invalid_raw_bar(self):
        """A22: an invalid raw fact is available to preflight, never strategy code."""

        invalid = make_adapter().project_bar(make_row(open=Decimal("0")), INSTRUMENT_ID)

        class View:
            def bars(self, instrument_id, *, start_date, end_date, lookback_sessions):
                return (invalid,)

            def adjusted_series(self, instrument_id, *, start_date, end_date, lookback_sessions, basis):
                return ()

        facade = StrategyDataDTO(
            View(), data_cutoff=datetime(2026, 8, 21, tzinfo=UTC)
        )
        with self.assertRaises(InvalidProviderResultError):
            facade.bars(INSTRUMENT_ID, start_date=date(2026, 8, 17), end_date=date(2026, 8, 17))


if __name__ == "__main__":
    import unittest

    unittest.main()
