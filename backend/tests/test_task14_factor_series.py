"""Acceptance checks for task 14-04 factor reads and 14-05 research bars."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.backtesting.data.adjustment_policy import AdjustmentSeriesPolicy
from app.backtesting.data.etf_adjustment import (
    build_research_price_series,
    cutoff_local_date,
    normalize_adjustment_factor,
    normalize_adjustment_factors,
)
from app.backtesting.data.errors import (
    HistoryBarsDuplicateError,
    HistoryBarsIncompleteError,
    InvalidDataRequestError,
    ProviderContractViolationError,
)
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar, FactEvidence
from app.backtesting.data.requests import PriceBasis, QualityStatus


IID = uuid4()
SOURCE = "tushare"
CODE = "510300.SH"
UTC_OBSERVED = datetime(2026, 8, 22, 2, tzinfo=UTC)


def _row(day: object, factor: object = "1.0", **kwargs: object):
    values = {
        "ts_code": CODE,
        "trade_date": day,
        "adj_factor": factor,
        "updated_at": UTC_OBSERVED,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def _bars(days: tuple[date, ...]) -> tuple[Bar, ...]:
    evidence = FactEvidence(
        source=SOURCE,
        observed_at=UTC_OBSERVED,
        quality_status=QualityStatus.COMPLETE,
    )
    return tuple(
        Bar(
            instrument_id=IID,
            trade_date=day,
            frequency="1d",
            open="10",
            high="12",
            low="8",
            close="11",
            volume="100",
            amount="1100",
            evidence=evidence,
        )
        for day in days
    )


def _points(days: tuple[date, ...], factors: tuple[str, ...], basis: PriceBasis):
    evidence = FactEvidence(
        source=SOURCE,
        observed_at=UTC_OBSERVED,
        quality_status=QualityStatus.COMPLETE,
    )
    return tuple(
        AdjustedSeriesPoint(
            instrument_id=IID,
            point_date=day,
            price_basis=basis,
            adj_factor=factor,
            evidence=evidence,
            source_code=CODE,
            source_trade_date=day,
        )
        for day, factor in zip(days, factors)
    )


class FactorNormalizationTestCase(unittest.TestCase):
    def test_source_date_is_normalized_and_retained(self) -> None:
        normalized = normalize_adjustment_factor(
            _row("20260821", "1.125"),
            instrument_id=IID,
            source=SOURCE,
            expected_source_code=CODE,
            cutoff=date(2026, 8, 21),
        )
        self.assertEqual(normalized.source_trade_date, date(2026, 8, 21))
        self.assertEqual(normalized.effective_date, date(2026, 8, 21))
        self.assertEqual(normalized.adj_factor, Decimal("1.125"))
        self.assertEqual(normalized.point_date, normalized.effective_date)

    def test_invalid_identity_factor_and_cutoff_rows_fail_closed(self) -> None:
        cases = (
            _row("20260821", "0"),
            _row("20260821", "NaN"),
            _row("20260821", "1", ts_code="WRONG.SH"),
            _row("20260822", "1"),
        )
        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ProviderContractViolationError):
                    normalize_adjustment_factor(
                        row,
                        instrument_id=IID,
                        source=SOURCE,
                        expected_source_code=CODE,
                        cutoff=date(2026, 8, 21),
                    )

    def test_duplicate_or_incomplete_batch_does_not_shorten_sequence(self) -> None:
        with self.assertRaises(HistoryBarsDuplicateError):
            normalize_adjustment_factors(
                (_row("20260821", "1"), _row("20260821", "1.1")),
                instrument_id=IID,
                source=SOURCE,
                expected_source_code=CODE,
            )
        with self.assertRaises(HistoryBarsIncompleteError):
            normalize_adjustment_factors(
                (_row("20260821", "1"),),
                instrument_id=IID,
                source=SOURCE,
                expected_source_code=CODE,
                expected_dates=(date(2026, 8, 21), date(2026, 8, 22)),
            )

    def test_cutoff_uses_market_local_date(self) -> None:
        instant = datetime(2026, 8, 21, 23, 30, tzinfo=UTC)
        self.assertEqual(
            cutoff_local_date(instant, "Asia/Shanghai"), date(2026, 8, 22)
        )


class ResearchPriceSeriesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.days = (date(2026, 8, 20), date(2026, 8, 21))
        self.bars = _bars(self.days)

    def _build(self, basis: PriceBasis):
        return build_research_price_series(
            self.bars,
            _points(self.days, ("1", "2"), basis),
            price_basis=basis,
            formula=(
                "tushare_qfq_native_v1"
                if basis is PriceBasis.QFQ
                else "tushare_hfq_native_v1"
            ),
            anchor=(
                "latest-visible-close"
                if basis is PriceBasis.QFQ
                else "first-visible-close"
            ),
            precision=2,
            rounding="source-declared-half-up",
            policy_key="tushare_adj_factor_native",
            policy_version=1,
        )

    def test_qfq_and_hfq_are_separate_price_bases_and_do_not_mutate_raw(self) -> None:
        qfq = self._build(PriceBasis.QFQ)
        hfq = self._build(PriceBasis.HFQ)
        self.assertEqual([row.price_basis for row in qfq], [PriceBasis.QFQ] * 2)
        self.assertEqual([row.price_basis for row in hfq], [PriceBasis.HFQ] * 2)
        self.assertEqual(qfq[0].close, Decimal("5.50"))
        self.assertEqual(hfq[0].close, Decimal("11.00"))
        self.assertNotEqual(qfq[0].close, hfq[0].close)
        self.assertEqual(self.bars[0].close, Decimal("11"))
        self.assertIs(self.bars[0].price_basis, PriceBasis.RAW)

    def test_missing_factor_and_cross_basis_factor_are_blocked(self) -> None:
        with self.assertRaises(HistoryBarsIncompleteError):
            build_research_price_series(
                self.bars,
                _points((self.days[0],), ("1",), PriceBasis.QFQ),
                price_basis=PriceBasis.QFQ,
                formula="tushare_qfq_native_v1",
                anchor="latest-visible-close",
                precision=2,
                rounding="half-up",
            )

    def test_duplicate_raw_bar_dates_are_blocked(self) -> None:
        with self.assertRaises(HistoryBarsDuplicateError):
            build_research_price_series(
                self.bars + (self.bars[-1],),
                _points(self.days, ("1", "2"), PriceBasis.QFQ),
                price_basis=PriceBasis.QFQ,
                formula="tushare_qfq_native_v1",
                anchor="latest-visible-close",
                precision=2,
                rounding="half-up",
            )
        with self.assertRaises(ProviderContractViolationError):
            build_research_price_series(
                self.bars,
                _points(self.days, ("1", "2"), PriceBasis.QFQ),
                price_basis=PriceBasis.HFQ,
                formula="tushare_hfq_native_v1",
                anchor="first-visible-close",
                precision=2,
                rounding="half-up",
            )

    def test_missing_native_semantics_do_not_fallback_to_another_basis(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            build_research_price_series(
                self.bars,
                _points(self.days, ("1", "2"), PriceBasis.QFQ),
                price_basis=PriceBasis.QFQ,
                formula=None,
                anchor="latest-visible-close",
                precision=2,
                rounding="half-up",
            )


if __name__ == "__main__":
    unittest.main()
