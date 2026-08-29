"""Focused checks for the raw BarFact envelope and decimal fidelity."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import TestCase
from uuid import uuid4

from app.backtesting.data import Bar, BarFact, FactEvidence, PriceBasis, QualityStatus
from app.backtesting.data.errors import ProviderContractViolationError


class BarFactContractTests(TestCase):
    def _evidence(self, quality: QualityStatus) -> FactEvidence:
        return FactEvidence(
            source="fixture",
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            quality_status=quality,
        )

    def _bar(self, quality: QualityStatus = QualityStatus.INVALID, **kwargs) -> Bar:
        values = dict(
            instrument_id=uuid4(),
            trade_date=date(2026, 1, 2),
            frequency="1d",
            open="0",
            high="-1",
            low="-2",
            close=None,
            volume=None,
            amount=None,
            price_basis=PriceBasis.RAW,
            evidence=self._evidence(quality),
        )
        values.update(kwargs)
        return Bar(**values)

    def test_barfact_is_the_single_bar_model_and_preserves_missing_raw_values(self):
        bar = self._bar()
        self.assertIs(BarFact, Bar)
        self.assertEqual(bar.open, Decimal("0"))
        self.assertEqual(bar.high, Decimal("-1"))
        self.assertIsNone(bar.close)
        self.assertIsNone(bar.volume)
        self.assertIsNone(bar.turnover)
        self.assertIs(bar.metadata, bar.attributes)

    def test_complete_bar_requires_ohlc_but_allows_missing_optional_volume(self):
        with self.assertRaises(ProviderContractViolationError):
            self._bar(QualityStatus.COMPLETE)
        bar = self._bar(
            QualityStatus.COMPLETE,
            open="1",
            high="2",
            low="1",
            close="1.5",
        )
        self.assertIsNone(bar.volume)
        self.assertIsNone(bar.amount)

    def test_optional_fields_can_be_omitted(self):
        bar = Bar(
            instrument_id=uuid4(),
            trade_date=date(2026, 1, 2),
            frequency="1d",
            evidence=self._evidence(QualityStatus.INVALID),
        )
        self.assertIsNone(bar.open)
        self.assertIsNone(bar.volume)

    def test_float_values_are_rejected_at_fact_boundary(self):
        with self.assertRaises(ProviderContractViolationError):
            self._bar(open=1.5)


if __name__ == "__main__":
    import unittest

    unittest.main()
