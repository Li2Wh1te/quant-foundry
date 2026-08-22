"""Targeted tests for the authoritative data session and warmup (task 02-02).

Covers the ``created -> ready/blocked -> closed`` lifecycle, formal-session
resolution through ``strict_compatible@1``, bounded warmup resolution with
its stable blocking codes, immutability/frozen results, and the warmup
audit fields of the preflight report including hash behaviour.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timezone
from uuid import uuid4

from app.backtesting.calendar_axis import (
    CalendarAxisDifference,
    CalendarAxisDifferenceField,
    CalendarAxisStatus,
    CalendarDefinition,
    CalendarSessionFact,
    InMemoryCalendarAxisDataProvider,
)
from app.backtesting.data import (
    InvalidDataRequestError,
    MAX_LOOKBACK_SESSIONS,
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DataPreflightRequest,
    DataRequest,
    DataSessionClosedError,
    DataSessionState,
    DateRange,
    InstrumentScopeMode,
    IssueSeverity,
    LookbackSessionsLimitExceededError,
    LookbackWindow,
    MarketScope,
    NO_FORMAL_SESSIONS,
    PriceBasis,
    PreflightStatus,
    UnsupportedCapabilityError,
    UniverseQueryPolicy,
    WARMUP_CALENDAR_INCOMPATIBLE,
    WARMUP_COVERAGE_INSUFFICIENT,
    WARMUP_DEFINITION_MISSING,
    WARMUP_FACT_MISSING,
    WARMUP_HISTORY_UNRESOLVED,
    AuthoritativeDataSession,
    CoverageBoundedWarmupSessionResolver,
    WarmupCoverageStatus,
    WarmupResolution,
    WarmupSessionResolver,
    WarmupStatus,
    PreflightIssue,
    resolve_warmup_sessions,
)
from app.backtesting.domain import DomainValidationError

RULES = ContractRef(key="rules.cn.etf", version=1)

CHINA_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
VERSION = "china_exchange_daily@1"

# Shared trading days (all explicitly facted, weekends/holidays closed):
#   Dec29 Mon, Dec30 Tue, Dec31 Wed, Jan1 Thu (holiday), Jan2 Fri,
#   Jan3 Sat, Jan4 Sun, Jan5 Mon .. Jan9 Fri.
D29 = date(2025, 12, 29)
D30 = date(2025, 12, 30)
D31 = date(2025, 12, 31)
J1 = date(2026, 1, 1)
J2 = date(2026, 1, 2)
J3 = date(2026, 1, 3)
J4 = date(2026, 1, 4)
J5 = date(2026, 1, 5)
J6 = date(2026, 1, 6)
J7 = date(2026, 1, 7)


def base_days(open_days: set[date]) -> dict[date, bool]:
    """Every shared day with an explicit open/closed fact."""

    all_days = [D29, D30, D31, J1, J2, J3, J4, J5, J6, J7]
    return {day: day in open_days for day in all_days}


def make_definition(calendar_id: str) -> CalendarDefinition:
    return CalendarDefinition(
        calendar_id=calendar_id,
        definition_version=VERSION,
        timezone="Asia/Shanghai",
        default_sessions=CHINA_SESSIONS,
        valid_from=None,
        valid_to=None,
        source="test",
    )


def make_fact(
    calendar_id: str,
    session_date: date,
    is_open: bool,
) -> CalendarSessionFact:
    return CalendarSessionFact(
        calendar_id=calendar_id,
        session_date=session_date,
        is_open=is_open,
        definition_version=VERSION,
        timezone_override=None,
        sessions_override=None,
        source="test",
    )


class MutableFakeCalendarProvider:
    """In-memory calendar provider whose backing store can be mutated."""

    def __init__(self, schedule: dict[str, dict[date, bool]]) -> None:
        self._definitions = {
            calendar_id: make_definition(calendar_id) for calendar_id in schedule
        }
        self._facts = {
            (calendar_id, day): is_open
            for calendar_id, days in schedule.items()
            for day, is_open in days.items()
        }
        self.fact_calls = 0

    def definitions(self, calendar_id: str) -> tuple[CalendarDefinition, ...]:
        definition = self._definitions.get(calendar_id)
        return () if definition is None else (definition,)

    def fact(self, calendar_id: str, day: date) -> CalendarSessionFact | None:
        self.fact_calls += 1
        if (calendar_id, day) not in self._facts:
            return None
        return make_fact(calendar_id, day, self._facts[(calendar_id, day)])


def build_provider(
    sse_days: dict[date, bool] | set[date],
    szse_days: dict[date, bool] | set[date] | None = None,
) -> MutableFakeCalendarProvider:
    def normalize(days):
        if days is None:
            return None
        return days if isinstance(days, dict) else base_days(days)

    schedule = {"SSE": normalize(sse_days)}
    if szse_days is not None:
        schedule["SZSE"] = normalize(szse_days)
    return MutableFakeCalendarProvider(schedule)


def make_request(
    start: date,
    end: date,
    *,
    warmup: int = 0,
    calendar_ids: tuple[str, ...] = ("SSE", "SZSE"),
    instrument_id=None,
) -> DataRequest:
    return DataRequest(
        provider_key="memory",
        requested_window=DateRange(start_date=start, end_date=end),
        frequency="1d",
        rule_package=RULES,
        market_scope=MarketScope(exchanges=tuple(sorted(set(calendar_ids)))),
        universe_query_policy=UniverseQueryPolicy(),
        instrument_scope_mode=InstrumentScopeMode.FIXED,
        required_capabilities=(DataCapability.BARS,),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        static_instrument_ids=(instrument_id or uuid4(),),
        warmup_sessions=warmup,
        resolved_calendar_ids=calendar_ids,
        resolved_timezone="Asia/Shanghai",
        admission_calendar_session_signature="b" * 64,
        admission_preflight_status=PreflightStatus.READY,
        admission_preflight_hash="c" * 64,
        resolved_rule_snapshot_hash="d" * 64,
    )


def floor_from(days_by_calendar: dict[str, dict[date, bool]]):
    """Coverage floors at the earliest explicitly-facted day per calendar."""

    return CoverageBoundedWarmupSessionResolver(
        {
            calendar_id: min(days)
            for calendar_id, days in days_by_calendar.items()
        }
    )


COMMON_OPEN = {D29, D30, D31, J2, J5, J6, J7}


def _intent_of(frozen: DataRequest) -> DataPreflightRequest:
    """Rebuild the original unresolved intent from a frozen request."""

    names = DataPreflightRequest.__dataclass_fields__
    return DataPreflightRequest(
        **{name: getattr(frozen, name) for name in names}
    )


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestRequestValidation(unittest.TestCase):
    def _preflight_request(self, warmup):
        return DataPreflightRequest(
            provider_key="memory",
            requested_window=DateRange(start_date=J5, end_date=J7),
            frequency="1d",
            rule_package=RULES,
            market_scope=MarketScope(exchanges=("SSE",)),
            universe_query_policy=UniverseQueryPolicy(),
            instrument_scope_mode=InstrumentScopeMode.FIXED,
            required_capabilities=(DataCapability.BARS,),
            strategy_price_bases=(PriceBasis.RAW,),
            consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
            static_instrument_ids=(__import__("uuid").uuid4(),),
            warmup_sessions=warmup,
        )

    def test_warmup_accepts_zero_and_positive_integers(self):
        self.assertEqual(self._preflight_request(0).warmup_sessions, 0)
        self.assertEqual(self._preflight_request(3).warmup_sessions, 3)

    def test_warmup_rejects_negative_bool_and_other_types(self):
        for bad in (-1, True, False, 1.5, "3", None):
            with self.assertRaises(Exception):
                self._preflight_request(bad)

    def test_reversed_formal_window_fails(self):
        with self.assertRaises(InvalidDataRequestError):
            DateRange(start_date=J7, end_date=J5)


# ---------------------------------------------------------------------------
# Lifecycle and state boundaries
# ---------------------------------------------------------------------------


class TestLifecycle(unittest.TestCase):
    def _ready_session(self):
        request = make_request(J5, J7, warmup=3)
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": COMMON_OPEN, "SZSE": COMMON_OPEN}),
        )
        return session, request, provider

    def test_created_state_hides_sessions(self):
        session, _, _ = self._ready_session()
        self.assertIs(session.state, DataSessionState.CREATED)
        with self.assertRaises(Exception):
            session.resolved_sessions
        with self.assertRaises(Exception):
            session.warmup_sessions
        self.assertIsNone(session.report)

    def test_preflight_ready_transitions_and_freezes(self):
        session, _, _ = self._ready_session()
        report = session.preflight()
        self.assertIs(session.state, DataSessionState.READY)
        self.assertIs(report.status, PreflightStatus.READY)
        self.assertEqual(len(session.resolved_sessions), 3)
        self.assertEqual(len(session.warmup_sessions), 3)
        self.assertIsNotNone(session.report)

    def test_preflight_runs_exactly_once_and_never_recovers(self):
        session, _, _ = self._ready_session()
        session.preflight()
        with self.assertRaises(Exception):
            session.preflight()

    def test_on_ready_called_once_only_on_success(self):
        calls = []
        session, _, _ = self._ready_session()
        session._on_ready = lambda s: calls.append(s)
        session.preflight()
        self.assertEqual(len(calls), 1)

    def test_blocked_preflight_never_calls_strategy_hook(self):
        calls = []
        # Formal window fully closed -> blocked; the engine hook must never run.
        request = make_request(J5, J7, warmup=2)
        closed = {day: False for day in COMMON_OPEN}
        provider = build_provider(closed, closed)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": closed, "SZSE": closed}),
            on_ready=lambda s: calls.append(s),
        )
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertIs(session.state, DataSessionState.BLOCKED)
        self.assertEqual(calls, [])
        self.assertEqual(session.resolved_sessions, ())
        self.assertEqual(session.warmup_sessions, ())

    def test_closed_session_forbids_chunks_but_keeps_report(self):
        released = []
        session, _, _ = self._ready_session()
        session._on_close = lambda s: released.append(True)
        session.preflight()
        with self.assertRaises(UnsupportedCapabilityError):
            session.open_chunk(None)
        session.close()
        self.assertIs(session.state, DataSessionState.CLOSED)
        self.assertEqual(released, [True])
        session.close()  # idempotent
        self.assertEqual(released, [True])
        self.assertIsNotNone(session.report)
        with self.assertRaises(DataSessionClosedError):
            session.open_chunk(None)

    def test_context_manager_closes_on_exit(self):
        session, _, _ = self._ready_session()
        with session as entered:
            entered.preflight()
        self.assertIs(session.state, DataSessionState.CLOSED)

    def test_closed_before_preflight_reports_stable_error(self):
        session, _, _ = self._ready_session()
        session.close()
        with self.assertRaises(DataSessionClosedError):
            session.resolved_sessions
        with self.assertRaises(DataSessionClosedError):
            session.warmup_sessions

    def test_preflight_accepts_original_unfrozen_intent(self):
        # Per the documented flow the session is opened from the frozen
        # DataRequest and re-checked against the original unresolved intent;
        # shared business fields decide, not admission-only fields.
        request = make_request(J5, J7, warmup=2)
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": COMMON_OPEN, "SZSE": COMMON_OPEN}),
        )
        intent = _intent_of(request)
        self.assertNotEqual(intent, request)
        report = session.preflight(intent)
        self.assertIs(report.status, PreflightStatus.READY)

    def test_preflight_rejects_divergent_intent(self):
        request = make_request(J5, J7, warmup=2)
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": COMMON_OPEN, "SZSE": COMMON_OPEN}),
        )
        divergent = _intent_of(request)
        object.__setattr__(divergent, "frequency", "30m")
        with self.assertRaises(InvalidDataRequestError):
            session.preflight(divergent)

    def test_preflight_rejects_non_request_argument(self):
        request = make_request(J5, J7)
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
        )
        with self.assertRaises(InvalidDataRequestError):
            session.preflight(object())


# ---------------------------------------------------------------------------
# Formal sessions
# ---------------------------------------------------------------------------


class TestFormalSessions(unittest.TestCase):
    def test_compatible_calendars_produce_immutable_unique_sequence(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        request = make_request(J5, J7, warmup=3)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from(
                {"SSE": COMMON_OPEN, "SZSE": COMMON_OPEN}
            ),
        )
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.READY)
        self.assertIs(
            report.calendar_compatibility_status, CalendarAxisStatus.COMPATIBLE
        )
        formal = session.resolved_sessions
        self.assertIsInstance(formal, tuple)
        dates = [point.session_date for point in formal]
        self.assertEqual(dates, [J5, J6, J7])
        with self.assertRaises(FrozenInstanceError):
            formal[0].session_date = D29
        with self.assertRaises(AttributeError):
            session.resolved_sessions = ()

    def test_calendar_input_order_does_not_change_signature(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        fixed_id = uuid4()
        first = AuthoritativeDataSession(
            request=make_request(
                J5, J7, calendar_ids=("SSE", "SZSE"), instrument_id=fixed_id
            ),
            calendar_provider=provider,
        ).preflight()
        second = AuthoritativeDataSession(
            request=make_request(
                J5, J7, calendar_ids=("SZSE", "SSE"), instrument_id=fixed_id
            ),
            calendar_provider=provider,
        ).preflight()
        self.assertEqual(first.report_hash, second.report_hash)
        self.assertEqual(
            first.calendar_session_signature,
            second.calendar_session_signature,
        )

    def test_incompatible_formal_axis_blocks_with_full_differences(self):
        sse = dict(base_days(COMMON_OPEN))
        szse = dict(base_days(COMMON_OPEN))
        szse[J6] = False  # diverges inside the formal window
        provider = build_provider(sse, szse)
        session = AuthoritativeDataSession(
            request=make_request(J5, J7),
            calendar_provider=provider,
        )
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn("data_preflight_blocked", codes)
        self.assertEqual(report.resolved_sessions, ())
        self.assertEqual(report.warmup_sessions, ())
        self.assertTrue(report.calendar_axis_differences)
        self.assertEqual(
            {difference.date for difference in report.calendar_axis_differences},
            {J6},
        )

    def test_no_formal_sessions_blocks(self):
        closed = {day: False for day in COMMON_OPEN}
        provider = build_provider(closed, closed)
        session = AuthoritativeDataSession(
            request=make_request(J5, J7),
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": closed, "SZSE": closed}),
        )
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(NO_FORMAL_SESSIONS, codes)
        self.assertEqual(report.resolved_sessions, ())
        self.assertEqual(report.warmup_sessions, ())

    def test_resolution_is_cached_without_requery(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        request = make_request(J5, J7, warmup=2)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from(
                {"SSE": COMMON_OPEN, "SZSE": COMMON_OPEN}
            ),
        )
        session.preflight()
        calls_after_preflight = provider.fact_calls
        for _ in range(5):
            self.assertEqual(len(session.resolved_sessions), 3)
            self.assertEqual(len(session.warmup_sessions), 2)
        self.assertEqual(provider.fact_calls, calls_after_preflight)


# ---------------------------------------------------------------------------
# Warmup resolution
# ---------------------------------------------------------------------------


class TestWarmupSessions(unittest.TestCase):
    def _session(self, sse, szse, *, warmup, start=J5, end=J7):
        sse_days = sse if isinstance(sse, dict) else base_days(sse)
        szse_days = szse if isinstance(szse, dict) else base_days(szse)
        provider = build_provider(sse_days, szse_days)
        request = make_request(start, end, warmup=warmup)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": sse_days, "SZSE": szse_days}),
        )
        return session, provider

    def test_example_a_three_common_history_sessions(self):
        # Task-package example A: Jan2 is closed, so the three common
        # sessions immediately before Jan5 are Dec29..Dec31.
        open_days = {D29, D30, D31, J5, J6, J7}
        session, _ = self._session(open_days, open_days, warmup=3)
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.READY)
        self.assertEqual(
            [point.session_date for point in session.warmup_sessions],
            [D29, D30, D31],
        )
        # Warmup never leaks into the formal sequence.
        formal_dates = {point.session_date for point in session.resolved_sessions}
        self.assertFalse(formal_dates & {D29, D30, D31})

    def test_zero_warmup_returns_empty_tuple_without_fake_dates(self):
        session, _ = self._session(COMMON_OPEN, COMMON_OPEN, warmup=0)
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.READY)
        self.assertEqual(session.warmup_sessions, ())
        self.assertEqual(report.warmup_sessions_count, 0)
        self.assertIsNone(report.warmup_resolution)

    def test_weekends_do_not_consume_warmup_count(self):
        # Formal window Jan6..Jan7; history Jan2(Fri), Jan5(Mon); the
        # weekend Jan3/Jan4 carries explicit closed facts and counts for nothing.
        session, _ = self._session(COMMON_OPEN, COMMON_OPEN, warmup=2, start=J6)
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.READY)
        self.assertEqual(
            [point.session_date for point in session.warmup_sessions],
            [J2, J5],
        )

    def test_insufficient_history_blocks_without_shortening(self):
        # Facts start Dec29 but the resolver can only prove coverage from
        # Dec31: the provable window holds just Dec31 and Jan2.
        sse = base_days(COMMON_OPEN)
        szse = base_days(COMMON_OPEN)
        provider = build_provider(sse, szse)
        request = make_request(J5, J7, warmup=3)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=CoverageBoundedWarmupSessionResolver(
                {"SSE": D31, "SZSE": D31}
            ),
        )
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(WARMUP_COVERAGE_INSUFFICIENT, codes)
        issue = next(
            i for i in report.issues if i.code == WARMUP_COVERAGE_INSUFFICIENT
        )
        self.assertEqual(issue.details["requested_sessions"], 3)
        self.assertEqual(issue.details["actual_sessions"], 2)
        self.assertEqual(report.warmup_sessions, ())
        self.assertEqual(session.warmup_sessions, ())

    def test_warmup_calendar_mismatch_blocks_without_master_calendar(self):
        sse = base_days(COMMON_OPEN)
        szse = base_days(COMMON_OPEN)
        szse[D30] = False  # divergence strictly before the first formal session
        session, _ = self._session(sse, szse, warmup=3)
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(WARMUP_CALENDAR_INCOMPATIBLE, codes)
        details = next(
            i.details
            for i in report.issues
            if i.code == WARMUP_CALENDAR_INCOMPATIBLE
        )["differences"]
        self.assertEqual(details[0]["date"], D30.isoformat())
        self.assertEqual(details[0]["field"], "is_open")
        self.assertIn("false", (details[0]["actual_value"] or ""))
        self.assertIn("true", (details[0]["expected_value"] or ""))

    def test_missing_fact_in_warmup_window_blocks(self):
        sse = base_days(COMMON_OPEN)
        szse = base_days(COMMON_OPEN)
        del szse[D30]  # fact missing entirely: never read as a closed day
        session, _ = self._session(sse, szse, warmup=3)
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        codes = {issue.code for issue in report.issues}
        self.assertIn(WARMUP_FACT_MISSING, codes)

    def test_missing_definition_blocks_at_function_level(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        # A provider without any SZSE definition cannot resolve the axis.
        broken = MutableFakeCalendarProvider(
            {"SSE": base_days(COMMON_OPEN), "SZSE": base_days(COMMON_OPEN)}
        )
        broken._definitions.pop("SZSE")
        resolution = resolve_warmup_sessions(
            broken,
            calendar_ids=("SSE", "SZSE"),
            first_formal_session=J5,
            requested_sessions=2,
            resolver=floor_from(
                {"SSE": base_days(COMMON_OPEN), "SZSE": base_days(COMMON_OPEN)}
            ),
        )
        self.assertIs(resolution.status, WarmupStatus.BLOCKED)
        codes = {issue.code for issue in resolution.issues}
        self.assertIn(WARMUP_DEFINITION_MISSING, codes)
        self.assertIs(resolution.coverage_status, WarmupCoverageStatus.UNRESOLVED)

    def test_unprovable_history_blocks_without_scanning(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)

        class RefusingResolver:
            def history_window(self, provider, calendar_ids, anchor, requested):
                return None

        resolution = resolve_warmup_sessions(
            provider,
            calendar_ids=("SSE", "SZSE"),
            first_formal_session=J5,
            requested_sessions=2,
            resolver=RefusingResolver(),
        )
        self.assertIs(resolution.status, WarmupStatus.BLOCKED)
        codes = {issue.code for issue in resolution.issues}
        self.assertIn(WARMUP_HISTORY_UNRESOLVED, codes)

    def test_no_resolver_means_unprovable_history(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        resolution = resolve_warmup_sessions(
            provider,
            calendar_ids=("SSE", "SZSE"),
            first_formal_session=J5,
            requested_sessions=2,
            resolver=None,
        )
        self.assertIs(resolution.status, WarmupStatus.BLOCKED)
        self.assertIn(
            WARMUP_HISTORY_UNRESOLVED,
            {issue.code for issue in resolution.issues},
        )

    def test_calendar_ids_are_strictly_validated(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)
        for bad in ((1, 2), ("SSE", None), ("SSE", ""), 7):
            with self.assertRaises(InvalidDataRequestError):
                resolve_warmup_sessions(
                    provider,
                    calendar_ids=bad,
                    first_formal_session=J5,
                    requested_sessions=1,
                    resolver=floor_from({"SSE": COMMON_OPEN}),
                )

    def test_difference_issue_messages_carry_requested_count(self):
        sse = base_days(COMMON_OPEN)
        szse = base_days(COMMON_OPEN)
        del szse[D30]
        provider = build_provider(sse, szse)
        resolution = resolve_warmup_sessions(
            provider,
            calendar_ids=("SSE", "SZSE"),
            first_formal_session=J5,
            requested_sessions=3,
            resolver=floor_from({"SSE": sse, "SZSE": szse}),
        )
        fact_issues = [
            issue
            for issue in resolution.issues
            if issue.code == WARMUP_FACT_MISSING
        ]
        self.assertEqual(len(fact_issues), 1)
        self.assertIn("请求数量 3", fact_issues[0].message)

    def test_resolver_window_touching_anchor_is_rejected_unread(self):
        provider = build_provider(COMMON_OPEN, COMMON_OPEN)

        class OverrunningResolver:
            def history_window(
                self, provider, calendar_ids, first_formal_session, requested
            ):
                # Invalid: end_date touches the anchor day.
                return DateRange(start_date=D29, end_date=J5)

        resolution = resolve_warmup_sessions(
            provider,
            calendar_ids=("SSE", "SZSE"),
            first_formal_session=J5,
            requested_sessions=2,
            resolver=OverrunningResolver(),
        )
        self.assertIs(resolution.status, WarmupStatus.BLOCKED)
        codes = {issue.code for issue in resolution.issues}
        self.assertIn(WARMUP_HISTORY_UNRESOLVED, codes)
        # Nothing may be read from a window crossing the anchor.
        self.assertEqual(provider.fact_calls, 0)

    def test_warmup_sessions_all_precede_first_formal_ascending(self):
        session, _ = self._session(COMMON_OPEN, COMMON_OPEN, warmup=4)
        report = session.preflight()
        warmup_dates = [p.session_date for p in session.warmup_sessions]
        self.assertEqual(warmup_dates, sorted(warmup_dates))
        self.assertTrue(all(day < J5 for day in warmup_dates))

    def test_lookback_can_read_warmup_under_independent_cap(self):
        session, _ = self._session(COMMON_OPEN, COMMON_OPEN, warmup=3)
        session.preflight()
        # First-formal-decision history queries read from the warmup tuple;
        # the 512-session cap is enforced by LookbackWindow independently.
        end_at = datetime(2026, 1, 5, tzinfo=timezone.utc)
        window = LookbackWindow(sessions=MAX_LOOKBACK_SESSIONS, end_at=end_at)
        self.assertEqual(window.sessions, MAX_LOOKBACK_SESSIONS)
        with self.assertRaises(LookbackSessionsLimitExceededError):
            LookbackWindow(sessions=MAX_LOOKBACK_SESSIONS + 1, end_at=end_at)


# ---------------------------------------------------------------------------
# Immutability, audit fields, hashing
# ---------------------------------------------------------------------------


class TestImmutabilityAndReport(unittest.TestCase):
    def _ready(self, warmup=3):
        sse = base_days(COMMON_OPEN)
        provider = build_provider(sse, dict(sse))
        request = make_request(J5, J7, warmup=warmup)
        session = AuthoritativeDataSession(
            request=request,
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": sse, "SZSE": dict(sse)}),
        )
        return session, provider

    def test_provider_mutation_after_preflight_changes_nothing(self):
        session, provider = self._ready(warmup=2)
        session.preflight()
        formal_before = session.resolved_sessions
        warmup_before = session.warmup_sessions
        hash_before = session.report.report_hash
        # Mutate the provider inside the formal window afterwards.
        provider._facts[(("SSE"), J6)] = False
        self.assertEqual(session.resolved_sessions, formal_before)
        self.assertEqual(session.warmup_sessions, warmup_before)
        self.assertEqual(session.report.report_hash, hash_before)

    def test_report_audit_fields_are_complete(self):
        session, _ = self._ready(warmup=3)
        report = session.preflight()
        self.assertEqual(report.warmup_sessions_count, 3)
        self.assertEqual(len(report.warmup_sessions), 3)
        self.assertIsNotNone(report.warmup_resolution)
        self.assertIs(
            report.warmup_resolution.coverage_status, WarmupCoverageStatus.PROVEN
        )
        self.assertEqual(
            report.warmup_resolution.first_formal_session, J5
        )
        self.assertIsNotNone(report.warmup_resolution.history_window)
        self.assertEqual(
            report.warmup_resolution_signature,
            report.warmup_resolution.resolution_signature,
        )

    def test_equivalent_runs_hash_identically_despite_time_and_order(self):
        sse = base_days(COMMON_OPEN)
        fixed_id = uuid4()
        first = AuthoritativeDataSession(
            request=make_request(
                J5,
                J7,
                warmup=2,
                calendar_ids=("SSE", "SZSE"),
                instrument_id=fixed_id,
            ),
            calendar_provider=build_provider(sse, dict(sse)),
            warmup_resolver=floor_from({"SSE": sse, "SZSE": sse}),
        ).preflight()
        second = AuthoritativeDataSession(
            request=make_request(
                J5,
                J7,
                warmup=2,
                calendar_ids=("SZSE", "SSE"),
                instrument_id=fixed_id,
            ),
            calendar_provider=build_provider(dict(sse), sse),
            warmup_resolver=floor_from({"SZSE": sse, "SSE": sse}),
        ).preflight()
        # Different generated_at instances, different input order, same hash.
        self.assertEqual(first.report_hash, second.report_hash)

    def test_chinese_message_wording_does_not_change_hash(self):
        def make(message):
            return WarmupResolution(
                requested_sessions=1,
                first_formal_session=J5,
                status=WarmupStatus.BLOCKED,
                coverage_status=WarmupCoverageStatus.INSUFFICIENT,
                issues=(
                    PreflightIssue(
                        code=WARMUP_COVERAGE_INSUFFICIENT,
                        severity=IssueSeverity.ERROR,
                        scope="warmup_sessions",
                        message=message,
                    ),
                ),
            )

        self.assertEqual(
            make("历史不足：请求 2 个，实际 0 个").resolution_signature,
            make("warmup 历史不够").resolution_signature,
        )

    def test_machine_content_change_changes_hash(self):
        def make(code, count):
            return WarmupResolution(
                requested_sessions=count,
                first_formal_session=J5,
                status=WarmupStatus.BLOCKED,
                coverage_status=WarmupCoverageStatus.INSUFFICIENT,
                issues=(
                    PreflightIssue(
                        code=code,
                        severity=IssueSeverity.ERROR,
                        scope="warmup_sessions",
                        message="同一段中文文案",
                    ),
                ),
            )

        self.assertNotEqual(
            make(WARMUP_COVERAGE_INSUFFICIENT, 2).resolution_signature,
            make(WARMUP_COVERAGE_INSUFFICIENT, 3).resolution_signature,
        )
        self.assertNotEqual(
            make(WARMUP_COVERAGE_INSUFFICIENT, 2).resolution_signature,
            make(WARMUP_HISTORY_UNRESOLVED, 2).resolution_signature,
        )

    def test_blocked_report_exposes_no_partial_sequences(self):
        sse = base_days(COMMON_OPEN)
        szse = base_days(COMMON_OPEN)
        del szse[D30]
        provider = build_provider(sse, szse)
        session = AuthoritativeDataSession(
            request=make_request(J5, J7, warmup=3),
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": sse, "SZSE": szse}),
        )
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertEqual(report.resolved_sessions, ())
        self.assertEqual(report.warmup_sessions, ())
        self.assertIsNotNone(report.warmup_resolution)
        self.assertEqual(report.warmup_resolution.resolved_sessions, ())
        self.assertGreaterEqual(len(report.issues), 1)

    def test_warmup_axis_differences_are_mounted_on_blocked_report(self):
        sse = base_days(COMMON_OPEN)
        szse = base_days(COMMON_OPEN)
        szse[D30] = False
        provider = build_provider(sse, szse)
        session = AuthoritativeDataSession(
            request=make_request(J5, J7, warmup=3),
            calendar_provider=provider,
            warmup_resolver=floor_from({"SSE": sse, "SZSE": szse}),
        )
        report = session.preflight()
        self.assertIs(report.status, PreflightStatus.BLOCKED)
        self.assertTrue(report.warmup_axis_differences)
        self.assertEqual(
            {d.date for d in report.warmup_axis_differences}, {D30}
        )
        self.assertEqual(
            report.warmup_axis_differences,
            report.warmup_resolution.axis_differences,
        )

    def test_warmup_issues_are_copied_sorted_and_frozen(self):
        first = _issue_with_code(WARMUP_HISTORY_UNRESOLVED)
        second = _issue_with_code(WARMUP_COVERAGE_INSUFFICIENT)
        source = [first, second]
        resolution = WarmupResolution(
            requested_sessions=2,
            first_formal_session=J5,
            status=WarmupStatus.BLOCKED,
            coverage_status=WarmupCoverageStatus.UNRESOLVED,
            issues=source,
        )
        self.assertIsInstance(resolution.issues, tuple)
        self.assertEqual(
            [issue.code for issue in resolution.issues],
            [WARMUP_COVERAGE_INSUFFICIENT, WARMUP_HISTORY_UNRESOLVED],
        )
        signature = resolution.resolution_signature
        source.clear()
        self.assertEqual(resolution.resolution_signature, signature)
        mirrored = WarmupResolution(
            requested_sessions=2,
            first_formal_session=J5,
            status=WarmupStatus.BLOCKED,
            coverage_status=WarmupCoverageStatus.UNRESOLVED,
            issues=[second, first],
        )
        self.assertEqual(
            mirrored.resolution_signature, resolution.resolution_signature
        )

    def test_report_rejects_same_count_different_warmup_content(self):
        session, _ = self._ready(warmup=2)
        report = session.preflight()
        # Same count, but one history date swapped for another.
        replacement = _session_point_on(D30)
        tampered = [
            replacement if point.session_date == D31 else point
            for point in report.warmup_sessions
        ]
        self.assertNotEqual(tuple(tampered), report.warmup_sessions)
        with self.assertRaises(InvalidDataRequestError):
            _copy_report(report, warmup_sessions=tuple(tampered))

    def test_report_rejects_anchor_mismatch(self):
        session, _ = self._ready(warmup=2)
        report = session.preflight()
        original = report.warmup_resolution
        wrong_anchor = WarmupResolution(
            requested_sessions=original.requested_sessions,
            first_formal_session=date(2026, 1, 3),
            status=WarmupStatus.READY,
            coverage_status=WarmupCoverageStatus.PROVEN,
            resolved_sessions=original.resolved_sessions,
            # A window that is internally consistent but anchored wrong.
            history_window=DateRange(
                start_date=D29, end_date=date(2026, 1, 2)
            ),
        )
        with self.assertRaises(InvalidDataRequestError):
            _copy_report(
                report,
                warmup_resolution=wrong_anchor,
                warmup_resolution_signature=wrong_anchor.resolution_signature,
            )

    def test_report_rejects_contradictory_warmup_difference_evidence(self):
        session, _ = self._ready(warmup=2)
        report = session.preflight()
        fabricated = (
            CalendarAxisDifference(
                date=D30,
                calendar_id="SZSE",
                field=CalendarAxisDifferenceField.IS_OPEN,
                actual_value="false",
                expected_value="true",
            ),
        )
        with self.assertRaises(InvalidDataRequestError):
            _copy_report(
                report,
                warmup_axis_differences=fabricated,
            )

    def test_direct_construction_rejects_invalid_history_window(self):
        sessions = tuple(
            _session_point_on(day) for day in (D29, D30)
        )
        # Window touching the anchor.
        with self.assertRaises(InvalidDataRequestError):
            WarmupResolution(
                requested_sessions=2,
                first_formal_session=D31,
                status=WarmupStatus.READY,
                coverage_status=WarmupCoverageStatus.PROVEN,
                resolved_sessions=sessions,
                history_window=DateRange(start_date=D29, end_date=D31),
            )
        # Session outside the window.
        with self.assertRaises(InvalidDataRequestError):
            WarmupResolution(
                requested_sessions=2,
                first_formal_session=D31,
                status=WarmupStatus.READY,
                coverage_status=WarmupCoverageStatus.PROVEN,
                resolved_sessions=sessions,
                history_window=DateRange(start_date=D30, end_date=date(2025, 12, 30)),
            )
        # Ready with requested sessions but no proven window at all.
        with self.assertRaises(InvalidDataRequestError):
            WarmupResolution(
                requested_sessions=1,
                first_formal_session=D31,
                status=WarmupStatus.READY,
                coverage_status=WarmupCoverageStatus.PROVEN,
                resolved_sessions=(_session_point_on(D29),),
            )

    def test_ready_resolution_cannot_carry_axis_differences(self):
        fabricated = (
            CalendarAxisDifference(
                date=D30,
                calendar_id="SZSE",
                field=CalendarAxisDifferenceField.IS_OPEN,
                actual_value="false",
                expected_value="true",
            ),
        )
        with self.assertRaises(InvalidDataRequestError):
            WarmupResolution(
                requested_sessions=1,
                first_formal_session=D31,
                status=WarmupStatus.READY,
                coverage_status=WarmupCoverageStatus.PROVEN,
                resolved_sessions=(_session_point_on(D29),),
                history_window=DateRange(start_date=D29, end_date=date(2025, 12, 30)),
                axis_differences=fabricated,
            )

    def test_report_rejects_fabricated_warmup_differences(self):
        session, _ = self._ready(warmup=2)
        report = session.preflight()
        fabricated = (
            CalendarAxisDifference(
                date=D30,
                calendar_id="SZSE",
                field=CalendarAxisDifferenceField.IS_OPEN,
                actual_value="false",
                expected_value="true",
            ),
        )
        # No mounted resolution: difference evidence can never stand alone.
        with self.assertRaises(InvalidDataRequestError):
            _copy_report(report, warmup_axis_differences=fabricated)

    def test_report_rejects_non_difference_evidence_stably(self):
        session, _ = self._ready(warmup=2)
        report = session.preflight()
        # Type errors surface as the stable request error, never a bare
        # AttributeError from premature sort-key access.
        try:
            _copy_report(report, warmup_axis_differences=(object(),))
        except InvalidDataRequestError:
            pass
        else:
            self.fail("InvalidDataRequestError not raised")

    def test_issue_order_and_details_do_not_change_signature(self):
        # Two issues with identical code/scope/field but different details
        # used to tie in sort_key and make the hash depend on input order.
        def build(details_a, details_b, swap):
            pair = [
                _issue_with_code(WARMUP_COVERAGE_INSUFFICIENT, details_a),
                _issue_with_code(WARMUP_COVERAGE_INSUFFICIENT, details_b),
            ]
            if swap:
                pair.reverse()
            return WarmupResolution(
                requested_sessions=2,
                first_formal_session=J5,
                status=WarmupStatus.BLOCKED,
                coverage_status=WarmupCoverageStatus.INSUFFICIENT,
                issues=pair,
            )

        first = {"requested_sessions": 2, "actual_sessions": 0}
        second = {"requested_sessions": 2, "actual_sessions": 1}
        self.assertEqual(
            build(first, second, swap=False).resolution_signature,
            build(second, first, swap=True).resolution_signature,
        )


def _issue_with_code(code: str, details=None) -> PreflightIssue:
    return PreflightIssue(
        code=code,
        severity=IssueSeverity.ERROR,
        scope="warmup_sessions",
        message=f"{code} 的中文说明文案",
        details=details,
    )


def _session_point_on(day: date):
    from app.backtesting.calendar_axis import SessionPoint

    return SessionPoint(
        session_date=day,
        session_id=day.isoformat(),
        timezone="Asia/Shanghai",
        sessions=CHINA_SESSIONS,
    )


def _copy_report(report, **overrides):
    """Rebuild a report with the same fields plus overrides (hash recomputed)."""

    values = {
        name: getattr(report, name)
        for name in type(report).__dataclass_fields__
    }
    values.pop("report_hash")
    values.update(overrides)
    return type(report)(**values)


if __name__ == "__main__":
    unittest.main()
