"""Targeted tests for the generic backtesting data contracts (task 03-01).

Covers value objects, request layering, PIT query boundaries, coverage and
preflight reports plus the canonical hash, the runtime-checkable protocols,
the consistency objects, and the unified stable-code error hierarchy.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import date, datetime, time, timezone
from decimal import Decimal
from types import MappingProxyType
from uuid import uuid4

from app.backtesting.calendar_axis import (
    CalendarAxisStatus,
    CalendarDefinition,
    SessionPoint,
    SessionWindow,
)
from app.backtesting.data import (
    CALENDAR_AXIS_POLICY,
    CHUNK_POLICY,
    DATA_CONTRACT_VERSION,
    MAX_LOOKBACK_SESSIONS,
    AdjustedSeriesQuery,
    AdjustedSeriesPoint,
    Bar,
    BarQuery,
    ConsistencyMode,
    ConsistencyTokenStatus,
    ConsistencyValidation,
    ContractRef,
    CorporateAction,
    CorporateActionQuery,
    CoverageQuery,
    DATA_CONTRACT_VERSION,
    DataCapability,
    DataChunkQuery,
    DataChunkSession,
    DataContractError,
    DataCoverageReport,
    DataCutoffExceededError,
    DataPoint,
    DataPreflightBlockedError,
    DataPreflightConfirmationMismatchError,
    DataPreflightReport,
    DataPreflightRequest,
    DataProvider,
    DataRequest,
    DataSession,
    DataValueQuery,
    DataConsistencyEvidence,
    DateRange,
    EffectiveDateRange,
    ERROR_CODES,
    FactEvidence,
    HistoryIncompleteError,
    InstrumentMappingQuery,
    InstrumentQuery,
    InstrumentScopeMode,
    IssueSeverity,
    LookbackSessionsLimitExceededError,
    LookbackWindow,
    MarketScope,
    PitSupport,
    PreflightIssue,
    PreflightStatus,
    PriceBasis,
    ProviderContractViolationError,
    QualityMode,
    QualityStatus,
    QueryBoundary,
    Tick,
    InvalidDataRequestError,
    TradingRule,
    TradingRuleQuery,
    TradingStatus,
    TradingStatusQuery,
    TickQuery,
    UniverseQuery,
    UniverseQueryPolicy,
    WarmupCoverageStatus,
    WarmupResolution,
    WarmupStatus,
    canonical_json,
    freeze_json,
)
from app.strategy_protocol.contract import (
    IdentityMappingMissingError,
    IncompleteHistoryError,
    InvalidProviderResultError,
    MAX_LOOKBACK_SESSIONS as LEGACY_MAX_LOOKBACK_SESSIONS,
)

UTC = timezone.utc
ID_A = uuid4()
ID_B = uuid4()
ID_C = uuid4()

RULES = ContractRef(key="rules.cn.stock", version=1)


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _aware(hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 8, 1, hour, minute, tzinfo=UTC)


def _boundary(cutoff: datetime | None = None) -> QueryBoundary:
    return QueryBoundary(data_cutoff=cutoff or _aware(15))


def _window(start: date = date(2026, 1, 5), end: date = date(2026, 1, 30)) -> DateRange:
    return DateRange(start_date=start, end_date=end)


def _evidence(**overrides) -> FactEvidence:
    values = dict(
        source="tushare",
        observed_at=_aware(16),
        quality_status=QualityStatus.COMPLETE,
        known_at=_aware(15),
    )
    values.update(overrides)
    return FactEvidence(**values)


def _market_scope() -> MarketScope:
    return MarketScope(exchanges=("SSE",), asset_classes=("etf",))


def _universe_rules(*refs: ContractRef) -> UniverseQueryPolicy:
    return UniverseQueryPolicy(candidate_set_rules=refs or ())


def _static_ids() -> tuple:
    return (ID_A, ID_B)


def _request_defaults() -> dict:
    return dict(
        provider_key="tushare",
        requested_window=_window(),
        frequency="1d",
        rule_package=RULES,
        market_scope=_market_scope(),
        universe_query_policy=_universe_rules(),
        instrument_scope_mode=InstrumentScopeMode.FIXED,
        required_capabilities=(DataCapability.BARS,),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        static_instrument_ids=_static_ids(),
    )


def _preflight_request(**overrides) -> DataPreflightRequest:
    values = _request_defaults()
    values.update(overrides)
    return DataPreflightRequest(**values)


def _session_point(day: date) -> SessionPoint:
    return SessionPoint(
        session_date=day,
        session_id=day.isoformat(),
        timezone="Asia/Shanghai",
        sessions=(
            SessionWindow(start_time=time(9, 30), end_time=time(11, 30)),
            SessionWindow(start_time=time(13, 0), end_time=time(15, 0)),
        ),
    )


def _issue(severity=IssueSeverity.WARNING, code="TEST_WARNING", **overrides) -> PreflightIssue:
    values = dict(
        code=code,
        severity=severity,
        scope="coverage:bars",
        message="示例中文告警信息",
    )
    values.update(overrides)
    return PreflightIssue(**values)


def _report(status: PreflightStatus = PreflightStatus.READY, **overrides):
    """A minimal valid report with the given status."""

    issues: tuple = ()
    if status is PreflightStatus.DEGRADED:
        issues = (_issue(),)
    elif status is PreflightStatus.BLOCKED:
        issues = (_issue(IssueSeverity.ERROR, "TEST_ERROR"),)
    values = dict(
        status=status,
        generated_at=_aware(18),
        provider_key="tushare",
        capability_manifest_version=1,
        requested_window=_window(),
        scope_mode=InstrumentScopeMode.FIXED,
        resolved_calendar_ids=("SSE",),
        resolved_calendar_definitions=(),
        resolved_timezone="Asia/Shanghai",
        calendar_axis_policy=CALENDAR_AXIS_POLICY,
        calendar_compatibility_status=CalendarAxisStatus.COMPATIBLE,
        calendar_session_signature="a" * 64,
        resolved_sessions=(_session_point(date(2026, 1, 6)),),
        warmup_sessions=(),
        max_lookback_sessions=MAX_LOOKBACK_SESSIONS,
        knowledge_as_of=None,
        non_strict_pit_capabilities=(),
        consistency_mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
        consistency_token_capability=False,
        consistency_token_contract=None,
        data_chunk_policy=CHUNK_POLICY,
        data_chunk_size_sessions=20,
        required_capabilities=(DataCapability.BARS,),
        rule_package=RULES,
        rule_exception_set=None,
        static_instrument_ids=_static_ids(),
        mandatory_instrument_ids=(),
        strategy_price_bases=(PriceBasis.RAW,),
        engine_price_basis=PriceBasis.RAW,
        frequency="1d",
        warmup_sessions_count=0,
        market_scope=_market_scope(),
        universe_query_policy=_universe_rules(),
        quality_mode=QualityMode.STRICT,
        coverage_reports=(),
        source_revisions={},
        issues=issues,
    )
    values.update(overrides)
    return DataPreflightReport(**values)


def _ready_warmup_resolution(sessions) -> "WarmupResolution":
    """Build a ready warmup resolution for one anchor and session tuple."""

    sessions = tuple(sessions)
    return WarmupResolution(
        requested_sessions=len(sessions),
        first_formal_session=date(2026, 1, 6),
        status=WarmupStatus.READY,
        coverage_status=WarmupCoverageStatus.PROVEN,
        resolved_sessions=sessions,
        history_window=DateRange(
            start_date=date(2025, 12, 29), end_date=date(2026, 1, 5)
        ),
    )


def _report_for(request: DataPreflightRequest, **overrides) -> DataPreflightReport:
    """Build a report bound to ``request`` (all shared semantics copied)."""

    values = dict(
        provider_key=request.provider_key,
        requested_window=request.requested_window,
        scope_mode=request.instrument_scope_mode,
        calendar_axis_policy=request.calendar_axis_policy,
        max_lookback_sessions=request.max_lookback_sessions,
        knowledge_as_of=request.knowledge_as_of,
        consistency_mode=request.consistency_mode,
        consistency_token_capability=(
            request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
        ),
        consistency_token_contract=request.consistency_token_contract,
        data_chunk_policy=request.data_chunk_policy,
        data_chunk_size_sessions=request.data_chunk_size_sessions,
        required_capabilities=request.required_capabilities,
        rule_package=request.rule_package,
        rule_exception_set=request.rule_exception_set,
        static_instrument_ids=request.static_instrument_ids,
        mandatory_instrument_ids=request.mandatory_instrument_ids,
        strategy_price_bases=request.strategy_price_bases,
        engine_price_basis=request.engine_price_basis,
        frequency=request.frequency,
        warmup_sessions_count=request.warmup_sessions,
        market_scope=request.market_scope,
        universe_query_policy=request.universe_query_policy,
        quality_mode=request.quality_mode,
        data_contract_version=request.data_contract_version,
    )
    values.update(overrides)
    return _report(**values)


def _frozen_data_request() -> DataRequest:
    return DataRequest(
        **_request_defaults(),
        resolved_calendar_ids=("SSE",),
        resolved_timezone="Asia/Shanghai",
        admission_calendar_session_signature="b" * 64,
        admission_preflight_status=PreflightStatus.READY,
        admission_preflight_hash="c" * 64,
    )


class _FakeChunkSession:
    """Minimal conforming chunk-session used for structural checks."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    @property
    def consistency_evidence(self):
        return None

    def validate_consistency(self):
        return ConsistencyTokenStatus(status=ConsistencyValidation.VALID)

    def instruments(self, query):
        return ()

    def instrument_mappings(self, query):
        return ()

    def trading_rules(self, query):
        return ()

    def trading_status(self, query):
        return ()

    def universe(self, query):
        return ()

    def bars(self, query):
        return ()

    def ticks(self, query):
        return ()

    def values(self, query):
        return ()

    def adjusted_series(self, query):
        return ()

    def corporate_actions(self, query):
        return ()

    def coverage(self, query):
        return None


