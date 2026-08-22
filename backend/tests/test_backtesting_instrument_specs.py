"""Tests for the instrument spec contract and its provider protocol."""

import unittest
from datetime import datetime, timezone
from uuid import uuid4

from app.backtesting.domain import DomainValidationError
from app.backtesting.instrument_specs import InstrumentSpec
from app.backtesting.result_models import InstrumentDisplaySnapshot, resolve_display_snapshot


AS_OF = datetime(2026, 1, 5, tzinfo=timezone.utc)


class FakeInstrumentSpecProvider:
    """In-memory provider proving the protocol needs no market-data client."""

    def __init__(self, specs) -> None:
        self._specs = {spec.instrument_id: spec for spec in specs}
        self.resolved_at: list[datetime] = []

    def resolve(self, instrument_id, *, as_of):
        self.resolved_at.append(as_of)
        return self._specs.get(instrument_id)


class InstrumentSpecTestCase(unittest.TestCase):
    def test_rejects_non_uuid_identity(self) -> None:
        with self.assertRaises(DomainValidationError):
            InstrumentSpec(
                instrument_id="not-a-uuid",
                asset_class="etf",
                trading_code="510300",
            )

    def test_rejects_blank_asset_class(self) -> None:
        with self.assertRaises(DomainValidationError):
            InstrumentSpec(instrument_id=uuid4(), asset_class="   ")

    def test_allows_missing_display_fields(self) -> None:
        spec = InstrumentSpec(instrument_id=uuid4(), asset_class="fictional")
        self.assertIsNone(spec.trading_code)
        self.assertIsNone(spec.name)
        self.assertIsNone(spec.display_name)


class DisplaySnapshotResolutionTestCase(unittest.TestCase):
    def test_snapshot_freezes_spec_values_at_resolution_time(self) -> None:
        instrument_id = uuid4()
        spec = InstrumentSpec(
            instrument_id=instrument_id,
            asset_class="etf",
            trading_code="510300",
            name="沪深300ETF",
            display_name="沪深300ETF",
        )
        provider = FakeInstrumentSpecProvider([spec])

        snapshot = resolve_display_snapshot(provider, instrument_id, as_of=AS_OF)

        self.assertEqual(snapshot.event_trading_code, "510300")
        self.assertEqual(snapshot.event_name, "沪深300ETF")
        self.assertEqual(snapshot.instrument_id, instrument_id)

        # A later catalogue change must never rewrite the frozen snapshot.
        renamed = InstrumentSpec(
            instrument_id=instrument_id,
            asset_class="etf",
            trading_code="510999",
            name="改名后的ETF",
            display_name="改名后的ETF",
        )
        provider._specs[instrument_id] = renamed
        self.assertEqual(snapshot.event_trading_code, "510300")
        self.assertEqual(snapshot.event_name, "沪深300ETF")

    def test_missing_spec_yields_identity_only_snapshot(self) -> None:
        instrument_id = uuid4()
        provider = FakeInstrumentSpecProvider([])

        snapshot = resolve_display_snapshot(provider, instrument_id, as_of=AS_OF)

        self.assertIsInstance(snapshot, InstrumentDisplaySnapshot)
        self.assertEqual(snapshot.instrument_id, instrument_id)
        self.assertIsNone(snapshot.event_trading_code)
        self.assertIsNone(snapshot.event_name)
        self.assertIsNone(snapshot.event_display_name)

    def test_snapshot_cannot_be_reused_for_another_instrument(self) -> None:
        snapshot = InstrumentDisplaySnapshot(instrument_id=uuid4())
        with self.assertRaises(DomainValidationError):
            snapshot.require_matching_instrument(uuid4(), "display")


if __name__ == "__main__":
    unittest.main()
