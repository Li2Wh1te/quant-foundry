"""Deterministic in-memory ``DataProvider`` for contract and engine tests.

This adapter is a *test fixture*, not a production data source: it serves
reproducible, fault-injectable facts for the generic data contract without
any ETF ingestion, Tushare, ORM, or database dependency.

Key constraints implemented here (frozen by data-contract version 1):

* ``max_lookback_sessions = 512`` -- a lookback window past the limit
  fails before any index access; failures never trim silently.
* ``data_cutoff`` -- queries past the boundary fail; returned facts are
  re-checked so a buggy fixture cannot leak future bars to a strategy.
* Missing bars are never back-filled; a complete lookback with a gap
  fails with ``history_incomplete`` instead of returning fewer rows.
* Warmup sessions stay fully isolated from formal sessions and chunks.
* Chunks follow ``fixed_trading_sessions@1`` with exactly 20 formal
  sessions (the tail chunk may be shorter).
* Chunk consistency runs before any business query, backed by a
  deterministic revision-vector token digest.  The digest binds the
  fixture revision, the frozen session tuples, the historical coverage
  envelope, the chunk boundaries, and the declared fact types.  Only the
  irreversible digest ever leaves the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import (
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
    CalendarAxisStatus,
    CalendarDefinition,
    CalendarSessionFact,
    InMemoryCalendarAxisDataProvider,
    SessionPoint,
    resolve_calendar_axis,
)
from app.backtesting.domain import _aware_datetime
from app.backtesting.data.errors import (
    ConsistencyCoverageIncompleteError,
    ConsistencyNotValidatedError,
    ConsistencyTokenExpiredError,
    ConsistencyTokenInvalidError,
    DataSessionClosedError,
    HistoryIncompleteError,
    InvalidDataRequestError,
    ProviderContractViolationError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.facts import Bar, InstrumentSpec
from app.backtesting.data.protocols import (
    ConsistencyTokenStatus,
    CoverageEnvelope,
    DataCapabilityManifest,
    DataConsistencyContext,
    DataConsistencyEvidence,
)
from app.backtesting.data.reports import (
    DataCoverageReport,
    DataPreflightReport,
    PreflightIssue,
    canonical_hash,
)
from app.backtesting.data.requests import (
    CALENDAR_AXIS_POLICY,
    CHUNK_POLICY,
    DATA_CONTRACT_VERSION,
    MAX_LOOKBACK_SESSIONS,
    AdjustedSeriesQuery,
    BarQuery,
    ConsistencyMode,
    ConsistencyValidation,
    ContractRef,
    CorporateActionQuery,
    CoverageQuery,
    DataCapability,
    DataChunkQuery,
    DataPreflightRequest,
    DataRequest,
    DataValueQuery,
    DateRange,
    InstrumentMappingQuery,
    InstrumentQuery,
    InstrumentScopeMode,
    IssueSeverity,
    PitSupport,
    PreflightStatus,
    PriceBasis,
    QualityMode,
    QualityStatus,
    TickQuery,
    TradingRuleQuery,
    TradingStatusQuery,
    UniverseQuery,
)
from app.backtesting.data.sessions import DataSessionState
from app.backtesting.data.warmup import (
    NO_FORMAL_SESSIONS,
    SCOPE_FORMAL,
    CoverageBoundedWarmupSessionResolver,
    WarmupResolution,
    WarmupStatus,
    resolve_warmup_sessions,
)

__all__ = [
    "CHUNK_TOKEN_CONTRACT",
    "ISSUE_CALENDAR_AXIS_INCOMPATIBLE",
    "ISSUE_INSTRUMENT_NOT_FOUND",
    "ISSUE_MANDATORY_BAR_COVERAGE_MISSING",
    "ISSUE_PROVIDER_KEY_MISMATCH",
    "ISSUE_UNSUPPORTED_CAPABILITY",
    "ISSUE_UNSUPPORTED_CONSISTENCY_MODE",
    "ISSUE_UNSUPPORTED_FREQUENCY",
    "ISSUE_UNSUPPORTED_PRICE_BASIS",
    "ISSUE_UNSUPPORTED_TOKEN_CONTRACT",
    "MemoryDataSet",
    "MemoryDataChunkSession",
    "MemoryDataProvider",
    "MemoryDataSession",
]


# ---------------------------------------------------------------------------
# Stable machine identifiers
# ---------------------------------------------------------------------------

CHUNK_TOKEN_CONTRACT = ContractRef(key="memory_revision_vector", version=1)
"""Versioned reference of the in-memory revision-vector token contract."""

ISSUE_PROVIDER_KEY_MISMATCH = "provider_key_mismatch"
ISSUE_UNSUPPORTED_CAPABILITY = "unsupported_capability"
ISSUE_UNSUPPORTED_FREQUENCY = "unsupported_frequency"
ISSUE_UNSUPPORTED_PRICE_BASIS = "unsupported_price_basis"
ISSUE_UNSUPPORTED_CONSISTENCY_MODE = "unsupported_consistency_mode"
ISSUE_UNSUPPORTED_TOKEN_CONTRACT = "unsupported_consistency_token_contract"
ISSUE_INSTRUMENT_NOT_FOUND = "instrument_not_found"
ISSUE_CALENDAR_AXIS_INCOMPATIBLE = "data_preflight_blocked"
ISSUE_MANDATORY_BAR_COVERAGE_MISSING = "mandatory_bar_coverage_missing"

_SERVABLE_CHUNK_FACT_TYPES = frozenset(
    {DataCapability.BARS, DataCapability.COVERAGE}
)
"""Fact types one chunk can actually serve in the first version.