class _FakeSession(_FakeChunkSession):
    """Minimal conforming session used for structural checks."""

    @property
    def resolved_sessions(self):
        return ()

    @property
    def warmup_sessions(self):
        return ()

    @property
    def consistency_context(self):
        return None

    def preflight(self, request):
        return None

    def open_chunk(self, query):
        return _FakeChunkSession()


class _FakeProvider(_FakeSession):
    """Minimal conforming provider used for structural checks."""

    def capability_manifest(self):
        return None

    def preflight(self, request):
        return None

    def open_session(self, request):
        return _FakeSession()


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class TestValueObjects(unittest.TestCase):
    def test_dto_and_nested_collections_are_immutable(self):
        window = _window()
        with self.assertRaises(AttributeError):
            window.start_date = date(2020, 1, 1)
        request = _preflight_request()
        with self.assertRaises(AttributeError):
            request.static_instrument_ids = ()
        self.assertIsInstance(request.static_instrument_ids, tuple)
        self.assertIsInstance(request.required_capabilities, tuple)

    def test_date_range_rejects_datetime_and_reverse_order(self):
        with self.assertRaises(Exception):
            DateRange(datetime(2026, 1, 1, tzinfo=UTC), date(2026, 1, 31))
        with self.assertRaises(Exception):
            DateRange(date(2026, 2, 1), date(2026, 1, 1))

    def test_effective_range_is_half_open(self):
        span = EffectiveDateRange(valid_from=date(2026, 1, 1), valid_to=date(2026, 2, 1))
        self.assertTrue(span.covers(date(2026, 1, 1)))
        self.assertTrue(span.covers(date(2026, 1, 31)))
        # valid_to itself is excluded by the [from, to) semantics.
        self.assertFalse(span.covers(date(2026, 2, 1)))
        open_ended = EffectiveDateRange(valid_from=date(2026, 1, 1))
        self.assertTrue(open_ended.covers(date(2100, 1, 1)))

    def test_timestamps_reject_naive_datetimes(self):
        with self.assertRaises(Exception):
            QueryBoundary(data_cutoff=datetime(2026, 8, 1))
        with self.assertRaises(Exception):
            LookbackWindow(sessions=5, end_at=datetime(2026, 8, 1))
        with self.assertRaises(Exception):
            _evidence(observed_at=datetime(2026, 8, 1))

    def test_money_values_reject_float_bool_nan_inf(self):
        base = dict(
            instrument_id=ID_A,
            trade_date=date(2026, 1, 6),
            frequency="1d",
            price_basis=PriceBasis.RAW,
            evidence=_evidence(),
        )
        for bad in ("open", "close", "volume"):
            values = dict(
                base,
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                volume=Decimal("1"),
                amount=Decimal("1"),
            )
            values[bad] = 10.5 if bad != "volume" else -1
            with self.assertRaises(ProviderContractViolationError):
                Bar(**values)
        values = dict(base, open=True, high="1", low="1", close="1", volume="0", amount="0")
        with self.assertRaises(ProviderContractViolationError):
            Bar(**values)
        with self.assertRaises(ProviderContractViolationError):
            DataPoint(
                instrument_id=ID_A,
                series="pe",
                point_date=date(2026, 1, 6),
                value=float("nan"),
                evidence=_evidence(),
            )

    def test_json_extension_freezes_and_rejects_non_json(self):
        frozen = freeze_json({"a": [1, {"b": "x"}]}, "attributes")
        self.assertIsInstance(frozen, MappingProxyType)
        self.assertIsInstance(frozen["a"], tuple)
        with self.assertRaises(ValueError):
            freeze_json({"bad": {1, 2}}, "attributes")
        bar = Bar(
            instrument_id=ID_A,
            trade_date=date(2026, 1, 6),
            frequency="1d",
            open="1",
            high="1",
            low="1",
            close="1",
            volume="0",
            amount="0",
            price_basis=PriceBasis.RAW,
            evidence=_evidence(),
            schema=ContractRef(key="etf.ext", version=3),
            attributes={"fund": "510300", "tags": ("a", "b")},
        )
        self.assertIsInstance(bar.attributes, MappingProxyType)
        with self.assertRaises(TypeError):
            bar.attributes["new"] = 1
        with self.assertRaises(ProviderContractViolationError):
            Bar(
                instrument_id=ID_A,
                trade_date=date(2026, 1, 6),
                frequency="1d",
                open="1",
                high="1",
                low="1",
                close="1",
                volume="0",
                amount="0",
                price_basis=PriceBasis.RAW,
                evidence=_evidence(),
                attributes={"set": {1}},
            )

    def test_issue_details_are_deep_frozen_and_machine_only_sorting(self):
        issue = _issue(details={"z": 1, "a": ["x", None]})
        self.assertIsInstance(issue.details, MappingProxyType)
        self.assertIsInstance(issue.details["a"], tuple)
        first = _issue(code="A_CODE", message="第一条")
        second = _issue(code="B_CODE", message="第二条")
        ordered = tuple(sorted([second, first], key=lambda item: item.sort_key))
        self.assertEqual([item.code for item in ordered], ["A_CODE", "B_CODE"])
        reworded = _issue(code="A_CODE", message="改写后的文案")
        self.assertEqual(first.sort_key, reworded.sort_key)


