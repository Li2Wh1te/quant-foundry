"""Tests for PIT code-mapping fixtures and cross-code history (task 03-07).

Covers the acceptance matrix of section 4.7 of the task package:
single-code windows, cross-code boundary behaviour, identity preservation,
mapping gaps (leading / interior / trailing), overlaps, ``known_at``
visibility past the cutoff, the 512/513 lookback boundary, missing /
duplicate / wrong-identity bars, cross-code adjustment-factor
completeness, and the impossibility of querying history through the
current code alone.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import unittest
from uuid import uuid4

from app.backtesting.data.errors import (
    DataContractError,
    DataCutoffExceededError,
    HistoryBarInstrumentMismatchError,
    HistoryBarsDuplicateError,
    HistoryBarsIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingIncompleteError,
    InvalidDataRequestError,
    LookbackSessionsLimitExceededError,
)
from app.backtesting.data.memory_pit import (
    PITFixtureBarRow,
    PITFixtureFactorRow,
    PITMappingFixture,
)
from app.backtesting.data.requests import (
    DateRange,
    LookbackWindow,
    PriceBasis,
    QueryBoundary,
)
from app.instruments.domain import InstrumentCodeMapping

INSTRUMENT_ID = uuid4()
SOURCE = "tushare"
CUTOFF = datetime(2026, 8, 22, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 8, 21, tzinfo=UTC)
KNOWN_AT = datetime(2025, 12, 1, tzinfo=UTC)


def sessions_from(start: date, count: int) -> list[date]:
    """Consecutive calendar days standing in for trading sessions."""

    return [start + timedelta(days=index) for index in range(count)]


def make_mapping(
    source_code: str,
    valid_from: date,
    valid_to: date | None = None,
    *,
    instrument_id=None,
    known_at: datetime = KNOWN_AT,
    evidence: str = "exchange announcement 2024-001",
) -> InstrumentCodeMapping:
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


def make_bar_row(trade_date: date, source_code: str) -> PITFixtureBarRow:
    return PITFixtureBarRow(
        source_code=source_code,
        trade_date=trade_date,
        open=Decimal("1.000"),
        high=Decimal("1.010"),
        low=Decimal("0.990"),
        close=Decimal("1.005"),
        volume=Decimal("1000"),
        amount=Decimal("1000"),
        observed_at=OBSERVED_AT,
        known_at=KNOWN_AT,
    )


def make_factor_row(point_date: date, source_code: str) -> PITFixtureFactorRow:
    return PITFixtureFactorRow(
        source_code=source_code,
        point_date=point_date,
        adj_factor=Decimal("1.05"),
        observed_at=OBSERVED_AT,
    )


BOUNDARY = QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True)


class FixtureTestCase(unittest.TestCase):
    def build_fixture(self, **kwargs) -> PITMappingFixture:
        defaults = dict(
            instrument_id=INSTRUMENT_ID,
            mappings=(),
            bar_rows=(),
            factor_rows=(),
            clock=CUTOFF,
        )
        defaults.update(kwargs)
        return PITMappingFixture(**defaults)


class CrossCodeHistoryTestCase(FixtureTestCase):
    """Matrix items 1-3: single code, code boundary, identity stability."""

    def setUp(self) -> None:
        # AAA.SH covers [2024-01-01, 2025-01-03), BBB.SH from 2025-01-03 on:
        # 2025-01-02 is the last AAA.SH session inside these fixtures.
        self.days = [date(2025, 1, 2), date(2025, 1, 3)]
        self.fixture = self.build_fixture(
            mappings=[
                make_mapping("AAA.SH", date(2024, 1, 1), date(2025, 1, 3)),
                make_mapping("BBB.SH", date(2025, 1, 3)),
            ],
            bar_rows=[
                make_bar_row(date(2025, 1, 2), "AAA.SH"),
                make_bar_row(date(2025, 1, 3), "BBB.SH"),
                make_bar_row(date(2025, 1, 6), "BBB.SH"),
            ],
        )

    def test_single_code_full_window_binds_every_session(self) -> None:
        days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
        history = self.fixture.bars(
            sessions=days,
            window=DateRange(
                start_date=date(2025, 1, 2), end_date=date(2025, 1, 6)
            ),
            boundary=BOUNDARY,
            data_cutoff=CUTOFF,
        )
        self.assertEqual(
            [bar.trade_date for bar in history.bars],
            days,
        )
        self.assertTrue(
            all(bar.instrument_id == INSTRUMENT_ID for bar in history.bars)
        )
        self.assertEqual(
            history.resolution.session_bindings,
            {
                date(2025, 1, 2): "AAA.SH",
                date(2025, 1, 3): "BBB.SH",
                date(2025, 1, 6): "BBB.SH",
            },
        )

    def test_code_boundary_splits_old_and_new_source_codes(self) -> None:
        window_days = [
            date(2024, 12, 31),
            date(2025, 1, 1),
            date(2025, 1, 2),
            date(2025, 1, 3),
        ]
        fixture = self.build_fixture(
            mappings=self.fixture.mappings,
            bar_rows=[make_bar_row(day, "AAA.SH") for day in window_days[:3]]
            + [make_bar_row(day, "BBB.SH") for day in window_days[3:]],
        )
        history = fixture.bars(
            sessions=window_days,
            window=DateRange(start_date=date(2024, 12, 31), end_date=date(2025, 1, 3)),
            boundary=QueryBoundary(
                data_cutoff=CUTOFF, include_cutoff_day=True
            ),
            data_cutoff=CUTOFF,
        )
        bindings = history.resolution.session_bindings
        self.assertEqual(bindings[date(2024, 12, 31)], "AAA.SH")
        self.assertEqual(bindings[date(2025, 1, 1)], "AAA.SH")
        self.assertEqual(bindings[date(2025, 1, 2)], "AAA.SH")
        self.assertEqual(bindings[date(2025, 1, 3)], "BBB.SH")

    def test_cross_code_merge_keeps_stable_identity(self) -> None:
        window_days = [date(2025, 1, 2), date(2025, 1, 3)]
        history = self.fixture.bars(
            sessions=window_days,
            window=DateRange(start_date=date(2025, 1, 2), end_date=date(2025, 1, 3)),
            boundary=QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(len(history.bars), 2)
        self.assertEqual({bar.instrument_id for bar in history.bars}, {INSTRUMENT_ID})


class MappingGapAndConflictTestCase(FixtureTestCase):
    """Matrix items 4-6: gaps, overlaps, future knowledge."""

    def make_covering_fixture(self, mappings, *, missing_day: date | None = None):
        all_days = sessions_from(date(2025, 1, 2), 3)
        rows = []
        resolution_mapping = {all_days[0]: "OLD.CODE"}
        if len(mappings) > 1:
            resolution_mapping[all_days[1]] = "NEW.CODE"
            resolution_mapping[all_days[2]] = "NEW.CODE"
        else:
            resolution_mapping[all_days[1]] = "OLD.CODE"
            resolution_mapping[all_days[2]] = "OLD.CODE"
        for day in all_days:
            if day == missing_day:
                continue
            rows.append(make_bar_row(day, resolution_mapping[day]))
        return self.build_fixture(mappings=mappings, bar_rows=rows)

    def test_leading_gap_blocks_before_reads(self) -> None:
        # First session has no mapping at all.
        mappings = [make_mapping("OLD.CODE", date(2025, 1, 3))]
        fixture = self.make_covering_fixture(mappings)
        with self.assertRaises(IdentityMappingIncompleteError):
            fixture.bars(
                sessions=sessions_from(date(2025, 1, 2), 3),
                window=DateRange(
                    start_date=date(2025, 1, 2), end_date=date(2025, 1, 4)
                ),
                boundary=BOUNDARY,
                data_cutoff=CUTOFF,
            )

    def test_interior_and_trailing_gaps_block(self) -> None:
        # Mapping ends after the first session; later sessions uncovered.
        mappings = [make_mapping("OLD.CODE", date(2025, 1, 2), date(2025, 1, 3))]
        fixture = self.make_covering_fixture(mappings, missing_day=date(2025, 1, 3))
        for window_end in (date(2025, 1, 4), date(2025, 1, 5)):
            with self.assertRaises(IdentityMappingIncompleteError):
                fixture.bars(
                    sessions=sessions_from(date(2025, 1, 2), 3),
                    window=DateRange(
                        start_date=date(2025, 1, 2), end_date=window_end
                    ),
                    boundary=QueryBoundary(
                        data_cutoff=datetime(2026, 8, 22, tzinfo=UTC),
                        include_cutoff_day=True,
                    ),
                    data_cutoff=datetime(2026, 8, 22, tzinfo=UTC),
                )
                break

    def test_mapping_overlap_conflicts(self) -> None:
        overlapping = [
            make_mapping("AAA.SH", date(2024, 1, 1), date(2025, 6, 1)),
            make_mapping("BBB.SH", date(2025, 1, 2)),
        ]
        fixture = self.build_fixture(
            mappings=overlapping,
            bar_rows=[make_bar_row(day, "AAA.SH") for day in sessions_from(date(2025, 1, 2), 2)],
        )
        with self.assertRaises(IdentityMappingConflictError):
            fixture.bars(
                sessions=sessions_from(date(2025, 1, 2), 2),
                window=DateRange(
                    start_date=date(2025, 1, 2), end_date=date(2025, 1, 3)
                ),
                boundary=BOUNDARY,
                data_cutoff=CUTOFF,
            )

    def test_known_at_after_cutoff_is_invisible(self) -> None:
        # BBB.SH was learned only on 2025-01-10; the query cutoff is earlier.
        late_cutoff = datetime(2025, 1, 5, tzinfo=UTC)
        mappings = [
            make_mapping("AAA.SH", date(2024, 1, 1), date(2025, 1, 2)),
            make_mapping(
                "BBB.SH",
                date(2025, 1, 2),
                known_at=datetime(2025, 1, 10, tzinfo=UTC),
            ),
        ]
        fixture = self.build_fixture(
            mappings=mappings,
            bar_rows=[
                make_bar_row(date(2025, 1, 2), "AAA.SH"),
                make_bar_row(date(2025, 1, 3), "BBB.SH"),
            ],
        )
        with self.assertRaises(IdentityMappingIncompleteError):
            fixture.bars(
                sessions=[date(2025, 1, 2), date(2025, 1, 3)],
                window=DateRange(
                    start_date=date(2025, 1, 2), end_date=date(2025, 1, 3)
                ),
                boundary=QueryBoundary(data_cutoff=late_cutoff, include_cutoff_day=True),
                data_cutoff=late_cutoff,
            )


class LookbackBoundaryTestCase(FixtureTestCase):
    """Matrix items 7-8: the 512/513 lookback boundary."""

    def setUp(self) -> None:
        self.days = sessions_from(date(2020, 1, 1), 600)
        self.mapping = make_mapping("ONE.CODE", date(2019, 1, 1))
        self.fixture = self.build_fixture(
            mappings=[self.mapping],
            bar_rows=[make_bar_row(day, "ONE.CODE") for day in self.days],
        )

    def test_lookback_512_succeeds_with_complete_coverage(self) -> None:
        end_at = CUTOFF
        window = LookbackWindow(sessions=512, end_at=end_at)
        history = self.fixture.bars(
            sessions=self.days,
            window=window,
            boundary=BOUNDARY,
            data_cutoff=CUTOFF,
        )
        self.assertEqual(len(history.bars), 512)

    def test_lookback_513_fails_before_any_read(self) -> None:
        # The cap lives in the query object itself, so an over-limit
        # request can never reach the segment readers: construction fails
        # first and the fixture's read audit trail stays empty.
        with self.assertRaises(LookbackSessionsLimitExceededError):
            LookbackWindow(sessions=513, end_at=CUTOFF)
        self.assertEqual(self.fixture.read_calls, [])

    def test_lookback_reads_touch_each_source_code_once_per_segment(self) -> None:
        # Sanity for the audit trail used above: a full 512 lookback over
        # one mapping reads exactly one source-code segment.
        window = LookbackWindow(sessions=512, end_at=CUTOFF)
        self.fixture.bars(
            sessions=self.days,
            window=window,
            boundary=QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(self.fixture.read_calls, [("bars", "ONE.CODE")])

    def test_lookback_512_with_one_missing_bar_blocks_without_shortening(self) -> None:
        from app.backtesting.data.requests import DateRange, LookbackWindow

        # Drop exactly one bar inside the final 512-session window.
        full_rows = [make_bar_row(day, "ONE.CODE") for day in self.days]
        dropped = full_rows[-1].trade_date
        fixture = self.build_fixture(
            mappings=[self.mapping],
            bar_rows=[row for row in full_rows if row.trade_date != dropped],
        )
        window = DateRange(start_date=self.days[-512], end_date=self.days[-1])
        with self.assertRaises(HistoryBarsIncompleteError):
            fixture.bars(
                sessions=self.days[-512:],
                window=window,
                boundary=BOUNDARY,
                data_cutoff=CUTOFF,
            )


class BarIntegrityTestCase(FixtureTestCase):
    """Matrix item 9: missing, duplicate, and wrong-identity bars."""

    def setUp(self) -> None:
        self.days = sessions_from(date(2025, 1, 2), 3)
        self.mappings = [make_mapping("ONE.CODE", date(2025, 1, 1))]

    def build(self, bar_rows):
        return self.build_fixture(mappings=self.mappings, bar_rows=bar_rows)

    def window(self):
        return DateRange(start_date=self.days[0], end_date=self.days[-1])

    def test_missing_bar_blocks(self) -> None:
        fixture = self.build([make_bar_row(day, "ONE.CODE") for day in self.days[:2]])
        with self.assertRaises(HistoryBarsIncompleteError):
            fixture.bars(
                sessions=self.days, window=self.window(), boundary=BOUNDARY,
                data_cutoff=CUTOFF,
            )

    def test_duplicate_bar_blocks(self) -> None:
        rows = [make_bar_row(day, "ONE.CODE") for day in self.days]
        rows.append(make_bar_row(self.days[0], "ONE.CODE"))
        fixture = self.build(rows)
        with self.assertRaises(HistoryBarsDuplicateError):
            fixture.bars(
                sessions=self.days, window=self.window(), boundary=BOUNDARY,
                data_cutoff=CUTOFF,
            )

    def test_wrong_identity_bar_blocks(self) -> None:
        # A row whose projected identity differs from the queried one is
        # modelled by pointing the mapping at another instrument while the
        # reader projects under that other id; the stitched result must be
        # rejected by the identity check inside read_segmented_history.
        other_id = uuid4()
        foreign_mappings = [make_mapping("ONE.CODE", date(2025, 1, 1), instrument_id=other_id)]
        foreign_fixture = self.build_fixture(
            instrument_id=other_id,
            mappings=foreign_mappings,
            bar_rows=[make_bar_row(day, "ONE.CODE") for day in self.days],
        )
        history = foreign_fixture.read_history(
            foreign_fixture.resolve(sessions=self.days, data_cutoff=CUTOFF)
        )
        self.assertEqual({bar.instrument_id for bar in history.bars}, {other_id})
        # And the same segments served into the real fixture's read path
        # must fail the identity check.
        from app.backtesting.data.pit_history import resolve_pit_mappings

        resolution = resolve_pit_mappings(
            INSTRUMENT_ID,
            source=SOURCE,
            sessions=self.days,
            mappings=[make_mapping("ONE.CODE", date(2025, 1, 1))],
            data_cutoff=CUTOFF,
        )

        class ForeignReader:
            def __init__(self, inner) -> None:
                self._inner = inner

            def read_bars(self, source_code, start_date, end_date):
                return self._inner.read_bars(source_code, start_date, end_date)

        from app.backtesting.data.pit_history import read_segmented_history

        with self.assertRaises(HistoryBarInstrumentMismatchError):
            read_segmented_history(
                resolution,
                ForeignReader(foreign_fixture._SegmentBarReader(foreign_fixture, "1d")),
            )


class AdjustedSeriesCompletenessTestCase(FixtureTestCase):
    """Matrix item 10: cross-code adjustment-factor completeness."""

    def setUp(self) -> None:
        self.days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
        self.mappings = [
            make_mapping("AAA.SH", date(2024, 1, 1), date(2025, 1, 3)),
            make_mapping("BBB.SH", date(2025, 1, 3)),
        ]

    def window(self):
        return DateRange(start_date=self.days[0], end_date=self.days[-1])

    def test_cross_code_factors_read_per_segment(self) -> None:
        fixture = self.build_fixture(
            mappings=self.mappings,
            factor_rows=[
                make_factor_row(self.days[0], "AAA.SH"),
                make_factor_row(self.days[1], "BBB.SH"),
                make_factor_row(self.days[2], "BBB.SH"),
            ],
        )
        series = fixture.adjusted_series(
            sessions=self.days,
            window=self.window(),
            boundary=BOUNDARY,
            data_cutoff=CUTOFF,
            price_basis=PriceBasis.QFQ,
        )
        self.assertEqual([point.point_date for point in series.points], self.days)
        self.assertEqual({point.instrument_id for point in series.points}, {INSTRUMENT_ID})

    def test_missing_factor_in_one_segment_blocks(self) -> None:
        fixture = self.build_fixture(
            mappings=self.mappings,
            factor_rows=[
                make_factor_row(self.days[0], "AAA.SH"),
                make_factor_row(self.days[1], "BBB.SH"),
                # BBB.SH factor for the last session is missing.
            ],
        )
        with self.assertRaises(HistoryBarsIncompleteError):
            fixture.adjusted_series(
                sessions=self.days,
                window=self.window(),
                boundary=BOUNDARY,
                data_cutoff=CUTOFF,
                price_basis=PriceBasis.QFQ,
            )

    def test_wrong_basis_factors_are_not_substituted(self) -> None:
        fixture = self.build_fixture(
            mappings=self.mappings,
            factor_rows=[
                make_factor_row(day, "AAA.SH" if day < date(2025, 1, 3) else "BBB.SH")
                for day in self.days
            ],
        )
        # No HFQ factors exist anywhere: the query must block rather than
        # silently fall back to QFQ rows.
        with self.assertRaises(HistoryBarsIncompleteError):
            fixture.adjusted_series(
                sessions=self.days,
                window=self.window(),
                boundary=BOUNDARY,
                data_cutoff=CUTOFF,
                price_basis=PriceBasis.HFQ,
            )


class NoReverseLookupTestCase(FixtureTestCase):
    """Matrix item 11: history is unreachable through the current code."""

    def test_current_code_cannot_fill_history_outside_its_mapping(self) -> None:
        # BBB.SH exists today; its bars exist for old dates too, but the
        # mapping says those dates belonged to AAA.SH. The current record
        # must never be used to back-fill the pre-change window.
        early_days = sessions_from(date(2024, 12, 30), 2)
        change_day = date(2025, 1, 2)
        fixture = self.build_fixture(
            mappings=[
                make_mapping("AAA.SH", date(2024, 1, 1), change_day),
                make_mapping("BBB.SH", change_day),
            ],
            bar_rows=[make_bar_row(day, "BBB.SH") for day in early_days]
            + [make_bar_row(change_day, "BBB.SH")],
        )
        with self.assertRaises((HistoryBarsIncompleteError, IdentityMappingIncompleteError)):
            fixture.bars(
                sessions=[*early_days, change_day],
                window=DateRange(start_date=early_days[0], end_date=change_day),
                boundary=QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True),
                data_cutoff=CUTOFF,
            )

    def test_querying_sessions_past_the_cutoff_fails(self) -> None:
        fixture = self.build_fixture(
            mappings=[make_mapping("BBB.SH", date(2024, 1, 1))],
            bar_rows=[make_bar_row(day, "BBB.SH") for day in sessions_from(date(2026, 8, 20), 5)],
        )
        with self.assertRaises(DataCutoffExceededError):
            fixture.bars(
                sessions=[date(2026, 8, 23)],
                window=DateRange(start_date=date(2026, 8, 23), end_date=date(2026, 8, 23)),
                boundary=QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True),
                data_cutoff=CUTOFF,
            )


class BoundaryEnforcementTestCase(FixtureTestCase):
    """The fixture must enforce QueryBoundary cutoff rules, not trim."""

    def setUp(self) -> None:
        self.days = sessions_from(date(2026, 8, 18), 5)
        self.fixture = self.build_fixture(
            mappings=[make_mapping("ONE.CODE", date(2026, 1, 1))],
            bar_rows=[make_bar_row(day, "ONE.CODE") for day in self.days],
        )

    def window(self, end):
        return DateRange(start_date=self.days[0], end_date=end)

    def test_cutoff_day_without_completion_proof_fails(self) -> None:
        # include_cutoff_day=False: the cutoff day is incomplete, so a
        # window touching it must fail instead of being silently trimmed.
        boundary = QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=False)
        with self.assertRaises(DataCutoffExceededError):
            self.fixture.bars(
                sessions=self.days,
                window=self.window(CUTOFF.date()),
                boundary=boundary,
                data_cutoff=CUTOFF,
            )

    def test_lookback_end_at_past_cutoff_fails(self) -> None:
        late_end = datetime(2026, 8, 25, tzinfo=UTC)
        with self.assertRaises(DataCutoffExceededError):
            self.fixture.bars(
                sessions=self.days,
                window=LookbackWindow(sessions=2, end_at=late_end),
                boundary=QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True),
                data_cutoff=CUTOFF,
            )

    def test_mismatched_boundary_and_query_cutoffs_fail(self) -> None:
        other_cutoff = datetime(2026, 8, 20, tzinfo=UTC)
        with self.assertRaises(InvalidDataRequestError):
            self.fixture.bars(
                sessions=self.days,
                window=self.window(self.days[-1]),
                boundary=QueryBoundary(
                    data_cutoff=other_cutoff, include_cutoff_day=True
                ),
                data_cutoff=CUTOFF,
            )
        with self.assertRaises(InvalidDataRequestError):
            self.fixture.adjusted_series(
                sessions=self.days,
                window=self.window(self.days[-1]),
                boundary=QueryBoundary(
                    data_cutoff=CUTOFF, include_cutoff_day=True
                ),
                data_cutoff=other_cutoff,
                price_basis=PriceBasis.QFQ,
            )

    def test_proven_cutoff_day_still_reads(self) -> None:
        # The happy path is unchanged: with the completion proof the
        # cutoff day participates and the full window returns.
        history = self.fixture.bars(
            sessions=self.days,
            window=self.window(CUTOFF.date()),
            boundary=QueryBoundary(data_cutoff=CUTOFF, include_cutoff_day=True),
            data_cutoff=CUTOFF,
        )
        self.assertEqual(len(history.bars), len(self.days))


class FixtureSourceAndFrequencyTestCase(FixtureTestCase):
    """Evidence provenance follows the fixture source; frequency is strict."""

    def setUp(self) -> None:
        self.days = [date(2026, 8, 19), date(2026, 8, 20)]
        custom_source = "custom"
        self.mappings = [
            InstrumentCodeMapping(
                instrument_id=INSTRUMENT_ID,
                source=custom_source,
                source_code="ONE.CODE",
                trading_code="510300",
                valid_from=date(2026, 1, 1),
                mapping_source="exchange_announcement",
                evidence="exchange announcement 2024-001",
                known_at=KNOWN_AT,
                observed_at=KNOWN_AT,
            )
        ]
        self.fixture = self.build_fixture(
            source=custom_source,
            mappings=self.mappings,
            bar_rows=[make_bar_row(day, "ONE.CODE") for day in self.days],
            factor_rows=[make_factor_row(day, "ONE.CODE") for day in self.days],
        )

    def test_custom_source_flows_into_fact_evidence(self) -> None:
        history = self.fixture.bars(
            sessions=self.days,
            window=DateRange(start_date=self.days[0], end_date=self.days[-1]),
            boundary=BOUNDARY,
            data_cutoff=CUTOFF,
        )
        self.assertTrue(
            all(bar.evidence.source == "custom" for bar in history.bars)
        )
        series = self.fixture.adjusted_series(
            sessions=self.days,
            window=DateRange(start_date=self.days[0], end_date=self.days[-1]),
            boundary=BOUNDARY,
            data_cutoff=CUTOFF,
            price_basis=PriceBasis.QFQ,
        )
        self.assertTrue(
            all(point.evidence.source == "custom" for point in series.points)
        )

    def test_non_daily_frequency_is_rejected(self) -> None:
        with self.assertRaises(InvalidDataRequestError):
            self.fixture.bars(
                sessions=self.days,
                window=DateRange(start_date=self.days[0], end_date=self.days[-1]),
                boundary=BOUNDARY,
                data_cutoff=CUTOFF,
                frequency="5m",
            )

    def test_read_history_entry_point_enforces_frequency_too(self) -> None:
        # The shared check must cover every public bar entry point, not
        # only the full bars() flow.
        resolution = self.fixture.resolve(
            sessions=self.days, data_cutoff=CUTOFF
        )
        with self.assertRaises(InvalidDataRequestError):
            self.fixture.read_history(resolution, frequency="5m")


if __name__ == "__main__":
    unittest.main()