``CALENDARS`` is deliberately absent even though the manifest declares it:
calendar facts are consumed during session resolution and warmup
resolution, not through chunk business queries, so a chunk query cannot
serve them and must fail closed instead of accepting an uncovered token.
"""


def _non_blank_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidDataRequestError(f"{field_name} must be non-blank text")
    return value


# ---------------------------------------------------------------------------
# Immutable fixture dataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryDataSet:
    """Immutable, indexed test dataset backing one memory provider.

    Construction copies, sorts, and freezes every collection, rejects
    duplicate facts, indexes bars by ``(instrument_id, frequency,
    price_basis)`` plus a per-day lookup, and records the caller-chosen
    deterministic ``fixture_revision``.  Mutating the original input
    collections afterwards never changes query results.

    ``clock`` is the deterministic "now" of the fixture: it feeds report
    generation timestamps and consistency validation timestamps so runs
    are reproducible.
    """

    provider_key: str
    fixture_revision: str
    calendar_definitions: tuple[CalendarDefinition, ...]
    calendar_facts: tuple[CalendarSessionFact, ...]
    instruments: tuple[InstrumentSpec, ...]
    bars: tuple[Bar, ...]
    clock: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_key", _non_blank_text(self.provider_key, "provider_key")
        )
        object.__setattr__(
            self,
            "fixture_revision",
            _non_blank_text(self.fixture_revision, "fixture_revision"),
        )
        object.__setattr__(
            self, "clock", _aware_datetime(self.clock, "clock")
        )

        definitions = tuple(self.calendar_definitions)
        object.__setattr__(
            self,
            "calendar_definitions",
            tuple(
                sorted(
                    set(definitions),
                    key=lambda item: (item.calendar_id, item.definition_version),
                )
            ),
        )
        facts = tuple(self.calendar_facts)
        seen_facts: set[tuple[str, date]] = set()
        for fact in facts:
            key = (fact.calendar_id, fact.session_date)
            if key in seen_facts:
                raise InvalidDataRequestError(
                    "duplicate calendar fact for one calendar and date",
                    details={"calendar_id": fact.calendar_id},
                )
            seen_facts.add(key)
        object.__setattr__(
            self,
            "calendar_facts",
            tuple(sorted(facts, key=lambda item: (item.calendar_id, item.session_date))),
        )

        instruments = tuple(self.instruments)
        instrument_map: dict[UUID, InstrumentSpec] = {}
        for spec in instruments:
            if spec.instrument_id in instrument_map:
                raise InvalidDataRequestError(
                    "duplicate instrument identity in dataset",
                    details={"instrument_id": str(spec.instrument_id)},
                )
            instrument_map[spec.instrument_id] = spec
        object.__setattr__(
            self,
            "instruments",
            tuple(sorted(instruments, key=lambda item: str(item.instrument_id))),
        )

        bars = tuple(self.bars)
        bar_keys: set[tuple[UUID, str, PriceBasis, date]] = set()
        for bar in bars:
            key = (bar.instrument_id, bar.frequency, bar.price_basis, bar.trade_date)
            if key in bar_keys:
                raise InvalidDataRequestError(
                    "duplicate bar fact for one instrument, frequency, "
                    "price basis, and trade date",
                )
            bar_keys.add(key)
        object.__setattr__(
            self,
            "bars",
            tuple(
                sorted(
                    bars,
                    key=lambda item: (
                        str(item.instrument_id),
                        item.frequency,
                        item.price_basis.value,
                        item.trade_date,
                    ),
                )
            ),
        )

        # Indexes: per-series ascending tuples plus an exact-point lookup.
        series_index: dict[tuple[UUID, str, PriceBasis], list[Bar]] = {}
        point_index: dict[tuple[UUID, str, PriceBasis, date], Bar] = {}
        day_index: dict[tuple[UUID, PriceBasis, date], list[Bar]] = {}
        floor_by_calendar: dict[str, date] = {}
        for bar in bars:
            series_index.setdefault(
                (bar.instrument_id, bar.frequency, bar.price_basis), []
            ).append(bar)
            point_index[
                (bar.instrument_id, bar.frequency, bar.price_basis, bar.trade_date)
            ] = bar
            day_index.setdefault(
                (bar.instrument_id, bar.price_basis, bar.trade_date), []
            ).append(bar)
        object.__setattr__(
            self,
            "_series_index",
            MappingProxyType(
                {key: tuple(rows) for key, rows in series_index.items()}
            ),
        )
        object.__setattr__(self, "_point_index", MappingProxyType(point_index))
        object.__setattr__(
            self,
            "_day_index",
            MappingProxyType(
                {key: tuple(rows) for key, rows in day_index.items()}
            ),
        )
        for fact in self.calendar_facts:
            current = floor_by_calendar.get(fact.calendar_id)
            if current is None or fact.session_date < current:
                floor_by_calendar[fact.calendar_id] = fact.session_date
        object.__setattr__(
            self, "_calendar_fact_floor", MappingProxyType(dict(floor_by_calendar))
        )
        object.__setattr__(
            self,
            "_calendar_axis_provider",
            InMemoryCalendarAxisDataProvider(definitions, facts),
        )

    # ------------------------------------------------------------------
    # Read accessors (never expose mutable internals)
    # ------------------------------------------------------------------

    @property
    def calendar_axis_provider(self) -> InMemoryCalendarAxisDataProvider:
        """The strict_compatible@1 fact source built from this dataset."""

        return self._calendar_axis_provider

    def instrument(self, instrument_id: UUID) -> InstrumentSpec | None:
        """Return the spec for one stable identity, or ``None``."""

        for spec in self.instruments:
            if spec.instrument_id == instrument_id:
                return spec
        return None

    def bars_in_range(
        self,
        instrument_ids: Sequence[UUID],
        frequency: str,
        price_basis: PriceBasis,
        start_day: date,
        end_day: date,
    ) -> tuple[Bar, ...]:
        """Ascending raw bars for the given identities inside ``[start, end]``."""

        rows: list[Bar] = []
        for instrument_id in instrument_ids:
            series = self._series_index.get((instrument_id, frequency, price_basis))
            if not series:
                continue
            rows.extend(
                bar
                for bar in series
                if start_day <= bar.trade_date <= end_day
            )
        rows.sort(key=lambda bar: (bar.trade_date, str(bar.instrument_id)))
        return tuple(rows)

    def bar_at(
        self,
        instrument_id: UUID,
        frequency: str,
        price_basis: PriceBasis,
        day: date,
    ) -> Bar | None:
        """The exact bar at one point of the index, or ``None``."""

        return self._point_index.get((instrument_id, frequency, price_basis, day))

    def any_bar_on(
        self, instrument_id: UUID, price_basis: PriceBasis, day: date
    ) -> Bar | None:
        """First bar of any frequency for one identity and day, or ``None``."""

        rows = self._day_index.get((instrument_id, price_basis, day))
        return rows[0] if rows else None

    def calendar_fact_floor(self, calendar_id: str) -> date | None:
        """Earliest date with an explicit fact for one calendar."""

        return self._calendar_fact_floor.get(calendar_id)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MemoryDataProvider:
    """Deterministic in-memory implementation of :class:`DataProvider`.

    The provider declares exactly the capabilities ``CALENDARS``, ``BARS``,
    and ``COVERAGE``; every other capability fails fast with
    ``unsupported_capability`` both at preflight and on chunk queries.

    ``invalidate_revision()`` is a **test-only** control that advances the
    internal revision counter so outstanding chunk tokens expire; it must
    never become a runtime entry point for switching consistency modes.
    """

    def __init__(
        self,
        dataset: MemoryDataSet,
        *,
        capability_manifest_version: int = 1,
    ) -> None:
        if not isinstance(dataset, MemoryDataSet):
            raise InvalidDataRequestError("dataset must be a MemoryDataSet")
        # The first version pins this fixture to daily bars: a dataset that
        # smuggles in any other frequency is a fixture bug and is rejected
        # instead of being silently advertised by the manifest.
        foreign_frequencies = sorted(
            {bar.frequency for bar in dataset.bars} - {"1d"}
        )
        if foreign_frequencies:
            raise InvalidDataRequestError(
                "the memory fixture serves daily bars only; the dataset "
                "contains bars of unsupported frequencies",
                details={"unsupported_frequencies": foreign_frequencies},
            )
        self._dataset = dataset
        self._manifest_version = capability_manifest_version
        self._revision = 0
        self._read_count = 0

        asset_classes = (
            sorted({spec.asset_class for spec in dataset.instruments}) or ["equity"]
        )
        served = (DataCapability.CALENDARS, DataCapability.BARS, DataCapability.COVERAGE)
        self._manifest = DataCapabilityManifest(
            provider_key=dataset.provider_key,
            manifest_version=capability_manifest_version,
            data_contract_version=DATA_CONTRACT_VERSION,
            supported_calendars=dataset.calendar_definitions,
            supported_calendar_axis_policies=(CALENDAR_AXIS_POLICY,),
            rule_packages=(),
            rule_exception_sets=(),
            supported_asset_classes=tuple(asset_classes),
            # Version 1 freezes this provider to exactly the daily frequency.
            supported_frequencies=("1d",),
            supported_price_bases=(PriceBasis.RAW,),
            pit_support_by_capability={
                capability: PitSupport.STRICT for capability in served
            },
            consistency_modes=(
                ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
                ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
            ),
            consistency_token_contracts=(CHUNK_TOKEN_CONTRACT,),
            supported_chunk_policies=(CHUNK_POLICY,),
            capabilities=served,
        )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def dataset(self) -> MemoryDataSet:
        """The immutable backing dataset."""

        return self._dataset

    @property
    def read_count(self) -> int:
        """How many times the bar index has been accessed (test observability)."""

        return self._read_count

    def capability_manifest(self) -> DataCapabilityManifest:
        """Return the static structured capability manifest."""

        return self._manifest

    def invalidate_revision(self) -> int:
        """Advance the revision vector, expiring all outstanding chunk tokens.

        Test/fixture control only: production runtimes may not switch
        consistency modes mid-run through this method.
        """

        self._revision += 1
        return self._revision

    def preflight(self, request: DataPreflightRequest) -> DataPreflightReport:
        """Run admission preflight and return the frozen report."""

        if not isinstance(request, DataPreflightRequest):
            raise InvalidDataRequestError("request must be a DataPreflightRequest")
        return self._build_preflight_report(request)

    def open_session(self, request: DataRequest) -> "MemoryDataSession":
        """Open an authoritative session bound to this provider's facts."""

        if not isinstance(request, DataRequest):
            raise InvalidDataRequestError("request must be a frozen DataRequest")
        if request.provider_key != self._dataset.provider_key:
            raise InvalidDataRequestError(
                "request provider_key does not match this provider",
                details={
                    "expected": self._dataset.provider_key,
                    "actual": request.provider_key,
                },
            )
        return MemoryDataSession(provider=self, request=request)

    # ------------------------------------------------------------------
    # Preflight pipeline (shared by provider.preflight and session.preflight)
    # ------------------------------------------------------------------

    def _build_preflight_report(
        self,
        request: DataPreflightRequest,
        *,
        frozen_calendar_ids: tuple[str, ...] | None = None,
    ) -> DataPreflightReport:
        issues: list[PreflightIssue] = []

        if request.provider_key != self._dataset.provider_key:
            issues.append(
                PreflightIssue(
                    code=ISSUE_PROVIDER_KEY_MISMATCH,
                    severity=IssueSeverity.ERROR,
                    scope="provider",
                    message=(
                        f"预检请求的 provider_key 与数据集不匹配，无法提供数据服务："
                        f"{request.provider_key} != {self._dataset.provider_key}"
                    ),
                    field="provider_key",
                )
            )
        if request.consistency_mode not in self._manifest.consistency_modes:
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_CONSISTENCY_MODE,
                    severity=IssueSeverity.ERROR,
                    scope="consistency",
                    message=(
                        f"请求的一致性模式 {request.consistency_mode.value} "
                        "不受内存 Provider 支持"
                    ),
                    field="consistency_mode",
                )
            )
        if (
            request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
            and request.consistency_token_contract is not None
            and request.consistency_token_contract
            not in self._manifest.consistency_token_contracts
        ):
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_TOKEN_CONTRACT,
                    severity=IssueSeverity.ERROR,
                    scope="consistency",
                    message="请求的一致性 token 契约不受内存 Provider 支持",
                    field="consistency_token_contract",
                )
            )
        if (
            request.consistency_mode is ConsistencyMode.TRANSITIONAL_REPEATABLE_READ
            and request.consistency_token_contract is not None
        ):
            # The transitional mode issues no logical tokens at all, so a
            # configured token contract can never be honored.
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_TOKEN_CONTRACT,
                    severity=IssueSeverity.ERROR,
                    scope="consistency",
                    message="过渡一致性模式不签发逻辑 token，不能配置 token 契约",
                    field="consistency_token_contract",
                )
            )
        for capability in request.required_capabilities:
            if capability not in self._manifest.capabilities:
                issues.append(
                    PreflightIssue(
                        code=ISSUE_UNSUPPORTED_CAPABILITY,
                        severity=IssueSeverity.ERROR,
                        scope="capabilities",
                        message=(
                            f"请求的数据能力 {capability.value} 不受内存 "
                            "Provider 支持"
                        ),
                        field="required_capabilities",
                        details={"capability": capability.value},
                    )
                )
        # Only the frozen fixed scope is supported: dynamic and hybrid runs
        # depend on the universe capability this fixture does not serve, so
        # they must produce a structured blocked report instead of an
        # internally invalid one.
        if request.instrument_scope_mode is not InstrumentScopeMode.FIXED:
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_CAPABILITY,
                    severity=IssueSeverity.ERROR,
                    scope="instrument_scope",
                    message=(
                        f"内存 Provider 仅支持固定标的范围，不支持 "
                        f"{request.instrument_scope_mode.value} 范围模式"
                    ),
                    field="instrument_scope_mode",
                    details={"scope_mode": request.instrument_scope_mode.value},
                )
            )
        if request.frequency not in self._manifest.supported_frequencies:
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_FREQUENCY,
                    severity=IssueSeverity.ERROR,
                    scope="frequency",
                    message=(
                        f"请求频率 {request.frequency} 不受内存 Provider 支持"
                    ),
                    field="frequency",
                )
            )
        for basis in request.strategy_price_bases:
            if basis not in self._manifest.supported_price_bases:
                issues.append(
                    PreflightIssue(
                        code=ISSUE_UNSUPPORTED_PRICE_BASIS,
                        severity=IssueSeverity.ERROR,
                        scope="price_basis",
                        message=f"请求价格基准 {basis.value} 不受内存 Provider 支持",
                        field="strategy_price_bases",
                    )
                )

        known_ids = {spec.instrument_id for spec in self._dataset.instruments}
        scope_ids = list(
            dict.fromkeys(
                [*request.static_instrument_ids, *request.mandatory_instrument_ids]
            )
        )
        for instrument_id in scope_ids:
            if instrument_id not in known_ids:
                issues.append(
                    PreflightIssue(
                        code=ISSUE_INSTRUMENT_NOT_FOUND,
                        severity=IssueSeverity.ERROR,
                        scope="instruments",
                        message="固定或强制标的不在内存数据集中，已阻断回测",
                        instrument_id=instrument_id,
                        field="static_instrument_ids",
                    )
                )

        # The authoritative calendar set is the one frozen into an admitted
        # ``DataRequest``; a raw intent (provider-level preflight) has its
        # axis derived strictly from the in-scope instruments' named
        # calendars.  Callers never pick calendars freely.
        if frozen_calendar_ids:
            calendar_ids = tuple(sorted(set(frozen_calendar_ids)))
        else:
            calendar_ids = tuple(
                sorted(
                    {
                        spec.calendar_id
                        for instrument_id in scope_ids
                        if (spec := self._dataset.instrument(instrument_id)) is not None
                    }
                )
            )
        warmup_resolution: WarmupResolution | None = None
        if calendar_ids:
            axis = resolve_calendar_axis(
                self._dataset.calendar_axis_provider,
                policy_key=POLICY_KEY_STRICT_COMPATIBLE,
                policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
                start_date=request.requested_window.start_date,
                end_date=request.requested_window.end_date,
                calendar_ids=calendar_ids,
            )
            if axis.status is CalendarAxisStatus.INCOMPATIBLE:
                issues.append(
                    PreflightIssue(
                        code=ISSUE_CALENDAR_AXIS_INCOMPATIBLE,
                        severity=IssueSeverity.ERROR,
                        scope=SCOPE_FORMAL,
                        message=(
                            f"正式区间 "
                            f"{request.requested_window.start_date.isoformat()}.."
                            f"{request.requested_window.end_date.isoformat()} "
                            f"日历轴不兼容，共 {len(axis.differences)} 处差异，"
                            "已阻断回测"
                        ),
                        field="calendar_axis",
                    )
                )
            elif not axis.resolved_sessions:
                issues.append(
                    PreflightIssue(
                        code=NO_FORMAL_SESSIONS,
                        severity=IssueSeverity.ERROR,
                        scope=SCOPE_FORMAL,
                        message=(
                            f"正式区间 "
                            f"{request.requested_window.start_date.isoformat()}.."
                            f"{request.requested_window.end_date.isoformat()} "
                            "内没有任何共同开市交易会话，无法启动回测"
                        ),
                        field="resolved_sessions",
                    )
                )
            else:
                if request.warmup_sessions > 0:
                    warmup_resolution = resolve_warmup_sessions(
                        self._dataset.calendar_axis_provider,
                        calendar_ids=calendar_ids,
                        first_formal_session=axis.resolved_sessions[0].session_date,
                        requested_sessions=request.warmup_sessions,
                        resolver=CoverageBoundedWarmupSessionResolver(
                            {
                                calendar_id: floor
                                for calendar_id in calendar_ids
                                if (
                                    floor := self._dataset.calendar_fact_floor(
                                        calendar_id
                                    )
                                )
                                is not None
                            }
                        ),
                    )
                    issues.extend(warmup_resolution.issues)
                issues.extend(
                    self._mandatory_coverage_issues(request, axis.resolved_sessions)
                )
            resolved_timezone = axis.timezone
            compatibility_status = axis.status
            session_signature = axis.session_signature
            differences = axis.differences
        else:
            # No resolvable scope at all: fabricate the minimal structurally
            # valid incompatible evidence so the blocked report stays legal.
            axis = None
            resolved_timezone = None
            compatibility_status = CalendarAxisStatus.INCOMPATIBLE
            session_signature = ""
            differences = ()

        blocked = any(issue.severity is IssueSeverity.ERROR for issue in issues)
        # Keep the report internally consistent: a blocked run must never
        # mount a *ready* warmup resolution (the report contract forbids
        # it), while a blocked warmup resolution may stay mounted so its
        # structured issues and calendar-difference evidence survive for
        # audit.  Warmup axis differences are carried inside the mounted
        # resolution's issues; the formal-axis differences field stays
        # reserved for the formal window.
        if blocked and (
            warmup_resolution is not None
            and warmup_resolution.status is WarmupStatus.READY
        ):
            warmup_resolution = None
        formal_sessions: tuple[SessionPoint, ...] = (
            () if blocked or axis is None else axis.resolved_sessions
        )
        warmup_sessions: tuple[SessionPoint, ...] = (
            ()
            if blocked
            or warmup_resolution is None
            or warmup_resolution.status is not WarmupStatus.READY
            else warmup_resolution.resolved_sessions
        )

        return DataPreflightReport(
            status=PreflightStatus.BLOCKED if blocked else PreflightStatus.READY,
            generated_at=self._dataset.clock,
            provider_key=request.provider_key,
            capability_manifest_version=self._manifest_version,
            requested_window=request.requested_window,
            scope_mode=request.instrument_scope_mode,
            resolved_calendar_ids=calendar_ids,
            resolved_calendar_definitions=self._resolved_definitions(calendar_ids),
            resolved_timezone=resolved_timezone,
            calendar_axis_policy=request.calendar_axis_policy,
            calendar_compatibility_status=compatibility_status,
            calendar_session_signature=session_signature,
            resolved_sessions=formal_sessions,
            warmup_sessions=warmup_sessions,
            max_lookback_sessions=request.max_lookback_sessions,
            knowledge_as_of=request.knowledge_as_of,
            non_strict_pit_capabilities=(),
            consistency_mode=request.consistency_mode,
            consistency_token_capability=(
                request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
                and request.consistency_token_contract is not None
            ),
            consistency_token_contract=(
                request.consistency_token_contract
                if request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
                else None
            ),
            data_chunk_policy=request.data_chunk_policy,
            data_chunk_size_sessions=request.data_chunk_size_sessions,
            required_capabilities=request.required_capabilities,
            rule_package=request.rule_package,
            rule_exception_set=request.rule_exception_set,
            static_instrument_ids=request.static_instrument_ids,
            mandatory_instrument_ids=request.mandatory_instrument_ids,
            strategy_price_bases=request.strategy_price_bases,
            engine_price_basis=request.engine_price_basis,
            data_contract_version=request.data_contract_version,
            frequency=request.frequency,
            warmup_sessions_count=request.warmup_sessions,
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            allowed_settlement_rule_class=request.allowed_settlement_rule_class,
            adjustment_series_policy=request.adjustment_series_policy,
            quality_mode=request.quality_mode,
            issues=tuple(issues),
            warmup_resolution=warmup_resolution,
            warmup_resolution_signature=(
                warmup_resolution.resolution_signature
                if warmup_resolution is not None
                else None
            ),
            calendar_axis_differences=differences,
        )

    def _resolved_definitions(
        self, calendar_ids: tuple[str, ...]
    ) -> tuple[CalendarDefinition, ...]:
        definitions: list[CalendarDefinition] = []
        for calendar_id in calendar_ids:
            definitions.extend(
                definition
                for definition in self._dataset.calendar_definitions
                if definition.calendar_id == calendar_id
            )
        return tuple(definitions)

    def _mandatory_coverage_issues(
        self,
        request: DataPreflightRequest,
        formal_sessions: Sequence[SessionPoint],
    ) -> list[PreflightIssue]:
        """Strict-mode coverage gate for mandatory instruments.

        Every mandatory instrument must hold a complete raw daily bar on
        every formal session; static-only instruments are checked for
        existence only, so deliberate gaps remain expressible in fixtures.
        """

        issues: list[PreflightIssue] = []
        if request.quality_mode is not QualityMode.STRICT:
            return issues
        for instrument_id in request.mandatory_instrument_ids:
            missing = [
                point.session_date
                for point in formal_sessions
                if (
                    bar := self._dataset.bar_at(
                        instrument_id,
                        request.frequency,
                        PriceBasis.RAW,
                        point.session_date,
                    )
                )
                is None
                or bar.evidence.quality_status is not QualityStatus.COMPLETE
            ]
            if missing:
                issues.append(
                    PreflightIssue(
                        code=ISSUE_MANDATORY_BAR_COVERAGE_MISSING,
                        severity=IssueSeverity.ERROR,
                        scope=SCOPE_FORMAL,
                        message=(
                            f"严格质量模式下强制标的存在 {len(missing)} 个正式会话"
                            "缺少完整 Bar 数据，已阻断回测"
                        ),
                        instrument_id=instrument_id,
                        field="resolved_sessions",
                        details={
                            "missing_session_count": len(missing),
                            "first_missing_date": missing[0].isoformat(),
                            "last_missing_date": missing[-1].isoformat(),
                        },
                    )
                )
        return issues

    # ------------------------------------------------------------------
    # Fact access and consistency-token internals
    # ------------------------------------------------------------------

    def _raw_bars(
        self,
        instrument_ids: Sequence[UUID],
        frequency: str,
        start_day: date,
        end_day: date,
    ) -> tuple[Bar, ...]:
        """Single funnel through which chunk reads touch the bar index.

        Fault-injection subclasses override exactly this method; every
        business query re-validates whatever comes back, so injected
        future or out-of-window facts still cannot reach a strategy.
        """

        self._read_count += 1
        return self._dataset.bars_in_range(
            instrument_ids, frequency, PriceBasis.RAW, start_day, end_day
        )

    def _coverage_envelope(self) -> date | None:
        floors = [
            floor
            for floor in self._dataset._calendar_fact_floor.values()
            if floor is not None
        ]
        return min(floors) if floors else None

    def _revision_snapshot(self) -> tuple[object, ...]:
        """The current repeatable-read revision vector as one comparable tuple.

        Transitional runs capture this snapshot when a chunk opens and
        require it to stay unchanged for the chunk's whole lifetime.
        """

        return (
            self._dataset.fixture_revision,
            self._revision,
            self._coverage_envelope(),
        )

    def _covers_fact_type(self, capability: DataCapability) -> bool:
        """Whether the dataset actually backs one declared fact type."""

        if capability is DataCapability.BARS:
            return bool(self._dataset.bars)
        if capability is DataCapability.CALENDARS:
            return bool(self._dataset.calendar_facts)
        if capability is DataCapability.COVERAGE:
            return True
        return False

    def _token_digest(
        self,
        *,
        formal_session_ids: Sequence[str],
        warmup_session_ids: Sequence[str],
        chunk_index: int,
        first_session_id: str,
        last_session_id: str,
        fact_types: Sequence[DataCapability],
    ) -> str:
        """Deterministic logical-token digest over the revision vector.

        The payload binds everything the first version promises: fixture
        revision, current revision counter, the historical coverage
        envelope, the frozen formal and warmup session ids, the chunk
        boundaries, and the declared fact types.  Only this irreversible
        digest is ever published.
        """

        envelope = self._coverage_envelope()
        payload = {
            "contract": {"key": CHUNK_TOKEN_CONTRACT.key, "version": CHUNK_TOKEN_CONTRACT.version},
            "fixture_revision": self._dataset.fixture_revision,
            "revision": self._revision,
            "coverage_envelope": {
                "earliest_provable_date": envelope.isoformat() if envelope else None,
            },
            "formal_session_ids": list(formal_session_ids),
            "warmup_session_ids": list(warmup_session_ids),
            "chunk": {
                "index": chunk_index,
                "first_session_id": first_session_id,
                "last_session_id": last_session_id,
                "fact_types": [capability.value for capability in fact_types],
            },
        }
        return canonical_hash(payload)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SessionConsistencyContext(DataConsistencyContext):
    mode: ConsistencyMode
    token_contract: ContractRef | None
    context_summary: Mapping[str, object]