# ---------------------------------------------------------------------------
# Requests and query boundaries
# ---------------------------------------------------------------------------


class TestRequests(unittest.TestCase):
    def test_contract_version_one_fixes_run_limit_to_512(self):
        for bad, expected in (
            (511, Exception),
            (513, LookbackSessionsLimitExceededError),
            (True, Exception),
            ("512", Exception),
            (512.0, Exception),
        ):
            overrides = dict(_request_defaults())
            overrides["max_lookback_sessions"] = bad
            try:
                DataPreflightRequest(**overrides)
            except expected:
                pass
            except Exception as exc:  # pragma: no cover - failure detail
                self.fail(f"{bad!r} raised unexpected {exc!r}")
            else:
                self.fail(f"{bad!r} unexpectedly accepted")

    def test_lookback_window_bounds(self):
        for good in (1, MAX_LOOKBACK_SESSIONS):
            LookbackWindow(sessions=good, end_at=_aware())
        for bad in (0, -1):
            with self.assertRaises(Exception):
                LookbackWindow(sessions=bad, end_at=_aware())
        with self.assertRaises(LookbackSessionsLimitExceededError):
            LookbackWindow(sessions=MAX_LOOKBACK_SESSIONS + 1, end_at=_aware())
        with self.assertRaises(Exception):
            LookbackWindow(sessions=True, end_at=_aware())

    def test_bar_query_window_is_exactly_one_shape(self):
        boundary = _boundary()
        BarQuery(instrument_ids=ID_A, frequency="1d", boundary=boundary, window=_window())
        BarQuery(
            instrument_ids=ID_A,
            frequency="1d",
            boundary=boundary,
            window=LookbackWindow(sessions=20, end_at=_aware(15)),
        )
        with self.assertRaises(Exception):
            BarQuery(instrument_ids=ID_A, frequency="1d", boundary=boundary, window=None)
        with self.assertRaises(Exception):
            # A tuple tries to smuggle both shapes into the single field.
            BarQuery(
                instrument_ids=ID_A,
                frequency="1d",
                boundary=boundary,
                window=(_window(), LookbackWindow(sessions=20, end_at=_aware())),
            )

    def test_cutoff_overflow_fails_instead_of_trimming(self):
        boundary = _boundary(_aware(15))
        with self.assertRaises(DataCutoffExceededError):
            BarQuery(
                instrument_ids=ID_A,
                frequency="1d",
                boundary=boundary,
                window=_window(end=date(2026, 8, 2)),
            )
        with self.assertRaises(DataCutoffExceededError):
            TickQuery(
                instrument_ids=ID_A,
                start_at=datetime(2026, 8, 1, 14, tzinfo=UTC),
                end_at=datetime(2026, 8, 1, 16, tzinfo=UTC),
                boundary=boundary,
            )
        with self.assertRaises(Exception):
            QueryBoundary(data_cutoff=_aware(15), knowledge_as_of=_aware(16))

    def test_scope_modes_enforce_combinations(self):
        rules = _universe_rules(ContractRef(key="universe.hs300", version=2))
        fixed = dict(_request_defaults())
        DataPreflightRequest(**fixed)
        dynamic = dict(_request_defaults())
        dynamic.update(
            instrument_scope_mode=InstrumentScopeMode.DYNAMIC,
            universe_query_policy=rules,
            static_instrument_ids=(),
        )
        DataPreflightRequest(**dynamic)
        hybrid = dict(dynamic)
        hybrid.update(
            instrument_scope_mode=InstrumentScopeMode.HYBRID,
            static_instrument_ids=_static_ids(),
        )
        DataPreflightRequest(**hybrid)
        # fixed with dynamic rules is forbidden
        bad_fixed = dict(_request_defaults())
        bad_fixed["universe_query_policy"] = rules
        with self.assertRaises(Exception):
            DataPreflightRequest(**bad_fixed)
        # dynamic without any candidate rule is forbidden
        empty_dynamic = dict(_request_defaults())
        empty_dynamic.update(
            instrument_scope_mode=InstrumentScopeMode.DYNAMIC,
            static_instrument_ids=(),
        )
        with self.assertRaises(Exception):
            DataPreflightRequest(**empty_dynamic)
        # hybrid needs both halves
        half_hybrid = dict(_request_defaults())
        half_hybrid["instrument_scope_mode"] = InstrumentScopeMode.HYBRID
        with self.assertRaises(Exception):
            DataPreflightRequest(**half_hybrid)

    def test_collections_dedupe_and_stably_sort(self):
        request = _preflight_request(
            static_instrument_ids=(ID_C, ID_A, ID_C),
            required_capabilities=(
                DataCapability.BARS,
                DataCapability.ACTIONS,
                DataCapability.BARS,
            ),
        )
        self.assertEqual(request.static_instrument_ids, tuple(sorted({ID_A, ID_C}, key=str)))
        self.assertEqual(
            request.required_capabilities,
            (DataCapability.ACTIONS, DataCapability.BARS),
        )
        scope = MarketScope(exchanges=("SZSE", "SSE", "SSE"))
        self.assertEqual(scope.exchanges, ("SSE", "SZSE"))
        rules = _universe_rules(
            ContractRef(key="b", version=2), ContractRef(key="a", version=3)
        )
        self.assertEqual(rules.candidate_set_rules[0].key, "a")

    def test_version_pinned_fields_cannot_drift(self):
        with self.assertRaises(Exception):
            _preflight_request(engine_price_basis=PriceBasis.QFQ)
        with self.assertRaises(Exception):
            _preflight_request(data_chunk_size_sessions=50)
        with self.assertRaises(Exception):
            _preflight_request(calendar_axis_policy=ContractRef(key="other", version=9))
        with self.assertRaises(Exception):
            _preflight_request(data_chunk_policy=ContractRef(key="sliding", version=1))
        with self.assertRaises(Exception):
            _preflight_request(data_contract_version=2)
        request = _preflight_request(
            consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
            consistency_token_contract=ContractRef(key="token.v1", version=1),
        )
        self.assertIsNotNone(request.consistency_token_contract)
        with self.assertRaises(Exception):
            _preflight_request(consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN)


