"""Tests for the strategy data-query contract and its boundaries."""

import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from inspect import signature
from uuid import uuid4

from app.backtesting.data.errors import HistoryIncompleteError
from app.strategy_protocol.contract import (
    MAX_LOOKBACK_SESSIONS,
    AdjustmentNotActiveError,
    DataCutoffViolationError,
    IdentityMappingMissingError,
    IncompleteHistoryError,
    InvalidProviderResultError,
    LookbackLimitExceededError,
)
from app.strategy_protocol.data_view import (
    AdjustmentBasis,
    AdjustmentPolicyGate,
    AdjustedSeriesPointDTO,
    BarDTO,
    InstrumentCandidateDTO,
    PitSegment,
    StrategyDataDTO,
    UniverseQueryDTO,
    stitch_segmented_history,
)

AWARE_CUTOFF = datetime(2026, 8, 21, 15, 0, 0, tzinfo=timezone(timedelta(hours=8)))
INSTRUMENT_ID = uuid4()


class _CountingView:
    """Read side that records whether any read actually happened."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.reads = 0

    def bars(self, instrument_id, *, start_date, end_date, lookback_sessions):
        self.reads += 1
        selected = [
            row
            for row in self.rows
            if (start_date is None or row.trade_date >= start_date)
            and (end_date is None or row.trade_date <= end_date)
        ]
        if lookback_sessions is not None:
            selected = selected[-lookback_sessions:]
        return tuple(selected)

    def adjusted_series(
        self, instrument_id, *, start_date, end_date, lookback_sessions, basis
    ):
        self.reads += 1
        if basis is AdjustmentBasis.RAW:
            # Raw series carry no adjustment factors by contract.
            return ()
        return (
            AdjustedSeriesPointDTO(instrument_id=instrument_id, trade_date=day, adj_factor=Decimal("1.1"))
            for day in (date(2026, 8, 20), date(2026, 8, 21))
        )


def _bar(day: date, close: str = "10") -> BarDTO:
    return BarDTO(
        instrument_id=INSTRUMENT_ID,
        trade_date=day,
        values={"close": Decimal(close)},
    )


class DataBoundaryTestCase(unittest.TestCase):
    """Cover cutoff and lookback enforcement before any read happens."""

    def test_query_past_data_cutoff_is_rejected_without_reading(self) -> None:
        view = _CountingView()
        facade = StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF)
        future = AWARE_CUTOFF.date() + timedelta(days=1)
        with self.assertRaises(DataCutoffViolationError) as context:
            facade.bars(INSTRUMENT_ID, end_date=future)
        details = context.exception.details
        self.assertEqual(details["instrument_id"], str(INSTRUMENT_ID))
        self.assertIsNone(details["source"])
        self.assertIsNone(details["source_code"])
        self.assertEqual(details["session_date"], future.isoformat())
        self.assertEqual(details["expected"], "<= 2026-08-21")
        self.assertEqual(details["actual"], future.isoformat())
        self.assertEqual(details["data_cutoff"], AWARE_CUTOFF.isoformat())
        self.assertIsNone(details["fact_version"])
        with self.assertRaises(DataCutoffViolationError):
            facade.bars(INSTRUMENT_ID, start_date=AWARE_CUTOFF.date() + timedelta(days=1))
        self.assertEqual(view.reads, 0)

    def test_lookback_over_512_fails_before_reading(self) -> None:
        view = _CountingView()
        facade = StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF)
        with self.assertRaises(LookbackLimitExceededError) as context:
            facade.bars(INSTRUMENT_ID, lookback_sessions=MAX_LOOKBACK_SESSIONS + 1)
        self.assertEqual(
            context.exception.details["instrument_id"], str(INSTRUMENT_ID)
        )
        self.assertIsNone(context.exception.details["source"])
        self.assertIsNone(context.exception.details["session_date"])
        self.assertEqual(
            context.exception.details["expected"], "<= 512 sessions"
        )
        self.assertEqual(context.exception.details["actual"], 513)
        self.assertEqual(
            context.exception.details["data_cutoff"], AWARE_CUTOFF.isoformat()
        )
        self.assertIsNone(context.exception.details["fact_version"])
        self.assertEqual(view.reads, 0)
        with self.assertRaises(LookbackLimitExceededError):
            facade.adjusted_series(INSTRUMENT_ID, lookback_sessions=513)

    def test_valid_window_within_limits_reads_normally(self) -> None:
        view = _CountingView([_bar(AWARE_CUTOFF.date())])
        facade = StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF)
        bars = facade.bars(INSTRUMENT_ID, lookback_sessions=MAX_LOOKBACK_SESSIONS)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].values["close"], Decimal("10"))

    def test_pit_constructor_exposes_only_canonical_dependency_names(self) -> None:
        parameters = signature(StrategyDataDTO).parameters
        self.assertEqual(
            {
                "resolved_sessions",
                "session_resolver",
                "pit_reader",
                "pit_source",
            },
            {
                name
                for name in parameters
                if name
                in {
                    "resolved_sessions",
                    "session_resolver",
                    "pit_reader",
                    "pit_source",
                    "sessions",
                    "sessions_resolver",
                    "pit_history",
                    "pit_adapter",
                    "source",
                }
            },
        )

    def test_pit_date_ranges_support_each_boundary_shape(self) -> None:
        sessions = tuple(date(2026, 8, day) for day in (18, 19, 20, 21))

        class PitReader:
            source = "provider-source"

            def __init__(self):
                self.resolve_calls = []

            def resolve(self, instrument_id, *, source, sessions, data_cutoff):
                self.resolve_calls.append(
                    (instrument_id, source, tuple(sessions), data_cutoff)
                )
                return tuple(sessions)

            def bars(self, instrument_id, *, resolution):
                return tuple(
                    BarDTO(
                        instrument_id=instrument_id,
                        trade_date=day,
                        values={"close": Decimal("1")},
                    )
                    for day in resolution
                )

        reader = PitReader()
        facade = StrategyDataDTO(
            object(),
            data_cutoff=AWARE_CUTOFF,
            resolved_sessions=sessions,
            pit_reader=reader,
            pit_source="configured-source",
        )
        cases = (
            ({"start_date": date(2026, 8, 20)}, sessions[2:]),
            ({"end_date": date(2026, 8, 19)}, sessions[:2]),
            (
                {"start_date": date(2026, 8, 19), "end_date": date(2026, 8, 20)},
                sessions[1:3],
            ),
        )
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                bars = facade.bars(INSTRUMENT_ID, **kwargs)
                self.assertEqual(
                    tuple(bar.trade_date for bar in bars), expected
                )
        self.assertEqual(len(reader.resolve_calls), len(cases))
        self.assertTrue(
            all(call[1] == "configured-source" for call in reader.resolve_calls)
        )

    def test_pit_lookback_rejects_insufficient_resolved_sessions(self) -> None:
        class PitReader:
            def __init__(self):
                self.resolve_calls = 0
                self.bars_calls = 0

            def resolve(self, instrument_id, *, sessions, data_cutoff):
                self.resolve_calls += 1
                return tuple(sessions)

            def bars(self, instrument_id, *, resolution):
                self.bars_calls += 1
                return ()

        reader = PitReader()
        facade = StrategyDataDTO(
            object(),
            data_cutoff=AWARE_CUTOFF,
            resolved_sessions=(date(2026, 8, 20), date(2026, 8, 21)),
            pit_reader=reader,
        )
        with self.assertRaises(HistoryIncompleteError) as context:
            facade.bars(INSTRUMENT_ID, lookback_sessions=3)
        self.assertEqual(context.exception.code, "history_incomplete")
        self.assertEqual(context.exception.details["requested"], 3)
        self.assertEqual(context.exception.details["available"], 2)
        self.assertEqual(context.exception.details["instrument_id"], str(INSTRUMENT_ID))
        self.assertEqual(reader.resolve_calls, 0)
        self.assertEqual(reader.bars_calls, 0)

    def test_pit_sessions_past_cutoff_are_reported_as_cutoff_violation(self) -> None:
        """A resolved session beyond the cutoff must not leak an indexing error."""

        class PitReader:
            def bars(self, instrument_id, *, resolution):
                self.called = True
                return ()

        reader = PitReader()
        future = AWARE_CUTOFF.date() + timedelta(days=1)
        facade = StrategyDataDTO(
            object(),
            data_cutoff=AWARE_CUTOFF,
            resolved_sessions=(future,),
            pit_reader=reader,
        )

        with self.assertRaises(DataCutoffViolationError) as context:
            facade.bars(INSTRUMENT_ID, lookback_sessions=1)

        details = context.exception.details
        self.assertEqual(details["instrument_id"], str(INSTRUMENT_ID))
        self.assertEqual(details["session_date"], future.isoformat())
        self.assertEqual(details["expected"], "<= 2026-08-21")
        self.assertEqual(details["actual"], future.isoformat())
        self.assertEqual(details["data_cutoff"], AWARE_CUTOFF.isoformat())
        self.assertFalse(hasattr(reader, "called"))

    def test_bar_dto_values_are_read_only_and_float_free(self) -> None:
        bar = _bar(AWARE_CUTOFF.date())
        with self.assertRaises(TypeError):
            bar.values["close"] = Decimal("1")
        # Binary floats, booleans, and non-finite values are rejected.
        for bad in (1.0, True, "NaN", "Infinity", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                BarDTO(
                    instrument_id=INSTRUMENT_ID,
                    trade_date=AWARE_CUTOFF.date(),
                    values={"close": bad},
                )


class ReadOnlyFacadeTestCase(unittest.TestCase):
    """Strategies must not be able to rewrite query conditions."""

    def _facade(self) -> StrategyDataDTO:
        return StrategyDataDTO(_CountingView(), data_cutoff=AWARE_CUTOFF)

    def test_provider_objects_are_not_part_of_the_public_surface(self) -> None:
        facade = self._facade()
        # Plain attribute names are gone entirely; only the name-mangled
        # engine-internal slots remain, and writes stay blocked.
        for name in ("_view", "_data_cutoff", "_max_lookback_sessions",
                     "_adjustment_gate"):
            self.assertFalse(hasattr(facade, name), name)
            with self.assertRaises(AttributeError):
                getattr(facade, name)
        with self.assertRaises(AttributeError):
            setattr(facade, "_StrategyDataDTO__view", _CountingView())

    def test_adjustment_gate_is_read_only(self) -> None:
        gate = AdjustmentPolicyGate.inactive_gate()
        with self.assertRaises(AttributeError):
            gate._active = True
        with self.assertRaises(AttributeError):
            delattr(gate, "_AdjustmentPolicyGate__active")
        self.assertFalse(gate.is_active())

    def test_lookback_cap_cannot_be_raised_by_configuration(self) -> None:
        view = _CountingView()
        with self.assertRaises(ValueError):
            StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF,
                            max_lookback_sessions=10_000)
        with self.assertRaises(ValueError):
            StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF,
                            max_lookback_sessions=MAX_LOOKBACK_SESSIONS + 1)
        with self.assertRaises(ValueError):
            StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF,
                            max_lookback_sessions=0)
        # Lowering the cap is the only configuration allowed.
        lowered = StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF,
                                  max_lookback_sessions=8)
        with self.assertRaises(LookbackLimitExceededError):
            lowered.bars(INSTRUMENT_ID, lookback_sessions=9)

    def test_universe_facade_attributes_are_read_only(self) -> None:
        class _Universe:
            def query(self, *, exchanges=None, asset_classes=None):
                return ()

        facade = UniverseQueryDTO(_Universe())
        with self.assertRaises(AttributeError):
            facade._query = lambda **kwargs: ()
        with self.assertRaises(AttributeError):
            delattr(facade, "_UniverseQueryDTO__query")

    def test_conflicting_lookback_and_start_date_is_rejected(self) -> None:
        view = _CountingView([_bar(day) for day in (
            AWARE_CUTOFF.date() - timedelta(days=offset) for offset in range(10)
        )])
        facade = StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF)
        start = AWARE_CUTOFF.date() - timedelta(days=2)
        # lookback 5 implies a start before the explicit one; the conflict is
        # a caller error instead of silently widening the read.
        with self.assertRaises(ValueError):
            facade.bars(
                INSTRUMENT_ID, start_date=start, lookback_sessions=5
            )
        self.assertEqual(view.reads, 0)

    def test_provider_rows_must_be_immutable_dtos(self) -> None:
        cutoff_day = AWARE_CUTOFF.date()

        class _FakeBar:
            instrument_id = INSTRUMENT_ID
            trade_date = cutoff_day
            values = {"close": []}

        class _FakeProvider(_CountingView):
            def bars(self, instrument_id, *, start_date, end_date, lookback_sessions):
                self.reads += 1
                return (_FakeBar(),)

        with self.assertRaises(InvalidProviderResultError):
            StrategyDataDTO(_FakeProvider(), data_cutoff=AWARE_CUTOFF).bars(
                INSTRUMENT_ID
            )

        class _FakePoint:
            instrument_id = INSTRUMENT_ID
            trade_date = cutoff_day
            adj_factor = 1.5

        class _FakeSeriesProvider(_CountingView):
            def adjusted_series(
                self, instrument_id, *, start_date, end_date, lookback_sessions, basis
            ):
                self.reads += 1
                return (_FakePoint(),)

        with self.assertRaises(InvalidProviderResultError):
            StrategyDataDTO(
                _FakeSeriesProvider(),
                data_cutoff=AWARE_CUTOFF,
                adjustment_gate=AdjustmentPolicyGate.active_gate(),
            ).adjusted_series(INSTRUMENT_ID, basis="qfq")

    def test_lookback_and_explicit_dates_cannot_be_combined(self) -> None:
        view = _CountingView([_bar(AWARE_CUTOFF.date())])
        facade = StrategyDataDTO(view, data_cutoff=AWARE_CUTOFF)
        day = AWARE_CUTOFF.date() - timedelta(days=3)
        with self.assertRaises(ValueError):
            facade.bars(INSTRUMENT_ID, start_date=day, lookback_sessions=5)
        with self.assertRaises(ValueError):
            facade.bars(INSTRUMENT_ID, end_date=day, lookback_sessions=5)
        # Pure lookback and pure explicit ranges both stay valid.
        facade.bars(INSTRUMENT_ID, lookback_sessions=5)
        facade.bars(INSTRUMENT_ID, start_date=day, end_date=AWARE_CUTOFF.date())

    def test_provider_rows_are_revalidated_on_the_way_out(self) -> None:
        cutoff_day = AWARE_CUTOFF.date()

        class _FutureProvider(_CountingView):
            def bars(self, instrument_id, *, start_date, end_date, lookback_sessions):
                self.reads += 1
                return (_bar(cutoff_day + timedelta(days=4), "11"),)

        with self.assertRaises(InvalidProviderResultError):
            StrategyDataDTO(_FutureProvider(), data_cutoff=AWARE_CUTOFF).bars(
                INSTRUMENT_ID
            )

        class _ForeignIdProvider(_CountingView):
            def bars(self, instrument_id, *, start_date, end_date, lookback_sessions):
                self.reads += 1
                return (
                    BarDTO(
                        instrument_id=uuid4(),
                        trade_date=cutoff_day,
                        values={"close": Decimal("1")},
                    ),
                )

        with self.assertRaises(InvalidProviderResultError):
            StrategyDataDTO(_ForeignIdProvider(), data_cutoff=AWARE_CUTOFF).bars(
                INSTRUMENT_ID
            )

        class _UnsortedProvider(_CountingView):
            def bars(self, instrument_id, *, start_date, end_date, lookback_sessions):
                self.reads += 1
                return (
                    _bar(cutoff_day),
                    _bar(cutoff_day - timedelta(days=1)),
                )

        with self.assertRaises(InvalidProviderResultError):
            StrategyDataDTO(_UnsortedProvider(), data_cutoff=AWARE_CUTOFF).bars(
                INSTRUMENT_ID
            )

        class _FutureSeriesProvider(_CountingView):
            def adjusted_series(
                self, instrument_id, *, start_date, end_date, lookback_sessions, basis
            ):
                self.reads += 1
                return (
                    AdjustedSeriesPointDTO(
                        instrument_id=instrument_id,
                        trade_date=cutoff_day + timedelta(days=1),
                        adj_factor=Decimal("1"),
                    ),
                )

        with self.assertRaises(InvalidProviderResultError):
            StrategyDataDTO(
                _FutureSeriesProvider(),
                data_cutoff=AWARE_CUTOFF,
                adjustment_gate=AdjustmentPolicyGate.active_gate(),
            ).adjusted_series(INSTRUMENT_ID, basis="qfq")


class AdjustmentPolicyTestCase(unittest.TestCase):
    """Cover raw/qfq/hfq gating rules."""

    def setUp(self) -> None:
        self.view = _CountingView()

    def test_raw_needs_no_active_policy(self) -> None:
        facade = StrategyDataDTO(
            self.view,
            data_cutoff=AWARE_CUTOFF,
            adjustment_gate=AdjustmentPolicyGate.inactive_gate(),
        )
        points = facade.adjusted_series(INSTRUMENT_ID, basis="raw")
        self.assertEqual(list(points), [])

    def test_qfq_hfq_blocked_until_policy_active(self) -> None:
        facade = StrategyDataDTO(
            self.view,
            data_cutoff=AWARE_CUTOFF,
            adjustment_gate=AdjustmentPolicyGate.inactive_gate(),
        )
        for basis in ("qfq", "hfq"):
            with self.assertRaises(AdjustmentNotActiveError):
                facade.adjusted_series(INSTRUMENT_ID, basis=basis)

    def test_adjusted_series_served_only_with_active_verified_policy(self) -> None:
        gate = AdjustmentPolicyGate.from_policy_key("tushare_adj_factor_native@1")
        facade = StrategyDataDTO(
            self.view, data_cutoff=AWARE_CUTOFF, adjustment_gate=gate
        )
        points = facade.adjusted_series(
            INSTRUMENT_ID,
            end_date=AWARE_CUTOFF.date(),
            basis=AdjustmentBasis.QFQ,
        )
        self.assertTrue(all(point.adj_factor == Decimal("1.1") for point in points))
        # An unknown policy key never activates the gate.
        inactive = AdjustmentPolicyGate.from_policy_key("some_other@9")
        self.assertFalse(inactive.is_active())


class PitStitchingTestCase(unittest.TestCase):
    """Cover cross-code PIT segmentation and blocking semantics."""

    def _segments(self) -> tuple[PitSegment, ...]:
        return (
            PitSegment("OLD.CODE", date(2026, 8, 17), date(2026, 8, 19)),
            PitSegment("NEW.CODE", date(2026, 8, 20), date(2026, 8, 21)),
        )

    def test_segments_stitch_back_to_one_sorted_series(self) -> None:
        sessions = [date(2026, 8, d) for d in (18, 19, 20, 21)]

        def reader(code, start, end):
            days = [day for day in sessions if start <= day <= end]
            return [
                BarDTO(instrument_id=INSTRUMENT_ID, trade_date=day, values={"close": Decimal("1")})
                for day in days
            ]

        stitched = stitch_segmented_history(
            self._segments(), sessions=sessions, read_segment=reader
        )
        self.assertEqual([bar.trade_date for bar in stitched], sessions)
        # All bars carry the same stable identity, not the historical codes.
        self.assertTrue(all(bar.instrument_id == INSTRUMENT_ID for bar in stitched))

    def test_mapping_gap_blocks_instead_of_returning_shorter_window(self) -> None:
        # Session 2026-08-20 is covered by neither segment.
        segments = (
            PitSegment("OLD.CODE", date(2026, 8, 17), date(2026, 8, 19)),
            PitSegment("NEW.CODE", date(2026, 8, 21), date(2026, 8, 21)),
        )

        def reader(code, start, end):
            return [
                BarDTO(instrument_id=INSTRUMENT_ID, trade_date=start, values={"close": Decimal("1")})
            ]

        with self.assertRaises(IdentityMappingMissingError):
            stitch_segmented_history(
                segments,
                sessions=[date(2026, 8, 18), date(2026, 8, 20), date(2026, 8, 21)],
                read_segment=reader,
            )

    def test_overlapping_segments_block_instead_of_duplicating_bars(self) -> None:
        # Both segments claim 2026-08-19; exactly-one coverage must fail
        # before any provider read happens.
        segments = (
            PitSegment("OLD.CODE", date(2026, 8, 17), date(2026, 8, 19)),
            PitSegment("MID.CODE", date(2026, 8, 18), date(2026, 8, 20)),
        )
        reads: list[str] = []

        def reader(code, start, end):
            reads.append(code)
            return []

        with self.assertRaises(IdentityMappingMissingError):
            stitch_segmented_history(
                segments,
                sessions=[date(2026, 8, 18), date(2026, 8, 19)],
                read_segment=reader,
            )
        self.assertEqual(reads, [])

    def test_segment_readers_must_return_real_bar_dtos(self) -> None:
        class _FakeBar:
            trade_date = date(2026, 8, 18)

        with self.assertRaises(InvalidProviderResultError):
            stitch_segmented_history(
                self._segments(),
                sessions=[date(2026, 8, 18)],
                read_segment=lambda code, start, end: [_FakeBar()],
            )

    def test_duplicate_bars_within_one_segment_are_rejected(self) -> None:
        def reader(code, start, end):
            return [
                BarDTO(
                    instrument_id=INSTRUMENT_ID,
                    trade_date=date(2026, 8, 18),
                    values={"close": Decimal("1")},
                ),
                BarDTO(
                    instrument_id=INSTRUMENT_ID,
                    trade_date=date(2026, 8, 18),
                    values={"close": Decimal("2")},
                ),
            ]

        with self.assertRaises(IncompleteHistoryError):
            stitch_segmented_history(
                self._segments(),
                sessions=[date(2026, 8, 18)],
                read_segment=reader,
            )

    def test_out_of_range_segment_bars_are_rejected(self) -> None:
        def reader(code, start, end):
            return [
                BarDTO(
                    instrument_id=INSTRUMENT_ID,
                    trade_date=date(2026, 8, 21),
                    values={"close": Decimal("1")},
                ),
            ]

        with self.assertRaises(IncompleteHistoryError):
            stitch_segmented_history(
                self._segments(),
                sessions=[date(2026, 8, 18)],
                read_segment=reader,
            )

    def test_extra_bars_on_mapped_but_unrequested_days_are_rejected(self) -> None:
        # Session 2026-08-19 is inside the old segment's mapped range but was
        # not requested; the reader must not smuggle it into the result.
        def reader(code, start, end):
            return [
                BarDTO(
                    instrument_id=INSTRUMENT_ID,
                    trade_date=day,
                    values={"close": Decimal("1")},
                )
                for day in (date(2026, 8, 18), date(2026, 8, 19))
            ]

        with self.assertRaises(IncompleteHistoryError):
            stitch_segmented_history(
                self._segments(),
                sessions=[date(2026, 8, 18)],
                read_segment=reader,
            )

    def test_duplicate_or_unsorted_session_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            stitch_segmented_history(
                self._segments(),
                sessions=[date(2026, 8, 19), date(2026, 8, 19)],
                read_segment=lambda code, start, end: [],
            )
        with self.assertRaises(ValueError):
            stitch_segmented_history(
                self._segments(),
                sessions=[date(2026, 8, 21), date(2026, 8, 18)],
                read_segment=lambda code, start, end: [],
            )

    def test_missing_history_bars_block_instead_of_forward_fill(self) -> None:
        sessions = [date(2026, 8, 18), date(2026, 8, 19)]

        def reader(code, start, end):
            # The old code only has the first session; the second is missing.
            return [
                BarDTO(
                    instrument_id=INSTRUMENT_ID,
                    trade_date=date(2026, 8, 18),
                    values={"close": Decimal("1")},
                )
            ]

        with self.assertRaises(IncompleteHistoryError):
            stitch_segmented_history(
                self._segments(), sessions=sessions, read_segment=reader
            )


class UniverseQueryTestCase(unittest.TestCase):
    """Cover candidate identity and immutability."""

    def test_query_returns_read_only_candidates_with_display_identity(self) -> None:
        candidate = InstrumentCandidateDTO(
            instrument_id=uuid4(),
            trading_code="SYN.A",
            name="合成标的 A",
            display_name="Synthetic A",
            asset_class="etf",
            exchange="SSE",
        )

        class _Universe:
            def query(self, *, exchanges=None, asset_classes=None):
                if exchanges is not None and "SSE" not in set(exchanges):
                    return ()
                return (candidate,)

        facade = UniverseQueryDTO(_Universe())
        result = facade.query()
        self.assertIsInstance(result, tuple)
        row = result[0]
        self.assertEqual(row.trading_code, "SYN.A")
        self.assertEqual(row.asset_class, "etf")
        self.assertEqual(row.exchange, "SSE")
        with self.assertRaises(FrozenInstanceError):
            row.name = "改写"
        # Strategy candidates expose only the frozen six-field projection;
        # provider evidence never travels through a hidden metadata mapping.
        self.assertFalse(hasattr(row, "metadata"))
        # Filtering by another exchange returns nothing.
        self.assertEqual(facade.query(exchanges=["SZSE"]), ())

    def test_candidate_has_only_the_frozen_six_fields(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(InstrumentCandidateDTO)),
            (
                "instrument_id",
                "trading_code",
                "name",
                "display_name",
                "asset_class",
                "exchange",
            ),
        )
        candidate = InstrumentCandidateDTO(
            instrument_id=uuid4(),
            trading_code="SYN.A",
            name="合成标的 A",
            display_name="Synthetic A",
            asset_class="etf",
            exchange="SSE",
        )
        self.assertFalse(hasattr(candidate, "metadata"))
        with self.assertRaises(TypeError):
            InstrumentCandidateDTO(
                instrument_id=uuid4(),
                trading_code="SYN.A",
                name="合成标的 A",
                display_name="Synthetic A",
                asset_class="etf",
                exchange="SSE",
                metadata={},  # type: ignore[call-arg]
            )

    def test_candidate_identity_fields_are_validated(self) -> None:
        base = dict(
            trading_code="SYN.A",
            name="合成标的 A",
            display_name="Synthetic A",
            asset_class="etf",
            exchange="SSE",
        )
        with self.assertRaises(ValueError):
            InstrumentCandidateDTO(instrument_id="not-a-uuid", **base)
        with self.assertRaises(ValueError):
            InstrumentCandidateDTO(instrument_id=None, **base)
        for blank_field in ("trading_code", "name", "display_name", "asset_class", "exchange"):
            with self.assertRaises(ValueError, msg=blank_field):
                InstrumentCandidateDTO(instrument_id=uuid4(), **{**base, blank_field: "  "})

        # Scalar subclasses can carry mutable attributes, so only exact
        # strings are accepted at the strategy boundary.
        class _MutableStr(str):
            pass

        with self.assertRaises(ValueError):
            InstrumentCandidateDTO(
                instrument_id=uuid4(),
                **{**base, "name": _MutableStr("别名")},
            )

    def test_universe_provider_results_are_validated_and_sorted(self) -> None:
        first_id = uuid4()
        second_id = uuid4()
        rows = [
            (second_id, "SYN.B"),
            (first_id, "SYN.A"),
        ]

        def make_provider(candidate_rows):
            class _Universe:
                def query(self, *, exchanges=None, asset_classes=None):
                    return tuple(
                        InstrumentCandidateDTO(
                            instrument_id=instrument_id,
                            trading_code=code,
                            name=code,
                            display_name=code,
                            asset_class="etf",
                            exchange="SSE",
                        )
                        for instrument_id, code in candidate_rows
                    )

            return _Universe()

        # Results come back in stable instrument_id order regardless of the
        # provider's ordering.
        result = UniverseQueryDTO(make_provider(rows)).query()
        expected_order = [
            code for _, code in sorted(rows, key=lambda row: str(row[0]))
        ]
        self.assertEqual(
            [row.trading_code for row in result], expected_order
        )

        # Duplicate identities use the task-15 stable projection contract:
        # choose the lexicographically smallest six-field projection, and do
        # so independently of provider input order (QUERY-07/A23).
        duplicate_forward = UniverseQueryDTO(
            make_provider([(first_id, "B"), (first_id, "A")])
        ).query()
        duplicate_reverse = UniverseQueryDTO(
            make_provider([(first_id, "A"), (first_id, "B")])
        ).query()
        self.assertEqual(
            [row.trading_code for row in duplicate_forward], ["A"]
        )
        self.assertEqual(duplicate_forward, duplicate_reverse)

        class _BrokenUniverse:
            def query(self, *, exchanges=None, asset_classes=None):
                return ("not-a-candidate",)

        with self.assertRaises(InvalidProviderResultError):
            UniverseQueryDTO(_BrokenUniverse()).query()


if __name__ == "__main__":
    unittest.main()