class MemoryDataSession:
    """Authoritative session over a :class:`MemoryDataSet`.

    Formal and warmup sessions come exclusively from the reused
    ``strict_compatible@1`` resolution and bounded warmup resolver;
    preflight additionally enforces the memory-fact checks (provider key,
    capability/frequency/basis support, instrument existence, and strict
    mandatory-instrument coverage).  Chunk opening validates boundaries
    against the frozen formal sessions only -- warmup never participates
    in chunk numbering.
    """

    def __init__(self, *, provider: MemoryDataProvider, request: DataRequest) -> None:
        self._provider = provider
        self._request = request
        self._state = DataSessionState.CREATED
        self._report: DataPreflightReport | None = None
        self._resolved_sessions: tuple[SessionPoint, ...] | None = None
        self._warmup_sessions: tuple[SessionPoint, ...] | None = None
        # Pinned once when the preflight completes: under the transitional
        # repeatable-read mode every chunk of this run must validate against
        # this ONE snapshot, so a revision between chunks fails the next
        # chunk instead of silently starting a new read window.
        self._revision_snapshot: tuple[object, ...] | None = None

    def __enter__(self) -> "MemoryDataSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool | None:
        self.close()
        return None

    @property
    def state(self):
        """Current lifecycle state."""

        return self._state

    def close(self) -> None:
        """Close the session; no new chunks or business queries afterwards."""

        self._state = DataSessionState.CLOSED

    def _assert_not_closed(self) -> None:
        if self._state is DataSessionState.CLOSED:
            raise DataSessionClosedError(
                "the data session is closed; no new chunks or queries may run",
                details={"session_state": self._state.value},
            )

    @property
    def resolved_sessions(self) -> tuple[SessionPoint, ...]:
        """Frozen formal sessions; forbidden before a completed preflight."""

        if self._state is DataSessionState.CREATED:
            raise InvalidDataRequestError(
                "resolved_sessions are not available before a completed preflight"
            )
        if self._resolved_sessions is None:
            # A session closed before its preflight completed never froze
            # any sessions; fail with the stable contract error instead of
            # leaking an internal assertion.
            raise DataSessionClosedError(
                "the data session was closed before its preflight "
                "completed; no frozen formal sessions exist",
                details={"session_state": self._state.value},
            )
        return self._resolved_sessions

    @property
    def warmup_sessions(self) -> tuple[SessionPoint, ...]:
        """Frozen warmup sessions; forbidden before a completed preflight."""

        if self._state is DataSessionState.CREATED:
            raise InvalidDataRequestError(
                "warmup_sessions are not available before a completed preflight"
            )
        if self._warmup_sessions is None:
            raise DataSessionClosedError(
                "the data session was closed before its preflight "
                "completed; no frozen warmup sessions exist",
                details={"session_state": self._state.value},
            )
        return self._warmup_sessions

    @property
    def consistency_context(self) -> DataConsistencyContext:
        """Read-only consistency face of this session (no secrets)."""

        return _SessionConsistencyContext(
            mode=self._request.consistency_mode,
            token_contract=self._request.consistency_token_contract,
            context_summary=MappingProxyType(
                {
                    "session_state": self._state.value,
                    "resolved_session_count": (
                        len(self._resolved_sessions)
                        if self._resolved_sessions is not None
                        else None
                    ),
                    "warmup_session_count": (
                        len(self._warmup_sessions)
                        if self._warmup_sessions is not None
                        else None
                    ),
                }
            ),
        )

    def preflight(
        self, request: DataPreflightRequest | None = None
    ) -> DataPreflightReport:
        """Run the authoritative preflight exactly once, from ``created``."""

        if self._state is not DataSessionState.CREATED:
            raise InvalidDataRequestError(
                "preflight must run exactly once from the created state; "
                f"current state is {self._state.value}"
            )
        if request is not None:
            # Type-check before the field-by-field comparison: an arbitrary
            # object would otherwise surface as a bare AttributeError.
            if not isinstance(request, DataPreflightRequest):
                raise InvalidDataRequestError(
                    "request must be a DataPreflightRequest instance"
                )
            if not self._matches_frozen_intent(request):
                raise InvalidDataRequestError(
                    "the preflight request must match the frozen session "
                    "request on every shared business field"
                )
        report = self._provider._build_preflight_report(
            self._request,
            # The calendars frozen at admission are authoritative; the
            # re-check must not silently re-derive a different axis.
            frozen_calendar_ids=self._request.resolved_calendar_ids or None,
        )
        self._report = report
        self._resolved_sessions = report.resolved_sessions
        self._warmup_sessions = report.warmup_sessions
        self._state = (
            DataSessionState.BLOCKED
            if report.status is PreflightStatus.BLOCKED
            else DataSessionState.READY
        )
        # Freeze the repeatable-read revision vector exactly once, before
        # the run starts: all chunks of this session share it.
        if self._state is DataSessionState.READY:
            self._revision_snapshot = self._provider._revision_snapshot()
        return report

    @property
    def report(self) -> DataPreflightReport | None:
        """The immutable preflight report, or ``None`` before preflight."""

        return self._report

    def _matches_frozen_intent(self, request: DataPreflightRequest) -> bool:
        """Compare the original intent with the frozen request by business.

        A session opened from a frozen ``DataRequest`` may legitimately
        receive the original unresolved :class:`DataPreflightRequest` for
        its authoritative re-check; the admission-only fields that
        preflight itself added (resolved calendars, time zone, hashes) are
        not part of the comparison.
        """

        for field_name in DataPreflightRequest.__dataclass_fields__:
            if getattr(request, field_name) != getattr(self._request, field_name):
                return False
        return True

    # ------------------------------------------------------------------
    # Chunk lifecycle
    # ------------------------------------------------------------------

    def _chunk_boundaries(self) -> tuple[tuple[int, int], ...]:
        """Half-open session-index ranges of every fixed 20-session chunk."""

        size = self._request.data_chunk_size_sessions
        total = len(self._resolved_sessions or ())
        return tuple(
            (offset, min(offset + size, total)) for offset in range(0, total, size)
        )

    def open_chunk(self, query: DataChunkQuery) -> "MemoryDataChunkSession":
        """Open one legal fixed chunk as its own context manager."""

        self._assert_not_closed()
        if self._state is DataSessionState.CREATED:
            raise InvalidDataRequestError(
                "open_chunk requires a completed ready preflight"
            )
        if self._state is DataSessionState.BLOCKED:
            raise InvalidDataRequestError(
                "a blocked session exposes no official chunks"
            )
        assert self._resolved_sessions is not None
        boundaries = self._chunk_boundaries()
        if query.chunk_index >= len(boundaries):
            raise InvalidDataRequestError(
                "chunk_index is out of range of the frozen official sessions",
                details={
                    "chunk_index": query.chunk_index,
                    "chunk_count": len(boundaries),
                },
            )
        start, end = boundaries[query.chunk_index]
        expected_first = self._resolved_sessions[start].session_id
        expected_last = self._resolved_sessions[end - 1].session_id
        if (
            query.first_session_id != expected_first
            or query.last_session_id != expected_last
        ):
            raise InvalidDataRequestError(
                "chunk boundaries do not match the frozen official sessions",
                details={
                    "chunk_index": query.chunk_index,
                    "expected_first_session_id": expected_first,
                    "expected_last_session_id": expected_last,
                    "actual_first_session_id": query.first_session_id,
                    "actual_last_session_id": query.last_session_id,
                },
            )
        unsupported = [
            capability
            for capability in query.fact_types
            if capability not in _SERVABLE_CHUNK_FACT_TYPES
        ]
        if unsupported:
            raise UnsupportedCapabilityError(
                "this chunk cannot serve the requested fact types",
                details={
                    "unsupported_fact_types": [
                        capability.value for capability in unsupported
                    ],
                },
            )
        chunk_sessions = self._resolved_sessions[start:end]
        warmup = self._warmup_sessions or ()
        envelope = CoverageEnvelope(
            chunk_first_session_date=chunk_sessions[0].session_date,
            chunk_last_session_date=chunk_sessions[-1].session_date,
            fact_types=query.fact_types,
            warmup_first_session_date=(
                warmup[0].session_date if warmup else None
            ),
            warmup_session_count=len(warmup),
            lookback_envelope_sessions=self._request.max_lookback_sessions,
        )
        digest_spec = {
            "formal_session_ids": [point.session_id for point in self._resolved_sessions],
            "warmup_session_ids": [point.session_id for point in warmup],
            "chunk_index": query.chunk_index,
            "first_session_id": expected_first,
            "last_session_id": expected_last,
            "fact_types": query.fact_types,
        }
        mode = self._request.consistency_mode
        if mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN:
            issued_digest = self._provider._token_digest(**digest_spec)
            revision_snapshot: tuple[object, ...] | None = None
        else:
            # transitional_repeatable_read issues no logical token; every
            # chunk validates against the snapshot pinned once for the
            # whole session, so a mid-run revision expires all remaining
            # chunks before any strategy call.
            assert self._revision_snapshot is not None
            issued_digest = None
            revision_snapshot = self._revision_snapshot
        return MemoryDataChunkSession(
            provider=self._provider,
            session=self,
            chunk_index=query.chunk_index,
            formal_end_index=end,
            sessions=chunk_sessions,
            fact_types=query.fact_types,
            coverage_envelope=envelope,
            issued_digest=issued_digest,
            revision_snapshot=revision_snapshot,
            digest_spec=digest_spec,
        )