class TestDataRequestFreezing(unittest.TestCase):
    def test_ready_admission_forbids_degraded_confirmation(self):
        request = _frozen_data_request()
        self.assertEqual(request.resolved_calendar_ids, ("SSE",))
        with self.assertRaises(Exception):
            values = dict(
                resolved_calendar_ids=("SSE",),
                resolved_timezone="Asia/Shanghai",
                admission_calendar_session_signature="b" * 64,
                admission_preflight_status=PreflightStatus.READY,
                admission_preflight_hash="c" * 64,
                accepted_degraded_preflight_hash="c" * 64,
            )
            DataRequest(**_request_defaults(), **values)

    def test_degraded_requires_identical_user_confirmed_hash(self):
        base = dict(
            resolved_calendar_ids=("SSE",),
            resolved_timezone="Asia/Shanghai",
            admission_calendar_session_signature="b" * 64,
            admission_preflight_status=PreflightStatus.DEGRADED,
            admission_preflight_hash="c" * 64,
        )
        DataRequest(**_request_defaults(), **base, accepted_degraded_preflight_hash="c" * 64)
        with self.assertRaises(DataPreflightConfirmationMismatchError):
            DataRequest(
                **_request_defaults(), **base, accepted_degraded_preflight_hash="d" * 64
            )
        with self.assertRaises(DataPreflightConfirmationMismatchError):
            DataRequest(**_request_defaults(), **base)

    def test_blocked_report_never_admits(self):
        with self.assertRaises(DataPreflightBlockedError):
            values = dict(
                resolved_calendar_ids=("SSE",),
                resolved_timezone="Asia/Shanghai",
                admission_calendar_session_signature="b" * 64,
                admission_preflight_status=PreflightStatus.BLOCKED,
                admission_preflight_hash="c" * 64,
            )
            DataRequest(**_request_defaults(), **values)

    def test_resolved_timezone_must_be_iana(self):
        with self.assertRaises(Exception):
            values = dict(
                resolved_calendar_ids=("SSE",),
                resolved_timezone="Mars/Olympus",
                admission_calendar_session_signature="b" * 64,
                admission_preflight_status=PreflightStatus.READY,
                admission_preflight_hash="c" * 64,
            )
            DataRequest(**_request_defaults(), **values)


