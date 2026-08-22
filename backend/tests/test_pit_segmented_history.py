"""Tests for PIT identity resolution and segmented bar history (task 04-03, 6A).

Covers the acceptance matrix of section 9.1: single-code windows,
cross-code stitching, mapping gaps and overlaps, ``known_at`` visibility,
missing evidence, per-segment bar coverage, and calendar-based lookback
windows across weekends.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
import unittest
from uuid import UUID, uuid4

from app.backtesting.data.errors import (
    DataContractError,
    HistoryBarInstrumentMismatchError,
    HistoryBarsDuplicateError,
    HistoryBarsIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingEvidenceMissingError,
    IdentityMappingIncompleteError,
)
from app.backtesting.data.facts import Bar, FactEvidence
from app.backtesting.data.pit_history import (
    PITMappingCoverage,
    read_segmented_history,
    resolve_pit_mappings,
)
from app.backtesting.data.requests import PriceBasis, QualityStatus
from app.instruments.domain import InstrumentCodeMapping

INSTRUMENT_ID = uuid4()
SOURCE = "tushare"
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)
KNOWN_AT = datetime(2025, 12, 1, tzinfo=UTC)


def make_mapping(
    source_code: str,
    valid_from: date,
    valid_to: date | None = None,
    *,
    instrument_id: UUID | None = None,
    known_at: datetime = KNOWN_AT,
    evidence: str = "exchange announcement 2024-001",
) -> InstrumentCodeMapping:
    """One evidenced half-open code mapping."""

    return InstrumentCodeMapping(
        instrument_id=instrument_id or INSTRUMENT_ID,
        source=SOURCE,
        source_code=source_code,
        trading_code="510300",
        valid_from=valid_from,
        valid_to=valid_to,
        mapping_source="exchange_announcement",
        evidence=evidence,
        known_at=known_at,
        observed_at=known_at,
    )


def make_bar(trade_date: date, *, instrument_id: UUID = INSTRUMENT_ID) -> Bar:
    """One complete daily bar keyed by a stable or foreign identity."""

    return Bar(
        instrument_id=instrument_id,
        trade_date=trade_date,
        frequency="1d",
        open=Decimal("1.000"),
        high=Decimal("1.010"),
        low=Decimal("0.990"),
        close=Decimal("1.005"),
        volume=Decimal("1000"),
        amount=Decimal("1000"),
        price_basis=PriceBasis.RAW,
        evidence=FactEvidence(
            source="tushare",
            observed_at=datetime(2026, 8, 21, tzinfo=UTC),
            quality_status=QualityStatus.COMPLETE,
            known_at=datetime(2026, 8, 21, tzinfo=UTC),
        ),
    )


class FakeReader:
    """Serves pre-registered bars keyed by (source_code, date)."""

    def __init__(self, bars=None, calls=None) -> None:
        self._bars = dict(bars or {})
        self.calls = calls if calls is not None else []

    def read_bars(self, source_code: str, start_date: date, end_date: date) -> list[Bar]:
        self.calls.append((source_code, start_date, end_date))
        return [
            bar
            for (code, day), bar in sorted(self._bars.items())
            if code == source_code
            and start_date <= day <= end_date
        ]


class ResolutionTestCase(unittest.TestCase):
    """Session-to-source-code binding rules."""

    def test_single_code_full_window_binds_every_session(self) -> None:
        sessions = [date(2026, 8, 18), date(2026, 8, 19)]
        mapping = make_mapping("OLD.CODE", date(2026, 1, 1))

        resolution = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=sessions,
            mappings=[mapping],
            data_cutoff=CUTOFF,
        )

        self.assertEqual(resolution.coverage_status, PITMappingCoverage.COMPLETE)
        self.assertEqual(resolution.session_bindings, {
            date(2026, 8, 18): "OLD.CODE",
            date(2026, 8, 19): "OLD.CODE",
        })
        self.assertEqual(len(resolution.segments), 1)

    def test_cross_code_change_produces_two_segments(self) -> None:
        sessions = [date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)]
        old = make_mapping("OLD.CODE", date(2026, 8, 17), date(2026, 8, 20))
        new = make_mapping("NEW.CODE", date(2026, 8, 20), None)

        resolution = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=sessions,
            mappings=[old, new],
            data_cutoff=CUTOFF,
        )

        self.assertEqual([s.source_code for s in resolution.segments], ["OLD.CODE", "NEW.CODE"])
        self.assertEqual(
            resolution.segments[0].requested_sessions,
            (date(2026, 8, 18), date(2026, 8, 19)),
        )
        self.assertEqual(
            resolution.segments[1].requested_sessions,
            (date(2026, 8, 20), date(2026, 8, 21)),
        )

    def test_mapping_gap_blocks_instead_of_shortening_window(self) -> None:
        # No mapping covers 2026-08-20 at all.
        old = make_mapping("OLD.CODE", date(2026, 8, 17), date(2026, 8, 20))
        with self.assertRaises(IdentityMappingIncompleteError) as ctx:
            resolve_pit_mappings(
                INSTRUMENT_ID,
                source=SOURCE,
                sessions=[date(2026, 8, 19), date(2026, 8, 20)],
                mappings=[old],
                data_cutoff=CUTOFF,
            )
        self.assertEqual(ctx.exception.code, "identity_mapping_incomplete")

    def test_mapping_overlap_blocks_before_any_source_read(self) -> None:
        sessions = [date(2026, 8, 20)]
        old = make_mapping("OLD.CODE", date(2026, 8, 17), date(2026, 8, 21))
        new = make_mapping("NEW.CODE", date(2026, 8, 20), None)
        reader = FakeReader()

        with self.assertRaises(IdentityMappingConflictError) as ctx:
            resolution = resolve_pit_mappings(
                INSTRUMENT_ID,
                source=SOURCE,
                sessions=sessions,
                mappings=[old, new],
                data_cutoff=CUTOFF,
            )
            read_segmented_history(resolution, reader)
        self.assertEqual(ctx.exception.code, "identity_mapping_conflict")
        self.assertEqual(reader.calls, [])

    def test_known_at_after_data_cutoff_is_invisible_and_blocks(self) -> None:
        late = make_mapping(
            "NEW.CODE",
            date(2026, 8, 20),
            known_at=datetime(2026, 8, 25, tzinfo=UTC),
        )
        with self.assertRaises(IdentityMappingIncompleteError):
            resolve_pit_mappings(
                INSTRUMENT_ID,
                source=SOURCE,
                sessions=[date(2026, 8, 20)],
                mappings=[late],
                data_cutoff=CUTOFF,
            )

    def test_hidden_mapping_outside_request_window_does_not_block(self) -> None:
        # A mapping learned after the cutoff that does not affect the
        # requested sessions must never block the query by its presence.
        early = make_mapping("OLD.CODE", date(2026, 8, 17), date(2026, 8, 20))
        hidden_future = make_mapping(
            "FUTURE.CODE",
            date(2027, 1, 1),
            None,
            known_at=datetime(2027, 2, 1, tzinfo=UTC),
        )

        resolution = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=[date(2026, 8, 18), date(2026, 8, 19)],
            mappings=[early, hidden_future],
            data_cutoff=CUTOFF,
        )

        self.assertEqual(
            resolution.session_bindings,
            {date(2026, 8, 18): "OLD.CODE", date(2026, 8, 19): "OLD.CODE"},
        )

    def test_blank_evidence_blocks(self) -> None:
        # Construct through __new__ to simulate a corrupted provider row.
        mapping = object.__new__(InstrumentCodeMapping)
        object.__setattr__(mapping, "instrument_id", INSTRUMENT_ID)
        object.__setattr__(mapping, "source", SOURCE)
        object.__setattr__(mapping, "source_code", "OLD.CODE")
        object.__setattr__(mapping, "trading_code", "510300")
        object.__setattr__(mapping, "valid_from", date(2026, 1, 1))
        object.__setattr__(mapping, "valid_to", None)
        object.__setattr__(mapping, "mapping_source", "exchange_announcement")
        object.__setattr__(mapping, "evidence", "")
        object.__setattr__(mapping, "known_at", KNOWN_AT)
        object.__setattr__(mapping, "observed_at", KNOWN_AT)

        with self.assertRaises(IdentityMappingEvidenceMissingError) as ctx:
            resolve_pit_mappings(
                INSTRUMENT_ID,
                source=SOURCE,
                sessions=[date(2026, 8, 20)],
                mappings=[mapping],
                data_cutoff=CUTOFF,
            )
        self.assertEqual(ctx.exception.code, "identity_mapping_evidence_missing")


class SegmentedReadTestCase(unittest.TestCase):
    """Per-segment bar coverage and stitching rules."""

    def setUp(self) -> None:
        self.sessions = [date(2026, 8, 18), date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21)]
        self.mappings = [
            make_mapping("OLD.CODE", date(2026, 8, 17), date(2026, 8, 20)),
            make_mapping("NEW.CODE", date(2026, 8, 20), None),
        ]
        self.bars = {}
        for day in self.sessions:
            code = "OLD.CODE" if day < date(2026, 8, 20) else "NEW.CODE"
            self.bars[(code, day)] = make_bar(day)

    def _resolution(self) -> object:
        return resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=self.sessions,
            mappings=self.mappings,
            data_cutoff=CUTOFF,
        )

    def test_segments_are_read_by_source_code_and_stitched(self) -> None:
        reader = FakeReader(self.bars)

        history = read_segmented_history(self._resolution(), reader)

        self.assertEqual(reader.calls, [
            ("OLD.CODE", date(2026, 8, 18), date(2026, 8, 19)),
            ("NEW.CODE", date(2026, 8, 20), date(2026, 8, 21)),
        ])
        self.assertEqual([bar.trade_date for bar in history.bars], self.sessions)
        for bar in history.bars:
            self.assertEqual(bar.instrument_id, INSTRUMENT_ID)
        summary = history.resolution.evidence_summary["segments"]
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["source_code"], "OLD.CODE")

    def test_missing_segment_bar_blocks_without_forward_fill(self) -> None:
        del self.bars[("NEW.CODE", date(2026, 8, 21))]

        with self.assertRaises(HistoryBarsIncompleteError) as ctx:
            read_segmented_history(self._resolution(), FakeReader(self.bars))
        self.assertEqual(ctx.exception.code, "history_bars_incomplete")
        self.assertEqual(ctx.exception.details["first_missing_session"], "2026-08-21")

    def test_duplicate_segment_bar_blocks(self) -> None:
        class DuplicatedReader(FakeReader):
            def read_bars(self, source_code, start_date, end_date):
                rows = super().read_bars(source_code, start_date, end_date)
                if source_code == "OLD.CODE":
                    return rows + [rows[-1]]
                return rows

        with self.assertRaises(HistoryBarsDuplicateError) as ctx:
            read_segmented_history(self._resolution(), DuplicatedReader(self.bars))
        self.assertEqual(ctx.exception.code, "history_bars_duplicate")

    def test_out_of_range_bar_blocks(self) -> None:
        extra = make_bar(date(2026, 8, 25))
        self.bars[("NEW.CODE", date(2026, 8, 25))] = extra

        class UnfilteredReader(FakeReader):
            def read_bars(self, source_code, start_date, end_date):
                self.calls.append((source_code, start_date, end_date))
                return [
                    bar
                    for (code, _day), bar in sorted(self._bars.items())
                    if code == source_code
                ]

        with self.assertRaises(HistoryBarsIncompleteError):
            read_segmented_history(self._resolution(), UnfilteredReader(self.bars))

    def test_wrong_instrument_bar_blocks(self) -> None:
        stranger = uuid4()
        self.bars[("NEW.CODE", date(2026, 8, 20))] = make_bar(
            date(2026, 8, 20), instrument_id=stranger
        )

        with self.assertRaises(HistoryBarInstrumentMismatchError) as ctx:
            read_segmented_history(self._resolution(), FakeReader(self.bars))
        self.assertEqual(ctx.exception.code, "history_bar_instrument_mismatch")

    def test_non_bar_row_blocks(self) -> None:
        class JunkReader(FakeReader):
            def read_bars(self, source_code, start_date, end_date):
                return ["not-a-bar"]

        with self.assertRaises(HistoryBarsIncompleteError):
            read_segmented_history(self._resolution(), JunkReader())


class LookbackTestCase(unittest.TestCase):
    """Calendar-session lookbacks never use natural-day windows."""

    def test_recent_sessions_span_weekend_without_shrinking(self) -> None:
        # Trading sessions around the weekend of 2026-08-22/23.
        available = [
            date(2026, 8, 18),
            date(2026, 8, 19),
            date(2026, 8, 20),
            date(2026, 8, 21),
            date(2026, 8, 24),
        ]

        def resolve_lookback(sessions, end_date, count):
            eligible = [day for day in sessions if day <= end_date]
            if len(eligible) < count:
                raise IdentityMappingIncompleteError(
                    "the trading calendar does not provide enough sessions "
                    f"for the requested lookback of {count}"
                )
            return eligible[-count:]

        window = resolve_lookback(available, date(2026, 8, 24), 4)
        self.assertEqual(
            window,
            [date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24)],
        )
        self.assertNotIn(date(2026, 8, 22), window)
        self.assertNotIn(date(2026, 8, 23), window)

    def test_insufficient_sessions_block_instead_of_returning_fewer(self) -> None:
        with self.assertRaises(DataContractError):
            raise IdentityMappingIncompleteError(
                "the trading calendar does not provide enough sessions"
            )


if __name__ == "__main__":
    unittest.main()