# ---------------------------------------------------------------------------
# Chunk session
# ---------------------------------------------------------------------------


class MemoryDataChunkSession:
    """One consistent fixed window over official sessions of a memory run.

    Business queries require a prior successful ``validate_consistency()``;
    a failed validation blocks every subsequent business read inside the
    chunk.  Results are immutable tuples, ascending by trade date, with
    gaps preserved and every row re-checked against the visibility
    boundary before it is returned.
    """

    def __init__(
        self,
        *,
        provider: MemoryDataProvider,
        session: MemoryDataSession,
        chunk_index: int,
        formal_end_index: int,
        sessions: tuple[SessionPoint, ...],
        fact_types: tuple[DataCapability, ...],
        coverage_envelope: CoverageEnvelope,
        issued_digest: str | None,
        revision_snapshot: tuple[object, ...] | None = None,
        digest_spec: dict[str, object],
    ) -> None:
        self._provider = provider
        self._session = session
        self._chunk_index = chunk_index
        # Exclusive index of this chunk's last session inside the frozen
        # formal tuple; bounds every read so later chunks cannot prefetch.
        self._formal_end_index = formal_end_index
        self._sessions = sessions
        self._fact_types = fact_types
        self._coverage_envelope = coverage_envelope
        self._issued_digest = issued_digest
        self._revision_snapshot = revision_snapshot
        self._digest_spec = digest_spec
        self._validation: ConsistencyTokenStatus | None = None
        self._closed = False
        mode = session._request.consistency_mode
        summary: dict[str, object] = {
            "chunk_session_count": len(sessions),
            "formal_session_count": len(session.resolved_sessions),
            "consistency_mode": mode.value,
            **dict(coverage_envelope.to_summary()),
        }
        if mode is ConsistencyMode.TRANSITIONAL_REPEATABLE_READ:
            # Result detail must mark the transitional mode and its resource
            # risk explicitly instead of presenting it as token consistency.
            summary["transitional_resource_risk"] = (
                "过渡模式在整个会话生命周期内保持可重复读，"
                "块级资源在会话关闭前不会释放"
            )
        self._evidence = DataConsistencyEvidence(
            chunk_index=chunk_index,
            first_session_id=sessions[0].session_id,
            last_session_id=sessions[-1].session_id,
            mode=mode,
            validation_status=ConsistencyValidation.NOT_VALIDATED,
            fact_types=fact_types,
            coverage_summary=summary,
            token_digest=issued_digest,
            failure_reason="consistency validation has not run yet",
        )

    def __enter__(self) -> "MemoryDataChunkSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool | None:
        self.close()
        return None

    # ------------------------------------------------------------------
    # Lifecycle and consistency
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close this chunk; further reads fail."""

        self._closed = True

    def _assert_open(self) -> None:
        if self._closed:
            raise DataSessionClosedError(
                "the chunk session is closed; no consistency check or "
                "business query may run",
                details={"chunk_index": self._chunk_index},
            )
        self._session._assert_not_closed()

    @property
    def consistency_evidence(self) -> DataConsistencyEvidence:
        """Persistable non-sensitive evidence about this chunk."""

        return self._evidence

    def validate_consistency(self) -> ConsistencyTokenStatus:
        """Validate the chunk token; must precede any business query."""

        self._assert_open()
        clock = self._provider.dataset.clock
        if self._session._request.consistency_mode is (
            ConsistencyMode.CHUNKED_LOGICAL_TOKEN
        ):
            current_digest = self._provider._token_digest(**self._digest_spec)
            if current_digest != self._issued_digest:
                status = ConsistencyValidation.EXPIRED
                reason = (
                    "the fixture revision vector advanced after this chunk "
                    "was opened; the chunk token expired"
                )
            else:
                status = ConsistencyValidation.VALID
                reason = None
        else:
            # transitional_repeatable_read: the pinned revision vector must
            # still match; no logical token exists to re-hash.
            assert self._revision_snapshot is not None
            if self._provider._revision_snapshot() != self._revision_snapshot:
                status = ConsistencyValidation.EXPIRED
                reason = (
                    "the fixture revision vector advanced after this chunk "
                    "was opened; the transitional repeatable-read snapshot "
                    "expired"
                )
            else:
                status = ConsistencyValidation.VALID
                reason = None
        if status is ConsistencyValidation.VALID:
            # A valid token must actually cover every declared fact type:
            # a declared capability without backing facts is a coverage
            # gap, never an empty result set.
            uncovered = [
                capability
                for capability in self._fact_types
                if not self._provider._covers_fact_type(capability)
            ]
            if uncovered:
                status = ConsistencyValidation.COVERAGE_INCOMPLETE
                reason = (
                    "the chunk token does not cover the declared fact "
                    "types with backing facts: "
                    + ", ".join(capability.value for capability in uncovered)
                )
        token_status = ConsistencyTokenStatus(
            status=status,
            validated_at=clock,
            covered_chunk=(
                self._chunk_index if status is ConsistencyValidation.VALID else None
            ),
            covered_fact_types=(
                self._fact_types if status is ConsistencyValidation.VALID else ()
            ),
            failure_reason=reason,
        )
        summary = dict(self._evidence.coverage_summary)
        if status is not ConsistencyValidation.VALID:
            # Persistable evidence must attribute the block to the data
            # consistency phase so result records never blame a later one.
            summary["failure_phase"] = "data_consistency"
        self._validation = token_status
        self._evidence = DataConsistencyEvidence(
            chunk_index=self._chunk_index,
            first_session_id=self._sessions[0].session_id,
            last_session_id=self._sessions[-1].session_id,
            mode=self._session._request.consistency_mode,
            validation_status=status,
            fact_types=self._fact_types,
            coverage_summary=summary,
            token_digest=self._issued_digest,
            validated_at=clock,
            failure_reason=reason,
        )
        return token_status

    def _authorized_instrument_ids(self) -> frozenset[UUID]:
        """The instrument scope frozen into the run's official request.

        Queries may only touch instruments the run was admitted for; the
        dataset holding more fixtures must not widen the authorization.
        """

        request = self._session._request
        return frozenset(
            [*request.static_instrument_ids, *request.mandatory_instrument_ids]
        )

    def _require_authorized_instruments(self, instrument_ids, operation: str) -> None:
        """Reject queries reaching outside the frozen run scope."""

        authorized = self._authorized_instrument_ids()
        strangers = sorted(
            (
                instrument_id
                for instrument_id in instrument_ids
                if instrument_id not in authorized
            ),
            key=str,
        )
        if strangers:
            raise InvalidDataRequestError(
                f"{operation} requested instruments outside the run's "
                "frozen fixed scope",
                details={
                    "unauthorized_instrument_ids": [
                        str(instrument_id) for instrument_id in strangers
                    ],
                },
            )

    def _require_declared_fact_type(
        self, capability: DataCapability, operation: str
    ) -> None:
        """Reject queries reaching beyond this chunk's declared fact types.

        ``open_chunk`` freezes the fact types the token was issued for;
        letting a later business query serve an undeclared type would both
        bypass the per-type serving rules and make the persisted
        consistency evidence overstate what the token actually covered.
        """

        if capability not in self._fact_types:
            raise InvalidDataRequestError(
                f"{operation} requested fact type {capability.value} which "
                "this chunk did not declare when it was opened",
                details={
                    "chunk_index": self._chunk_index,
                    "declared_fact_types": [
                        item.value for item in self._fact_types
                    ],
                    "requested_fact_type": capability.value,
                },
            )

    def _guard_business_query(self, operation: str) -> None:
        """Block business reads until validation passed and still holds."""

        self._assert_open()
        validation = self._validation
        if validation is None:
            raise ConsistencyNotValidatedError(
                f"{operation} ran before validate_consistency() succeeded",
                details={"chunk_index": self._chunk_index},
            )
        if validation.status is ConsistencyValidation.VALID:
            return
        if validation.status is ConsistencyValidation.EXPIRED:
            raise ConsistencyTokenExpiredError(
                "the chunk consistency token expired; business reads are blocked",
                details={"chunk_index": self._chunk_index},
            )
        if validation.status is ConsistencyValidation.COVERAGE_INCOMPLETE:
            raise ConsistencyCoverageIncompleteError(
                "the chunk token does not cover the declared fact types; "
                "business reads are blocked",
                details={"chunk_index": self._chunk_index},
            )
        raise ConsistencyTokenInvalidError(
            "the chunk consistency token is invalid; business reads are blocked",
            details={"chunk_index": self._chunk_index},
        )

    # ------------------------------------------------------------------
    # Business queries
    # ------------------------------------------------------------------

    def instruments(self, query: InstrumentQuery) -> tuple[InstrumentSpec, ...]:
        """Resolve full specs for known identities valid at ``effective_at``."""

        self._guard_business_query("instruments")
        self._require_authorized_instruments(query.instrument_ids, "instruments")
        rows: list[InstrumentSpec] = []
        for instrument_id in query.instrument_ids:
            spec = self._provider.dataset.instrument(instrument_id)
            if spec is None:
                continue
            if spec.valid_from > query.effective_at:
                continue
            if spec.valid_to is not None and query.effective_at >= spec.valid_to:
                continue
            rows.append(spec)
        return tuple(sorted(rows, key=lambda spec: str(spec.instrument_id)))

    def instrument_mappings(
        self, query: InstrumentMappingQuery
    ) -> tuple[object, ...]:
        raise UnsupportedCapabilityError(
            "the memory fixture does not implement instrument_mappings"
        )

    def trading_rules(self, query: TradingRuleQuery) -> tuple[object, ...]:
        raise UnsupportedCapabilityError(
            "the memory fixture does not implement trading_rules"
        )

    def trading_status(self, query: TradingStatusQuery) -> tuple[object, ...]:
        raise UnsupportedCapabilityError(
            "the memory fixture does not implement trading_status"
        )

    def universe(self, query: UniverseQuery) -> tuple[InstrumentSpec, ...]:
        raise UnsupportedCapabilityError(
            "the memory fixture does not implement universe queries"
        )

    def bars(self, query: BarQuery) -> tuple[Bar, ...]:
        """Serve bars for an explicit range or a lookback window.

        Reads are bounded by the current chunk: an explicit range may never
        extend past the chunk's last session (no whole-window prefetch), and
        a lookback may only draw on warmup, earlier formal sessions, and
        the current chunk's sessions.  Explicit ranges keep their gaps; a
        lookback demands that the *provider result* contains a complete
        bar for every requested instrument on every selected session and
        fails with ``history_incomplete`` otherwise.  Every returned row is
        re-checked against the query boundary, so future-dated,
        out-of-window, or wrong-instrument facts can never leak.
        """

        self._guard_business_query("bars")
        if not isinstance(query, BarQuery):
            raise InvalidDataRequestError("query must be a BarQuery")
        self._require_declared_fact_type(DataCapability.BARS, "bars")
        self._require_authorized_instruments(query.instrument_ids, "bars")
        boundary = query.boundary
        chunk_last_day = self._sessions[-1].session_date
        if isinstance(query.window, DateRange):
            if query.window.end_date > chunk_last_day:
                raise InvalidDataRequestError(
                    "the query end date exceeds the current chunk's last "
                    "official session; cross-chunk prefetch is forbidden",
                    details={
                        "chunk_index": self._chunk_index,
                        "chunk_last_session_date": chunk_last_day.isoformat(),
                        "requested_end_date": query.window.end_date.isoformat(),
                    },
                )
            start_day = query.window.start_date
            end_day = query.window.end_date
            rows = list(
                self._provider._raw_bars(
                    query.instrument_ids, query.frequency, start_day, end_day
                )
            )
            wanted_days: set[date] | None = None
        else:
            lookback = query.window
            # Defensive re-check: the DTO already enforces the limit, but a
            # bypassed constructor must still fail before any index access.
            if lookback.sessions > MAX_LOOKBACK_SESSIONS:
                raise _lookback_limit_error(lookback.sessions)
            pool = [
                *self._session.warmup_sessions,
                # History plus the current chunk only: sessions of later
                # chunks stay invisible to this chunk's lookback reads.
                *self._session.resolved_sessions[: self._formal_end_index],
            ]
            last_eligible_day = lookback.end_at.date()
            eligible = [
                point
                for point in pool
                if point.session_date < last_eligible_day
                or (
                    boundary.include_cutoff_day
                    and point.session_date == boundary.cutoff_date
                )
            ]
            if len(eligible) < lookback.sessions:
                raise HistoryIncompleteError(
                    "the lookback window requests more sessions than the "
                    "bounded history up to this chunk provides",
                    details={
                        "requested": lookback.sessions,
                        "available": len(eligible),
                        "chunk_index": self._chunk_index,
                    },
                )
            window_points = eligible[-lookback.sessions :]
            wanted_days = {point.session_date for point in window_points}
            raw_rows = list(
                self._provider._raw_bars(
                    query.instrument_ids,
                    query.frequency,
                    min(wanted_days),
                    max(wanted_days),
                )
            )
            start_day, end_day = min(wanted_days), max(wanted_days)
            # Visibility is asserted on the provider's RAW result *before*
            # any filtering: a future-dated or out-of-window injected bar
            # must surface as provider_contract_violation, never be
            # silently dropped by the completeness filter below.
            _assert_rows_visible(
                raw_rows,
                boundary=boundary,
                start_day=start_day,
                end_day=end_day,
                wanted_days=wanted_days,
                frequency=query.frequency,
                instrument_ids=frozenset(query.instrument_ids),
            )
            rows = [bar for bar in raw_rows if bar.trade_date in wanted_days]
            # Completeness is judged on the provider *result*, never on the
            # dataset index: a fault-injecting source that drops a bar must
            # surface as history_incomplete, not as a shortened window.
            actual: dict[tuple[UUID, date], Bar] = {
                (bar.instrument_id, bar.trade_date): bar for bar in rows
            }
            for instrument_id in query.instrument_ids:
                for day in sorted(wanted_days):
                    bar = actual.get((instrument_id, day))
                    if bar is None:
                        raise HistoryIncompleteError(
                            "a session inside the lookback window is missing "
                            "from the provider result; the window is not "
                            "shortened",
                            details={
                                "instrument_id": str(instrument_id),
                                "missing_date": day.isoformat(),
                                "requested": lookback.sessions,
                            },
                        )
                    if bar.evidence.quality_status is not QualityStatus.COMPLETE:
                        raise HistoryIncompleteError(
                            "a session inside the lookback window lacks a "
                            "complete bar; the window is not shortened",
                            details={
                                "instrument_id": str(instrument_id),
                                "missing_date": day.isoformat(),
                                "requested": lookback.sessions,
                            },
                        )
        _assert_rows_visible(
            rows,
            boundary=boundary,
            start_day=start_day,
            end_day=end_day,
            wanted_days=wanted_days,
            frequency=query.frequency,
            instrument_ids=frozenset(query.instrument_ids),
        )
        rows.sort(key=lambda bar: (bar.trade_date, str(bar.instrument_id)))
        return tuple(rows)

    def ticks(self, query: TickQuery) -> tuple[object, ...]:
        raise UnsupportedCapabilityError("the memory fixture does not serve ticks")

    def values(self, query: DataValueQuery) -> tuple[object, ...]:
        raise UnsupportedCapabilityError("the memory fixture does not serve values")

    def adjusted_series(self, query: AdjustedSeriesQuery) -> tuple[object, ...]:
        raise UnsupportedCapabilityError(
            "the memory fixture does not serve adjusted_series"
        )

    def corporate_actions(
        self, query: CorporateActionQuery
    ) -> tuple[object, ...]:
        raise UnsupportedCapabilityError(
            "the memory fixture does not serve corporate_actions"
        )

    def coverage(self, query: CoverageQuery) -> DataCoverageReport:
        """Coverage accounting over one capability and explicit window.

        Like every business query, coverage is bounded by the current
        chunk: the window may not extend past the chunk's last session and
        only warmup, earlier formal sessions, and the current chunk count.
        """

        self._guard_business_query("coverage")
        self._require_declared_fact_type(DataCapability.COVERAGE, "coverage")
        # The audited capability must itself be declared: a coverage report
        # about a fact type the token never declared would fabricate
        # evidence about facts this chunk cannot serve.
        self._require_declared_fact_type(query.capability, "coverage")
        self._require_authorized_instruments(query.instrument_ids, "coverage")
        if query.capability not in self._provider._manifest.capabilities:
            raise UnsupportedCapabilityError(
                "coverage is not offered for the requested capability",
                details={"capability": query.capability.value},
            )
        chunk_last_day = self._sessions[-1].session_date
        if query.window.end_date > chunk_last_day:
            raise InvalidDataRequestError(
                "the coverage window exceeds the current chunk's last "
                "official session; cross-chunk prefetch is forbidden",
                details={
                    "chunk_index": self._chunk_index,
                    "chunk_last_session_date": chunk_last_day.isoformat(),
                    "requested_end_date": query.window.end_date.isoformat(),
                },
            )
        pool = [
            *self._session.warmup_sessions,
            *self._session.resolved_sessions[: self._formal_end_index],
        ]
        days = sorted(
            {
                point.session_date
                for point in pool
                if query.window.start_date
                <= point.session_date
                <= query.window.end_date
            }
        )
        expected = len(query.instrument_ids) * len(days)
        complete = partial = invalid = 0
        missing_days: list[date] = []
        if expected:
            for day in days:
                day_complete = True
                for instrument_id in query.instrument_ids:
                    bar = self._provider.dataset.any_bar_on(
                        instrument_id, PriceBasis.RAW, day
                    )
                    if bar is None:
                        day_complete = False
                    elif bar.evidence.quality_status is QualityStatus.COMPLETE:
                        complete += 1
                    elif bar.evidence.quality_status is QualityStatus.PARTIAL:
                        partial += 1
                        day_complete = False
                    else:
                        invalid += 1
                        day_complete = False
                if not day_complete:
                    missing_days.append(day)
        found = complete + partial + invalid
        unavailable_count = expected - found
        if expected == 0 or found == 0:
            quality = QualityStatus.UNAVAILABLE
        elif invalid:
            quality = QualityStatus.INVALID
        elif partial or unavailable_count:
            quality = QualityStatus.PARTIAL
        else:
            quality = QualityStatus.COMPLETE
        return DataCoverageReport(
            requested_window=query.window,
            capability=query.capability,
            instrument_ids=query.instrument_ids,
            expected_count=expected,
            complete_count=complete,
            partial_count=partial,
            invalid_count=invalid,
            unavailable_count=unavailable_count,
            quality_status=quality,
            missing_ranges=_merge_missing_ranges(missing_days),
        )


def _assert_rows_visible(
    rows: list[Bar],
    *,
    boundary,
    start_day: date,
    end_day: date,
    wanted_days: set[date] | None,
    frequency: str,
    instrument_ids: frozenset[UUID],
) -> None:
    """Re-check every provider-returned row before it reaches a strategy.

    This is the second line of defence: even a faulty fixture (or a
    fault-injecting subclass) cannot leak future-dated, out-of-window,
    wrong-basis, wrong-frequency, or wrong-instrument facts past this
    point.
    """

    cutoff_day = boundary.cutoff_date
    for bar in rows:
        details = {
            "instrument_id": str(bar.instrument_id),
            "trade_date": bar.trade_date.isoformat(),
        }
        if bar.instrument_id not in instrument_ids:
            raise ProviderContractViolationError(
                "provider returned a bar for an instrument outside the "
                "requested set",
                details=details,
            )
        if bar.price_basis is not PriceBasis.RAW:
            raise ProviderContractViolationError(
                "provider returned a bar with a non-raw price basis",
                details=details,
            )
        if bar.frequency != frequency:
            raise ProviderContractViolationError(
                "provider returned a bar with an unexpected frequency",
                details={**details, "frequency": bar.frequency},
            )
        if not (start_day <= bar.trade_date <= end_day) or (
            wanted_days is not None and bar.trade_date not in wanted_days
        ):
            raise ProviderContractViolationError(
                "provider returned a bar outside the requested window",
                details=details,
            )
        if bar.trade_date > cutoff_day or (
            bar.trade_date == cutoff_day and not boundary.include_cutoff_day
        ):
            raise ProviderContractViolationError(
                "provider returned a bar beyond the data_cutoff visibility "
                "boundary",
                details={
                    **details,
                    "data_cutoff": boundary.data_cutoff.isoformat(),
                },
            )


def _lookback_limit_error(requested: int) -> Exception:
    from app.backtesting.data.errors import LookbackSessionsLimitExceededError

    return LookbackSessionsLimitExceededError(
        f"sessions {requested} exceeds the maximum of {MAX_LOOKBACK_SESSIONS}",
        details={"requested": requested, "maximum": MAX_LOOKBACK_SESSIONS},
    )


def _merge_missing_ranges(days: Sequence[date]) -> tuple[DateRange, ...]:
    """Merge consecutive missing dates into inclusive ``DateRange`` values."""

    if not days:
        return ()
    ranges: list[DateRange] = []
    start = previous = days[0]
    for day in days[1:]:
        if (day - previous).days == 1:
            previous = day
            continue
        ranges.append(DateRange(start_date=start, end_date=previous))
        start = previous = day
    ranges.append(DateRange(start_date=start, end_date=previous))
    return tuple(ranges)