# ---------------------------------------------------------------------------
# Reports and hashing
# ---------------------------------------------------------------------------


class TestReports(unittest.TestCase):
    def test_status_issue_invariants(self):
        _report(PreflightStatus.READY)
        with self.assertRaises(Exception):
            _report(PreflightStatus.READY, issues=(_issue(IssueSeverity.ERROR, "E"),))
        # degraded requires at least one warning and forbids errors
        _report(PreflightStatus.DEGRADED, resolved_sessions=())
        with self.assertRaises(Exception):
            _report(PreflightStatus.DEGRADED, resolved_sessions=(), issues=())
        with self.assertRaises(Exception):
            _report(
                PreflightStatus.DEGRADED,
                resolved_sessions=(),
                issues=(_issue(IssueSeverity.ERROR, "E"),),
            )
        # blocked requires at least one error
        with self.assertRaises(Exception):
            _report(PreflightStatus.BLOCKED, resolved_sessions=(), issues=())

    def test_blocked_reports_have_no_usable_sessions(self):
        with self.assertRaises(Exception):
            _report(
                PreflightStatus.BLOCKED,
                resolved_sessions=(_session_point(date(2026, 1, 6)),),
                issues=(_issue(IssueSeverity.ERROR, "E"),),
            )  # a blocked report must not carry resolved sessions
        incompatible = _report(
            PreflightStatus.BLOCKED,
            calendar_compatibility_status=CalendarAxisStatus.INCOMPATIBLE,
            calendar_session_signature="",
            resolved_sessions=(),
            issues=(_issue(IssueSeverity.ERROR, "E"),),
        )
        self.assertEqual(incompatible.resolved_sessions, ())
        # an incompatible axis cannot be anything but blocked
        with self.assertRaises(Exception):
            _report(
                PreflightStatus.READY,
                calendar_compatibility_status=CalendarAxisStatus.INCOMPATIBLE,
                calendar_session_signature="",
                resolved_sessions=(),
            )

    def test_warmup_sessions_stay_separate_from_official(self):
        official = date(2026, 1, 6)
        history = date(2025, 12, 30)
        history_point = _session_point(history)
        _report(
            warmup_sessions=(history_point,),
            warmup_sessions_count=1,
            warmup_resolution=_ready_warmup_resolution((history_point,)),
            warmup_resolution_signature=_ready_warmup_resolution(
                (history_point,)
            ).resolution_signature,
        )
        # a warmup session may not repeat an official session date
        with self.assertRaises(Exception):
            _report(
                warmup_sessions=(_session_point(official),),
                warmup_sessions_count=1,
            )

    def test_coverage_counts_reject_negatives_and_bad_sums(self):
        def coverage(**overrides):
            values = dict(
                requested_window=_window(),
                capability=DataCapability.BARS,
                instrument_ids=_static_ids(),
                expected_count=4,
                complete_count=3,
                partial_count=1,
                invalid_count=0,
                unavailable_count=0,
                quality_status=QualityStatus.PARTIAL,
            )
            values.update(overrides)
            return DataCoverageReport(**values)

        coverage()
        with self.assertRaises(Exception):
            coverage(complete_count=-1, partial_count=2)
        with self.assertRaises(Exception):
            coverage(partial_count=2)  # sums to 5 != expected 4
        with self.assertRaises(Exception):
            coverage(expected_count=-4, complete_count=-3, partial_count=-1)

    def _base_content(self, report: DataPreflightReport) -> dict:
        return report._hash_content()

    def test_hash_ignores_message_order_and_generation_time(self):
        early = _report(generated_at=_aware(10))
        late = _report(generated_at=_aware(23, 59))
        self.assertEqual(early.report_hash, late.report_hash)
        reordered_issues = _report(
            issues=(
                _issue(code="W1", message="警告一"),
                _issue(code="W2", message="警告二"),
            ),
            status=PreflightStatus.DEGRADED,
            resolved_sessions=(),
        )
        flipped = _report(
            issues=(
                _issue(code="W2", message="措辞不同也无关"),
                _issue(code="W1", message="另一措辞"),
            ),
            status=PreflightStatus.DEGRADED,
            resolved_sessions=(),
        )
        self.assertEqual(reordered_issues.report_hash, flipped.report_hash)

    def test_hash_tracks_semantic_changes(self):
        baseline = _report()
        variants = [
            _report(capability_manifest_version=2),
            _report(calendar_session_signature="f" * 64),
            _report(non_strict_pit_capabilities=(DataCapability.VALUES,)),
            _report(source_revisions={"tushare": "rev-7"}),
            _report(
                status=PreflightStatus.DEGRADED,
                resolved_sessions=(),
                issues=(_issue(code="NEW_WARN"),),
            ),
            _report(
                resolved_sessions=(
                    _session_point(date(2026, 1, 6)),
                    _session_point(date(2026, 1, 7)),
                )
            ),
            _report(
                coverage_reports=(
                    DataCoverageReport(
                        requested_window=_window(),
                        capability=DataCapability.BARS,
                        instrument_ids=_static_ids(),
                        expected_count=2,
                        complete_count=2,
                        partial_count=0,
                        invalid_count=0,
                        unavailable_count=0,
                        quality_status=QualityStatus.COMPLETE,
                    ),
                )
            ),
            _report(rule_package=ContractRef(key="rules.other", version=1)),
            _report(
                consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
                consistency_token_capability=True,
                consistency_token_contract=ContractRef(key="token.v1", version=1),
            ),
        ]
        hashes = {baseline.report_hash}
        for variant in variants:
            self.assertNotIn(variant.report_hash, hashes, "semantic change must alter hash")
            hashes.add(variant.report_hash)

    def test_canonical_json_excludes_tokens_and_credentials(self):
        payload = _report()._hash_content()
        text = canonical_json(payload)
        for secret_marker in ("raw_token", "password", "secret", "credential"):
            self.assertNotIn(secret_marker, text)


