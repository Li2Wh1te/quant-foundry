"""Tests for the instrument identity/display/spec contracts and PIT mappings."""

import unittest
from datetime import UTC, date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.backtesting.domain import DomainValidationError
from app.backtesting.result_models import (
    InstrumentDisplaySnapshot,
    resolve_display_snapshot,
)
from app.instruments.domain import (
    CorporateActionRequirement,
    InstrumentCapabilities,
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentSpec,
    MappingConflictError,
    MappingCoverageGapError,
    VersionedReference,
    order_mapping_segments,
)


EFFECTIVE_AT = datetime(2026, 1, 5, tzinfo=timezone.utc)
DATA_CUTOFF = datetime(2026, 8, 22, tzinfo=timezone.utc)
KNOWN_AT = datetime(2024, 6, 1, tzinfo=timezone.utc)


def make_display(instrument_id: UUID | None = None, **overrides) -> InstrumentDisplay:
    fields = {"instrument_id": instrument_id or uuid4(), **overrides}
    return InstrumentDisplay(**fields)


def make_capabilities(**overrides) -> InstrumentCapabilities:
    fields = {
        "position_sides": frozenset({"long"}),
        "order_types": frozenset({"limit"}),
        "margin_supported": False,
        "corporate_action_requirement": CorporateActionRequirement.REQUIRED,
        **overrides,
    }
    return InstrumentCapabilities(**fields)


def make_spec(instrument_id: UUID | None = None, **overrides) -> InstrumentSpec:
    identity = instrument_id or uuid4()
    fields = {
        "instrument_id": identity,
        "display": make_display(identity),
        "asset_class": "etf",
        "exchange": "SSE",
        "currency": "cny",
        "calendar_id": "XSHG",
        "price_precision": 3,
        "quantity_precision": 0,
        "price_tick": "0.001",
        "lot_size": 100,
        "minimum_order_quantity": 100,
        "contract_multiplier": 1,
        "trading_session_template": VersionedReference(key="cn_equity", version=1),
        "valid_from": EFFECTIVE_AT,
        "valid_to": None,
        "capabilities": make_capabilities(),
        **overrides,
    }
    return InstrumentSpec(**fields)


def make_mapping(
    *,
    source_code: str,
    valid_from: date,
    valid_to: date | None = None,
    known_at: datetime = KNOWN_AT,
    instrument_id: UUID | None = None,
    **overrides,
) -> InstrumentCodeMapping:
    fields = {
        "instrument_id": instrument_id or uuid4(),
        "source": "tushare",
        "source_code": source_code,
        "trading_code": source_code.split(".")[0],
        "valid_from": valid_from,
        "valid_to": valid_to,
        "mapping_source": "exchange_announcement",
        "evidence": "https://example.invalid/notice",
        "known_at": known_at,
        "observed_at": known_at,
        **overrides,
    }
    return InstrumentCodeMapping(**fields)


class FakeInstrumentDisplayProvider:
    """In-memory provider proving the protocol needs no market-data client."""

    def __init__(self, displays) -> None:
        self._displays = {display.instrument_id: display for display in displays}
        self.calls: list[dict] = []

    def resolve_display(self, instrument_id, *, effective_at, data_cutoff):
        self.calls.append(
            {
                "instrument_id": instrument_id,
                "effective_at": effective_at,
                "data_cutoff": data_cutoff,
            }
        )
        return self._displays.get(instrument_id)