# ---------------------------------------------------------------------------
# Protocols, consistency, and errors
# ---------------------------------------------------------------------------


class TestProtocolsAndConsistency(unittest.TestCase):
    def test_structural_types_pass_runtime_checks(self):
        self.assertIsInstance(_FakeProvider(), DataProvider)
        self.assertIsInstance(_FakeSession(), DataSession)
        self.assertIsInstance(_FakeChunkSession(), DataChunkSession)

        class NotAProvider:
            pass

        self.assertNotIsInstance(NotAProvider(), DataProvider)

    def test_session_and_chunk_have_context_manager_boundaries(self):
        with _FakeProvider().open_session(None) as session:
            self.assertIsInstance(session, DataSession)
        with session.open_chunk(
            DataChunkQuery(
                chunk_index=0,
                first_session_id="2026-01-06",
                last_session_id="2026-01-06",
                fact_types=(DataCapability.BARS,),
            )
        ) as chunk:
            self.assertIsInstance(chunk, DataChunkSession)
            status = chunk.validate_consistency()
            self.assertIs(status.status, ConsistencyValidation.VALID)

    def test_open_chunk_takes_only_a_chunk_query(self):
        params = list(inspect.signature(DataSession.open_chunk).parameters)
        self.assertEqual(params, ["self", "query"])

    def test_consistency_token_status_invariants(self):
        ok = ConsistencyTokenStatus(status=ConsistencyValidation.VALID)
        self.assertIsNone(ok.failure_reason)
        with self.assertRaises(Exception):
            ConsistencyTokenStatus(
                status=ConsistencyValidation.VALID, failure_reason="不应有原因"
            )
        for failing in (
            ConsistencyValidation.NOT_VALIDATED,
            ConsistencyValidation.INVALID,
            ConsistencyValidation.EXPIRED,
            ConsistencyValidation.COVERAGE_INCOMPLETE,
        ):
            with self.assertRaises(Exception):
                ConsistencyTokenStatus(status=failing)
            explained = ConsistencyTokenStatus(
                status=failing, failure_reason="原因说明"
            )
            self.assertEqual(explained.failure_reason, "原因说明")

    def test_evidence_has_no_raw_secret_fields(self):
        evidence_fields = set(
            DataConsistencyEvidence.__dataclass_fields__
        )
        for forbidden in (
            "raw_token",
            "token",
            "credential",
            "password",
            "secret",
            "snapshot_handle",
        ):
            self.assertNotIn(forbidden, evidence_fields)
        # chunked_logical_token carries a digest; transitional mode has no
        # logical token and must not fabricate one.
        tokened = DataConsistencyEvidence(
            chunk_index=0,
            first_session_id="2026-01-06",
            last_session_id="2026-01-06",
            mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
            validation_status=ConsistencyValidation.EXPIRED,
            fact_types=(DataCapability.BARS,),
            token_digest="ab12cd34",
            coverage_summary={"chunks": 1},
            failure_reason="校验超时",
        )
        self.assertEqual(tokened.token_digest, "ab12cd34")
        transitional = DataConsistencyEvidence(
            chunk_index=0,
            first_session_id="2026-01-06",
            last_session_id="2026-01-06",
            mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
            validation_status=ConsistencyValidation.VALID,
            fact_types=(DataCapability.BARS,),
            coverage_summary={"snapshot": "rr"},
        )
        self.assertIsNone(transitional.token_digest)
        with self.assertRaises(Exception):
            DataConsistencyEvidence(
                chunk_index=0,
                first_session_id="2026-01-06",
                last_session_id="2026-01-06",
                mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
                validation_status=ConsistencyValidation.VALID,
                fact_types=(DataCapability.BARS,),
                coverage_summary={},
            )  # logical-token mode requires a digest
        with self.assertRaises(Exception):
            DataConsistencyEvidence(
                chunk_index=0,
                first_session_id="2026-01-06",
                last_session_id="2026-01-06",
                mode=ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
                validation_status=ConsistencyValidation.VALID,
                fact_types=(DataCapability.BARS,),
                token_digest="ab12cd34",
                coverage_summary={},
            )  # transitional mode must not carry a digest

    def test_every_required_error_code_exists(self):
        required = {
            "invalid_data_request",
            "unsupported_capability",
            "data_preflight_blocked",
            "data_preflight_confirmation_mismatch",
            "data_cutoff_exceeded",
            "lookback_sessions_limit_exceeded",
            "identity_mapping_incomplete",
            "history_incomplete",
            "consistency_not_validated",
            "consistency_token_invalid",
            "consistency_token_expired",
            "consistency_coverage_incomplete",
            "provider_contract_violation",
        }
        self.assertTrue(required.issubset(ERROR_CODES))
        for code in required:
            self.assertIsInstance(code, str)
            self.assertEqual(code.lower(), code)


class TestErrorConvergence(unittest.TestCase):
    def test_over_limit_lookback_maps_to_stable_code(self):
        with self.assertRaises(LookbackSessionsLimitExceededError) as ctx:
            LookbackWindow(sessions=513, end_at=_aware())
        self.assertEqual(ctx.exception.code, "lookback_sessions_limit_exceeded")

    def test_beyond_cutoff_maps_to_stable_code(self):
        with self.assertRaises(DataCutoffExceededError) as ctx:
            BarQuery(
                instrument_ids=ID_A,
                frequency="1d",
                boundary=_boundary(_aware(15)),
                window=_window(end=date(2026, 9, 1)),
            )
        self.assertEqual(ctx.exception.code, "data_cutoff_exceeded")

    def test_mapping_history_and_provider_codes_distinct(self):
        cases = [
            (IdentityMappingMissingError("缺失"), "identity_mapping_incomplete"),
            (IncompleteHistoryError("缺口"), "history_incomplete"),
            (InvalidProviderResultError("违约"), "provider_contract_violation"),
        ]
        for exc, code in cases:
            self.assertEqual(exc.code, code)
        self.assertNotEqual(cases[0][1], cases[1][1])
        self.assertNotEqual(cases[1][1], cases[2][1])

    def test_legacy_paths_still_compatible(self):
        from app.strategy_protocol.contract import DataCutoffViolationError, LookbackLimitExceededError

        legacy = DataCutoffViolationError(date(2026, 9, 1), date(2026, 8, 1))
        self.assertIsInstance(legacy, DataContractError)
        self.assertIsInstance(legacy, ValueError)
        self.assertEqual(legacy.code, "data_cutoff_exceeded")
        self.assertEqual(legacy.requested_end, date(2026, 9, 1))
        oversized = LookbackLimitExceededError(600, 512)
        self.assertIsInstance(oversized, LookbackSessionsLimitExceededError)
        self.assertEqual(oversized.code, "lookback_sessions_limit_exceeded")
        self.assertIs(MAX_LOOKBACK_SESSIONS, LEGACY_MAX_LOOKBACK_SESSIONS)
        self.assertEqual(MAX_LOOKBACK_SESSIONS, 512)


class TestAdmissionBinding(unittest.TestCase):
    """P1: admission hash must bind the full request semantics + status."""

    def test_from_admission_freezes_ready_report(self):
        request = _preflight_request()
        report = _report_for(request)
        frozen = DataRequest.from_admission(request, report)
        self.assertEqual(frozen.resolved_calendar_ids, ("SSE",))
        self.assertEqual(frozen.resolved_timezone, "Asia/Shanghai")
        self.assertEqual(frozen.admission_preflight_hash, report.report_hash)
        self.assertIsNone(frozen.accepted_degraded_preflight_hash)
        # Every preflight semantic is carried over unchanged.
        for field in (
            "provider_key",
            "frequency",
            "requested_window",
            "market_scope",
            "rule_package",
        ):
            self.assertEqual(getattr(frozen, field), getattr(request, field))

    def test_from_admission_rejects_foreign_report(self):
        request = _preflight_request()
        # Same shape, different frequency/market scope: the report describes
        # another intent and must never be admitted against this request.
        with self.assertRaises(InvalidDataRequestError):
            DataRequest.from_admission(
                request, _report_for(request, frequency="1m")
            )
        other_scope = MarketScope(exchanges=("SZSE",))
        with self.assertRaises(InvalidDataRequestError):
            DataRequest.from_admission(
                request, _report_for(request, market_scope=other_scope)
            )
        drifted = DataPreflightRequest(**{
            **_request_defaults(),
            "frequency": "1m",
        })
        with self.assertRaises(InvalidDataRequestError):
            # A drifted request cannot reuse the original report's hash.
            DataRequest.from_admission(drifted, _report_for(request))

    def test_from_admission_enforces_degraded_confirmation(self):
        request = _preflight_request()
        degraded = _report_for(
            request,
            status=PreflightStatus.DEGRADED,
            resolved_sessions=(),
        )
        with self.assertRaises(DataPreflightConfirmationMismatchError):
            DataRequest.from_admission(request, degraded)
        confirmed = DataRequest.from_admission(
            request, degraded, accepted_degraded=True
        )
        self.assertEqual(
            confirmed.accepted_degraded_preflight_hash, degraded.report_hash
        )
        ready = _report_for(request)
        with self.assertRaises(Exception):
            DataRequest.from_admission(request, ready, accepted_degraded=True)

    def test_from_admission_rejects_blocked_report(self):
        request = _preflight_request()
        blocked = _report_for(
            request,
            status=PreflightStatus.BLOCKED,
            resolved_sessions=(),
        )
        with self.assertRaises(DataPreflightBlockedError):
            DataRequest.from_admission(request, blocked)

    def test_blocked_report_may_express_unresolved_calendar(self):
        # P1: a run whose calendars never resolve must still be expressible
        # as a blocked report instead of only a construction error.
        blocked = _report_for(
            _preflight_request(),
            status=PreflightStatus.BLOCKED,
            resolved_calendar_ids=(),
            resolved_timezone=None,
            calendar_compatibility_status=CalendarAxisStatus.INCOMPATIBLE,
            calendar_session_signature="",
            resolved_sessions=(),
        )
        self.assertEqual(blocked.resolved_calendar_ids, ())
        # Empty calendar ids force the full blocked/incompatible shape.
        with self.assertRaises(Exception):
            _report(resolved_calendar_ids=())
        with self.assertRaises(Exception):
            _report_for(
                _preflight_request(),
                status=PreflightStatus.BLOCKED,
                resolved_calendar_ids=(),
                calendar_compatibility_status=CalendarAxisStatus.COMPATIBLE,
                resolved_sessions=(),
            )
        with self.assertRaises(Exception):
            _report_for(
                _preflight_request(),
                status=PreflightStatus.BLOCKED,
                resolved_calendar_ids=(),
                resolved_timezone="Asia/Shanghai",
                calendar_compatibility_status=CalendarAxisStatus.INCOMPATIBLE,
                calendar_session_signature="",
                resolved_sessions=(),
            )
        # ... while an admissible DataRequest still requires real calendars.
        with self.assertRaises(Exception):
            values = dict(
                resolved_calendar_ids=(),
                resolved_timezone="Asia/Shanghai",
                admission_calendar_session_signature="b" * 64,
                admission_preflight_status=PreflightStatus.READY,
                admission_preflight_hash="c" * 64,
            )
            DataRequest(**_request_defaults(), **values)

    def test_status_participates_in_the_hash(self):
        warning = _issue(code="W1")
        ready_with_warning = _report(issues=(warning,))
        degraded = _report(
            status=PreflightStatus.DEGRADED,
            resolved_sessions=(),
            issues=(warning,),
        )
        self.assertNotEqual(ready_with_warning.report_hash, degraded.report_hash)