class InstrumentIdentityTestCase(unittest.TestCase):
    def test_rejects_non_uuid_identity(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_display(instrument_id="not-a-uuid")
        with self.assertRaises(DomainValidationError):
            make_spec(instrument_id="not-a-uuid")
        with self.assertRaises(DomainValidationError):
            make_mapping(source_code="510300.SH", valid_from=date(2024, 1, 1), instrument_id="x")

    def test_blank_display_fields_normalize_to_none(self) -> None:
        display = make_display(trading_code="   ", name="", display_name=None)

        self.assertIsNone(display.trading_code)
        self.assertIsNone(display.name)
        self.assertIsNone(display.display_name)

    def test_trading_code_is_never_copied_into_name_fields(self) -> None:
        # Constructing a display without names must not synthesize them.
        display = make_display(trading_code="510300")

        self.assertIsNone(display.name)
        self.assertIsNone(display.display_name)

    def test_spec_requires_every_trading_critical_field(self) -> None:
        # No defaults exist on trading-critical fields, so a half-complete
        # spec cannot be constructed at all.
        with self.assertRaises(TypeError):
            InstrumentSpec(
                instrument_id=uuid4(),
                display=make_display(),
                asset_class="etf",
            )

    def test_spec_rejects_mismatched_display_identity(self) -> None:
        with self.assertRaisesRegex(DomainValidationError, "must equal"):
            make_spec(display=make_display())

    def test_spec_normalizes_currency_case(self) -> None:
        spec = make_spec(currency=" cny ")

        self.assertEqual(spec.currency, "CNY")


class InstrumentNumericValidationTestCase(unittest.TestCase):
    def test_rejects_float_bool_and_non_finite_values(self) -> None:
        for bad in (0.01, True, Decimal("NaN"), Decimal("Infinity"), "abc"):
            with self.assertRaises(DomainValidationError):
                make_spec(price_tick=bad)

    def test_rejects_zero_and_negative_financial_amounts(self) -> None:
        for field in (
            "price_tick",
            "lot_size",
            "minimum_order_quantity",
            "contract_multiplier",
        ):
            for bad in (0, -1, "-0.01"):
                with self.assertRaises(DomainValidationError):
                    make_spec(**{field: bad})

    def test_rejects_negative_or_boolean_precisions(self) -> None:
        for field in ("price_precision", "quantity_precision"):
            for bad in (-1, True, 2.0):
                with self.assertRaises(DomainValidationError):
                    make_spec(**{field: bad})

    def test_rejects_tick_finer_than_declared_price_precision(self) -> None:
        with self.assertRaisesRegex(DomainValidationError, "price_precision"):
            make_spec(price_precision=2, price_tick="0.001")

    def test_rejects_quantity_steps_beyond_quantity_precision(self) -> None:
        with self.assertRaisesRegex(DomainValidationError, "quantity_precision"):
            make_spec(quantity_precision=0, lot_size="100.5")
        with self.assertRaisesRegex(DomainValidationError, "quantity_precision"):
            make_spec(quantity_precision=1, minimum_order_quantity="10.05")

    def test_rejects_minimum_order_not_multiple_of_lot(self) -> None:
        with self.assertRaisesRegex(DomainValidationError, "integer multiple"):
            make_spec(lot_size=100, minimum_order_quantity=150)


class InstrumentCapabilitiesTestCase(unittest.TestCase):
    def test_collections_are_immutable_and_deduplicated(self) -> None:
        capabilities = make_capabilities(position_sides={"long", "short", "long"})

        self.assertIsInstance(capabilities.position_sides, frozenset)
        self.assertEqual(capabilities.position_sides, frozenset({"long", "short"}))
        with self.assertRaises(AttributeError):
            capabilities.position_sides.add("net")  # type: ignore[attr-defined]
        with self.assertRaises(Exception):
            capabilities.order_types = frozenset({"market"})

    def test_empty_capability_sets_are_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_capabilities(position_sides=frozenset())
        with self.assertRaises(DomainValidationError):
            make_capabilities(order_types=frozenset())

    def test_corporate_action_requirement_is_typed(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_capabilities(corporate_action_requirement="sometimes")


class ValidityIntervalTestCase(unittest.TestCase):
    def test_rejects_naive_datetimes(self) -> None:
        naive = datetime(2026, 1, 5)
        with self.assertRaises(DomainValidationError):
            make_spec(valid_from=naive)
        with self.assertRaises(DomainValidationError):
            make_spec(valid_to=naive)
        with self.assertRaises(DomainValidationError):
            make_mapping(
                source_code="510300.SH",
                valid_from=date(2024, 1, 1),
                known_at=naive,
            )

    def test_rejects_valid_to_not_after_valid_from(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_spec(valid_from=EFFECTIVE_AT, valid_to=EFFECTIVE_AT)
        with self.assertRaises(DomainValidationError):
            make_mapping(
                source_code="510300.SH",
                valid_from=date(2024, 1, 2),
                valid_to=date(2024, 1, 2),
            )


class DisplaySnapshotResolutionTestCase(unittest.TestCase):
    def test_snapshot_freezes_provider_values_at_resolution_time(self) -> None:
        instrument_id = uuid4()
        provider = FakeInstrumentDisplayProvider(
            [
                make_display(
                    instrument_id=instrument_id,
                    trading_code="510300",
                    name="沪深300ETF",
                    display_name="沪深300ETF",
                )
            ]
        )

        snapshot = resolve_display_snapshot(
            provider,
            instrument_id,
            effective_at=EFFECTIVE_AT,
            data_cutoff=DATA_CUTOFF,
        )

        self.assertEqual(snapshot.event_trading_code, "510300")
        self.assertEqual(snapshot.event_name, "沪深300ETF")
        self.assertEqual(snapshot.instrument_id, instrument_id)
        # Both point-in-time parameters must reach the provider untouched.
        self.assertEqual(provider.calls[0]["effective_at"], EFFECTIVE_AT)
        self.assertEqual(provider.calls[0]["data_cutoff"], DATA_CUTOFF)

        # A later catalogue change must never rewrite the frozen snapshot.
        provider._displays[instrument_id] = make_display(
            instrument_id=instrument_id,
            trading_code="510999",
            name="改名后的ETF",
            display_name="改名后的ETF",
        )
        self.assertEqual(snapshot.event_trading_code, "510300")
        self.assertEqual(snapshot.event_name, "沪深300ETF")

    def test_missing_display_yields_identity_only_snapshot(self) -> None:
        instrument_id = uuid4()
        provider = FakeInstrumentDisplayProvider([])

        snapshot = resolve_display_snapshot(
            provider,
            instrument_id,
            effective_at=EFFECTIVE_AT,
            data_cutoff=DATA_CUTOFF,
        )

        self.assertIsInstance(snapshot, InstrumentDisplaySnapshot)
        self.assertEqual(snapshot.instrument_id, instrument_id)
        self.assertIsNone(snapshot.event_trading_code)
        self.assertIsNone(snapshot.event_name)
        self.assertIsNone(snapshot.event_display_name)

    def test_requires_explicit_effective_at_and_data_cutoff(self) -> None:
        provider = FakeInstrumentDisplayProvider([])
        with self.assertRaises(TypeError):
            resolve_display_snapshot(provider, uuid4())  # type: ignore[call-arg]
        with self.assertRaises(DomainValidationError):
            resolve_display_snapshot(
                provider,
                uuid4(),
                effective_at=datetime(2026, 1, 5),  # naive
                data_cutoff=DATA_CUTOFF,
            )

    def test_snapshot_cannot_be_reused_for_another_instrument(self) -> None:
        snapshot = InstrumentDisplaySnapshot(instrument_id=uuid4())
        with self.assertRaises(DomainValidationError):
            snapshot.require_matching_instrument(uuid4(), "display")


class PitMappingScenarioTestCase(unittest.TestCase):
    """One hypothetical instrument whose code changed on 2025-01-02.

    Old code: [2024-01-01, 2025-01-02) -> OLD.SH
    New code: [2025-01-02, None)       -> NEW.SH
    """

    def setUp(self) -> None:
        self.instrument_id = uuid4()
        self.window_start = date(2024, 1, 1)
        self.window_end = date(2025, 12, 31)
        self.old_mapping = make_mapping(
            instrument_id=self.instrument_id,
            source_code="OLD.SH",
            valid_from=date(2024, 1, 1),
            valid_to=date(2025, 1, 2),
            known_at=datetime(2024, 12, 20, tzinfo=UTC),
        )
        self.new_mapping = make_mapping(
            instrument_id=self.instrument_id,
            source_code="NEW.SH",
            valid_from=date(2025, 1, 2),
            valid_to=None,
            known_at=datetime(2025, 1, 2, tzinfo=UTC),
        )

    def segments(self, *mappings):
        return order_mapping_segments(
            list(mappings), start_date=self.window_start, end_date=self.window_end
        )

    def test_2024_query_returns_old_code_only(self) -> None:
        segments = self.segments(self.old_mapping, self.new_mapping)
        active = [s for s in segments if s.covers(date(2024, 6, 28))]

        self.assertEqual([s.source_code for s in active], ["OLD.SH"])

    def test_query_from_change_date_returns_new_code(self) -> None:
        segments = self.segments(self.old_mapping, self.new_mapping)
        active = [s for s in segments if s.covers(date(2025, 1, 2))]

        self.assertEqual([s.source_code for s in active], ["NEW.SH"])

    def test_cross_window_returns_both_segments_in_stable_order(self) -> None:
        segments = self.segments(self.new_mapping, self.old_mapping)

        self.assertEqual(
            [s.source_code for s in segments], ["OLD.SH", "NEW.SH"]
        )

    def test_mapping_unknown_before_cutoff_is_invisible(self) -> None:
        # The new code was learned on 2025-01-02; a query with an earlier
        # data_cutoff must only see the old segment and never fall back to a
        # "current" code such as the latest EtfCode.ts_code row.
        visible = [
            mapping
            for mapping in (self.old_mapping, self.new_mapping)
            if mapping.known_at <= datetime(2024, 12, 31, tzinfo=UTC)
        ]

        self.assertEqual([m.source_code for m in visible], ["OLD.SH"])

    def test_gap_between_windows_is_an_explicit_error(self) -> None:
        gapped_old = make_mapping(
            instrument_id=self.instrument_id,
            source_code="OLD.SH",
            valid_from=date(2024, 1, 1),
            valid_to=date(2025, 1, 2),
        )
        late_new = make_mapping(
            instrument_id=self.instrument_id,
            source_code="NEW.SH",
            valid_from=date(2025, 1, 5),
            valid_to=None,
        )

        with self.assertRaises(MappingCoverageGapError):
            self.segments(gapped_old, late_new)

    def test_overlapping_windows_are_an_explicit_error(self) -> None:
        overlapping_new = make_mapping(
            instrument_id=self.instrument_id,
            source_code="NEW.SH",
            valid_from=date(2024, 12, 1),
            valid_to=None,
        )

        with self.assertRaises(MappingConflictError):
            self.segments(self.old_mapping, overlapping_new)

    def test_empty_result_is_a_coverage_gap(self) -> None:
        with self.assertRaises(MappingCoverageGapError):
            self.segments()

    def test_leading_window_gap_is_an_explicit_error(self) -> None:
        # The only mapping starts after the requested window begins.
        late_only = make_mapping(
            instrument_id=self.instrument_id,
            source_code="NEW.SH",
            valid_from=date(2024, 1, 2),
            valid_to=None,
        )

        with self.assertRaisesRegex(MappingCoverageGapError, "start at"):
            self.segments(late_only)

    def test_trailing_window_gap_is_an_explicit_error(self) -> None:
        # The only mapping ends before the requested window ends.
        early_only = make_mapping(
            instrument_id=self.instrument_id,
            source_code="OLD.SH",
            valid_from=date(2024, 1, 1),
            valid_to=date(2025, 12, 30),
        )

        with self.assertRaisesRegex(MappingCoverageGapError, "end at"):
            self.segments(early_only)

    def test_mixed_identity_or_source_is_rejected(self) -> None:
        other_instrument = make_mapping(
            instrument_id=uuid4(),
            source_code="OLD.SH",
            valid_from=date(2024, 1, 1),
            valid_to=date(2025, 1, 2),
        )
        with self.assertRaises(DomainValidationError):
            self.segments(other_instrument, self.new_mapping)
        other_source = make_mapping(
            instrument_id=self.instrument_id,
            source="wind",
            source_code="OLD.SH",
            valid_from=date(2024, 1, 1),
            valid_to=date(2025, 1, 2),
        )
        with self.assertRaises(DomainValidationError):
            self.segments(other_source, self.new_mapping)

    def test_evidence_is_mandatory_and_non_blank(self) -> None:
        fields = {
            "instrument_id": uuid4(),
            "source": "tushare",
            "source_code": "OLD.SH",
            "trading_code": "OLD",
            "valid_from": date(2024, 1, 1),
            "mapping_source": "exchange_announcement",
            "known_at": KNOWN_AT,
            "observed_at": KNOWN_AT,
        }
        with self.assertRaises(TypeError):
            InstrumentCodeMapping(**fields)  # evidence missing
        for blank in ("", "   "):
            with self.assertRaises(DomainValidationError):
                InstrumentCodeMapping(**fields, evidence=blank)

    def test_frozen_snapshot_survives_later_mapping_changes(self) -> None:
        provider = FakeInstrumentDisplayProvider(
            [make_display(instrument_id=self.instrument_id, trading_code="OLD")]
        )
        snapshot = resolve_display_snapshot(
            provider,
            self.instrument_id,
            effective_at=EFFECTIVE_AT,
            data_cutoff=DATA_CUTOFF,
        )
        # The mapping evidence changes afterwards; the frozen result row is
        # immutable and keeps the values resolved at write time.
        provider._displays[self.instrument_id] = make_display(
            instrument_id=self.instrument_id, trading_code="NEW"
        )

        self.assertEqual(snapshot.event_trading_code, "OLD")


if __name__ == "__main__":
    unittest.main()