class TestCutoffDaySemantics(unittest.TestCase):
    """P1/P2: date windows must not silently cross the cutoff instant."""

    def test_cutoff_day_is_rejected_by_default(self):
        boundary = QueryBoundary(data_cutoff=datetime(2026, 8, 1, 9, tzinfo=UTC))
        with self.assertRaises(DataCutoffExceededError):
            BarQuery(
                instrument_ids=ID_A,
                frequency="1d",
                boundary=boundary,
                window=_window(end=date(2026, 8, 1)),
            )

    def test_cutoff_day_is_readable_when_whole_day_declared_complete(self):
        boundary = QueryBoundary(
            data_cutoff=datetime(2026, 8, 1, 23, 59, tzinfo=UTC),
            include_cutoff_day=True,
        )
        query = BarQuery(
            instrument_ids=ID_A,
            frequency="1d",
            boundary=boundary,
            window=_window(end=date(2026, 8, 1)),
        )
        self.assertEqual(query.window.end_date, date(2026, 8, 1))


class TestManifestContractVersion(unittest.TestCase):
    """P2: a manifest may only declare the implemented contract version."""

    def test_manifest_rejects_other_contract_versions(self):
        from app.backtesting.data import DataCapabilityManifest
        from app.backtesting.calendar_axis import CalendarDefinition

        base = dict(
            provider_key="tushare",
            manifest_version=1,
            supported_calendars=(
                CalendarDefinition(
                    calendar_id="SSE",
                    definition_version="1",
                    timezone="Asia/Shanghai",
                    default_sessions=(
                        SessionWindow(time(9, 30), time(11, 30)),
                    ),
                ),
            ),
            supported_calendar_axis_policies=(CALENDAR_AXIS_POLICY,),
            rule_packages=(RULES,),
            rule_exception_sets=(),
            supported_asset_classes=("etf",),
            supported_frequencies=("1d",),
            supported_price_bases=(PriceBasis.RAW,),
            pit_support_by_capability={DataCapability.BARS: PitSupport.STRICT},
            consistency_modes=(ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,),
            consistency_token_contracts=(),
            supported_chunk_policies=(CHUNK_POLICY,),
            capabilities=(DataCapability.BARS,),
        )
        manifest = DataCapabilityManifest(data_contract_version=DATA_CONTRACT_VERSION, **base)
        self.assertEqual(manifest.data_contract_version, 1)
        for bad in (0, -1, 2, True, "1"):
            with self.assertRaises(Exception):
                DataCapabilityManifest(data_contract_version=bad, **base)


class TestRawFactPreservation(unittest.TestCase):
    """P1/P2: invalid raw OHLC must survive into coverage as invalid facts."""

    def _bar(self, quality: QualityStatus) -> Bar:
        return Bar(
            instrument_id=ID_A,
            trade_date=date(2026, 1, 6),
            frequency="1d",
            open="0",
            high="-3",
            low="-4",
            close="-5",
            volume="-10",
            amount="-20",
            price_basis=PriceBasis.RAW,
            evidence=_evidence(quality_status=quality),
        )

    def test_incomplete_quality_preserves_raw_illegal_values(self):
        for quality in (
            QualityStatus.PARTIAL,
            QualityStatus.INVALID,
            QualityStatus.UNAVAILABLE,
        ):
            bar = self._bar(quality)
            self.assertEqual(bar.open, Decimal("0"))
            self.assertEqual(bar.high, Decimal("-3"))

    def test_complete_quality_still_requires_consumable_values(self):
        with self.assertRaises(ProviderContractViolationError):
            self._bar(QualityStatus.COMPLETE)

    def test_float_nan_inf_are_always_rejected(self):
        with self.assertRaises(ProviderContractViolationError):
            Bar(
                instrument_id=ID_A,
                trade_date=date(2026, 1, 6),
                frequency="1d",
                open=float("nan"),
                high="0",
                low="0",
                close="0",
                volume="0",
                amount="0",
                price_basis=PriceBasis.RAW,
                evidence=_evidence(quality_status=QualityStatus.INVALID),
            )


if __name__ == "__main__":
    unittest.main()
