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

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
import json
import inspect
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import (
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
    CalendarAxisStatus,
    CalendarAxisDifference,
    calendar_snapshot_usage,
    CalendarAxisDifferenceField,
    CalendarDefinition,
    CalendarSessionFact,
    CalendarSnapshot,
    CalendarSnapshotRequest,
    CalendarPITContext,
    CAPABILITY_SUSPENSION,
    CAPABILITY_OPENING_AVAILABILITY,
    CAPABILITY_PRICE_LIMIT_TRADABILITY,
    InMemoryCalendarAxisDataProvider,
    SessionPoint,
    normalize_calendar_id,
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
    UniverseCalendarNotPreflightedError,
    UniverseCapabilityMissingError,
    UniversePitBoundaryViolationError,
    UniversePreflightHashMismatchError,
    UniverseProviderContractViolationError,
    UniverseScopeUnresolvedError,
    UnsupportedCapabilityError,
    CalendarContractError,
    CalendarPreflightResourceLimitExceededError,
)
from app.backtesting.data.facts import Bar, InstrumentSpec
from app.backtesting.data.protocols import (
    ConsistencyTokenStatus,
    CoverageEnvelope,
    DataCapabilityManifest,
    DataConsistencyContext,
    DataConsistencyEvidence,
    InstrumentCoverageQualification,
)
from app.backtesting.data.reports import (
    DataCoverageReport,
    DataPreflightReport,
    PreflightIssue,
    canonical_hash,
    canonical_json,
)
from app.backtesting.data.requests import (
    CALENDAR_AXIS_POLICY,
    CHUNK_POLICY,
    DATA_CONTRACT_VERSION,
    MAX_LOOKBACK_SESSIONS,
    AdjustedSeriesQuery,
    BarQuery,
    CapabilitySource,
    ConsistencyMode,
    ConsistencyValidation,
    ContractRef,
    CorporateActionQuery,
    CoverageQuery,
    DataCapability,
    DataChunkQuery,
    CoverageQualificationRequest,
    DataPreflightRequest,
    DataRequest,
    DataValueQuery,
    DateRange,
    InstrumentMappingQuery,
    InstrumentQuery,
    InstrumentScopeMode,
    IssueSeverity,
    InternalFixture,
    InternalFixtureCapability,
    FORMAL_PROFILE,
    INTERNAL_LINK_ACCEPTANCE_PROFILE,
    INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY,
    FORMAL_PROFILE,
    FORMAL_PROFILE_KEY,
    PreflightProfile,
    PreflightProfileRegistry,
    PitSupport,
    PreflightStatus,
    PriceBasis,
    QualityMode,
    QualityStatus,
    TickQuery,
    TradingRuleQuery,
    TradingStatusQuery,
    UniverseQuery,
    UniverseQueryPolicy,
)
from app.backtesting.data.sessions import (
    DataSessionState,
    evaluate_calendar_capability_gate,
)
from app.backtesting.data.warmup import (
    NO_FORMAL_SESSIONS,
    SCOPE_FORMAL,
    CoverageBoundedWarmupSessionResolver,
    WarmupCoverageStatus,
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


def _calendar_issue_code(
    code: str,
    details: Mapping[str, object] | None = None,
) -> str:
    """Map a stable calendar exception code to its report issue spelling."""

    # Snapshot preparation can prove an insufficient warmup span before the
    # payload batch is read.  Preserve the existing warmup issue contract in
    # that case instead of exposing the transport-level coverage-unknown
    # wrapper as the primary report code.
    if isinstance(details, Mapping) and details.get("cause_code") == "warmup_coverage_insufficient":
        return "WARMUP_COVERAGE_INSUFFICIENT"
    return {
        "calendar_fact_missing": "CALENDAR_FACT_MISSING",
        "calendar_fact_ambiguous": "CALENDAR_FACT_AMBIGUOUS",
        "calendar_definition_missing": "CALENDAR_DEFINITION_MISSING",
        "calendar_definition_ambiguous": "CALENDAR_DEFINITION_AMBIGUOUS",
        "calendar_session_unresolved": "CALENDAR_SESSION_UNRESOLVED",
        "calendar_pit_metadata_missing": "CALENDAR_PIT_METADATA_MISSING",
        "data_cutoff_exceeded": "DATA_CUTOFF_EXCEEDED",
        "data_cutoff_required": "DATA_CUTOFF_REQUIRED",
        "calendar_timezone_inconsistent": "CALENDAR_TIMEZONE_INCONSISTENT",
        "calendar_timezone_mismatch": "CALENDAR_TIMEZONE_MISMATCH",
        "calendar_timezone_unsupported": "CALENDAR_TIMEZONE_UNSUPPORTED",
    }.get(code, code.upper())

_SERVABLE_CHUNK_FACT_TYPES = frozenset(
    # Universe reads are immutable metadata reads bound to the same chunk
    # consistency context.  They do not widen the formal chunk and therefore
    # can safely participate in the declared token fact set.
    {DataCapability.BARS, DataCapability.COVERAGE, DataCapability.UNIVERSE}
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
    # Optional canonical task-11 metadata.  Legacy task-02 fixtures leave
    # these empty and continue through the compatibility calendar path.
    calendar_registries: tuple[object, ...] = ()
    calendar_bindings: tuple[object, ...] = ()
    calendar_capabilities: tuple[object, ...] = ()
    calendar_source_priorities: tuple[object, ...] = ()
    # Explicit internal-link substitutes are injected by tests/callers.  The
    # dataset never derives a fixture from an empty table or an adapter
    # default, which keeps fixture evidence distinguishable from production.
    fixtures: tuple[InternalFixture, ...] = ()
    # Optional versioned PIT rows are kept separate from the legacy fixed
    # table.  They are only used when an explicit universe source is injected;
    # a dataset never promotes these rows to a dynamic catalogue by itself.
    pit_instruments: tuple[object, ...] = ()

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
        for name in (
            "calendar_registries",
            "calendar_bindings",
            "calendar_capabilities",
            "calendar_source_priorities",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        fixtures = tuple(self.fixtures)
        if any(not isinstance(item, InternalFixture) for item in fixtures):
            raise InvalidDataRequestError(
                "fixtures entries must be InternalFixture instances"
            )
        fixture_keys = {
            (item.fixture_key, item.fixture_version, item.capability)
            for item in fixtures
        }
        if len(fixture_keys) != len(fixtures):
            raise InvalidDataRequestError(
                "fixtures must not repeat one key/version/capability"
            )
        object.__setattr__(
            self,
            "fixtures",
            tuple(
                sorted(
                    fixtures,
                    key=lambda item: (
                        item.capability,
                        item.fixture_key,
                        str(item.fixture_version),
                        item.start_date,
                        item.end_date,
                    ),
                )
            ),
        )
        object.__setattr__(self, "pit_instruments", tuple(self.pit_instruments))

        definitions = tuple(self.calendar_definitions)
        object.__setattr__(
            self,
            "calendar_definitions",
            tuple(
                sorted(
                    definitions,
                    key=lambda item: (item.calendar_id, item.definition_version),
                )
            ),
        )
        facts = tuple(self.calendar_facts)
        seen_facts: set[tuple[str, date, object]] = set()
        seen_versions: set[tuple[str, int]] = set()
        for fact in facts:
            key = (fact.calendar_id, fact.session_date, fact.fact_id)
            if key in seen_facts:
                raise InvalidDataRequestError(
                    "duplicate calendar fact identity",
                    details={"calendar_id": fact.calendar_id, "session_date": fact.session_date.isoformat()},
                )
            version_key = (fact.logical_fact_key, fact.fact_version)
            if version_key in seen_versions:
                raise InvalidDataRequestError(
                    "duplicate calendar fact logical version",
                    details={"logical_fact_key": fact.logical_fact_key, "fact_version": fact.fact_version},
                )
            seen_facts.add(key)
            seen_versions.add(version_key)
        object.__setattr__(
            self,
            "calendar_facts",
            tuple(sorted(facts, key=lambda item: (item.calendar_id, item.session_date))),
        )

        instruments = tuple(self.instruments)
        pit_instruments = tuple(self.pit_instruments)
        # Keep all non-overlapping PIT versions for one stable identity.  A
        # duplicate interval is ambiguous and remains a fixture contract
        # violation; unlike the old one-row map this representation can
        # answer historical identity/display queries without current-row
        # fallback.
        grouped_instruments: dict[UUID, list[InstrumentSpec]] = {}
        for spec in (*instruments, *pit_instruments):
            instrument_id = getattr(spec, "instrument_id", None)
            if not isinstance(instrument_id, UUID):
                raise InvalidDataRequestError(
                    "instrument rows must carry a UUID instrument_id"
                )
            grouped_instruments.setdefault(instrument_id, []).append(spec)
        for instrument_id, versions in grouped_instruments.items():
            ordered = sorted(
                versions,
                key=lambda item: (
                    getattr(item, "valid_from", datetime.min.replace(tzinfo=UTC)),
                    getattr(item, "valid_to", None)
                    or datetime.max.replace(tzinfo=UTC),
                    str(getattr(item, "rule_package_reference", "")),
                ),
            )
            for previous, current in zip(ordered, ordered[1:]):
                previous_end = getattr(previous, "valid_to", None)
                current_start = getattr(current, "valid_from", None)
                if previous_end is None or current_start < previous_end:
                    raise InvalidDataRequestError(
                        "versioned instrument intervals overlap",
                        details={"instrument_id": str(instrument_id)},
                    )
        object.__setattr__(
            self,
            "instruments",
            tuple(
                sorted(
                    instruments,
                    key=lambda item: (
                        str(item.instrument_id),
                        getattr(item, "valid_from", datetime.min.replace(tzinfo=UTC)),
                        getattr(item, "valid_to", None)
                        or datetime.max.replace(tzinfo=UTC),
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "_instrument_versions",
            MappingProxyType(
                {
                    instrument_id: tuple(
                        sorted(
                            versions,
                            key=lambda item: (
                                getattr(
                                    item,
                                    "valid_from",
                                    datetime.min.replace(tzinfo=UTC),
                                ),
                                getattr(item, "valid_to", None)
                                or datetime.max.replace(tzinfo=UTC),
                            ),
                        )
                    )
                    for instrument_id, versions in grouped_instruments.items()
                }
            ),
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
            InMemoryCalendarAxisDataProvider(
                definitions,
                facts,
                registries=self.calendar_registries,
                bindings=self.calendar_bindings,
                capabilities=self.calendar_capabilities,
                source_priorities=self.calendar_source_priorities,
                fixture_revision=self.fixture_revision,
            ),
        )

    # ------------------------------------------------------------------
    # Read accessors (never expose mutable internals)
    # ------------------------------------------------------------------

    @property
    def calendar_axis_provider(self) -> InMemoryCalendarAxisDataProvider:
        """The strict_compatible@1 fact source built from this dataset."""

        return self._calendar_axis_provider

    def instrument(self, instrument_id: UUID) -> InstrumentSpec | None:
        """Return the latest fixed-fixture spec for one stable identity."""

        versions = self._instrument_versions.get(instrument_id, ())
        return versions[-1] if versions else None

    def instrument_at(
        self,
        instrument_id: UUID,
        effective_at: datetime,
        data_cutoff: datetime | None = None,
    ) -> InstrumentSpec | None:
        """Resolve one versioned spec at an explicit PIT coordinate.

        ``effective_at`` selects the market-valid interval; ``known_at`` on a
        richer fixture row, when present, is additionally bounded by
        ``data_cutoff``.  No current row is used when no version covers the
        requested date.
        """

        if not isinstance(instrument_id, UUID):
            raise InvalidDataRequestError("instrument_id must be a UUID")
        effective = _aware_datetime(effective_at, "effective_at")
        cutoff = (
            _aware_datetime(data_cutoff, "data_cutoff")
            if data_cutoff is not None
            else None
        )
        candidates = []
        for spec in self._instrument_versions.get(instrument_id, ()):
            valid_from = getattr(spec, "valid_from", None)
            valid_to = getattr(spec, "valid_to", None)
            if valid_from is not None and effective < valid_from:
                continue
            if valid_to is not None and effective >= valid_to:
                continue
            known_at = getattr(spec, "known_at", None)
            if cutoff is not None and known_at is not None and known_at > cutoff:
                continue
            candidates.append(spec)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                getattr(item, "valid_from", datetime.min.replace(tzinfo=UTC)),
                getattr(item, "fact_version", 0),
                str(getattr(item, "rule_package_reference", "")),
            ),
        )

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
        """Earliest date with an explicit fact for one calendar.

        Task-11 canonical IDs are ASCII-uppercase, while older deterministic
        fixtures may retain a lower-case calendar label.  Resolve both forms
        without changing the immutable dataset or guessing an exchange.
        """

        floor = self._calendar_fact_floor.get(calendar_id)
        if floor is not None:
            return floor
        try:
            from app.backtesting.calendar_axis import normalize_calendar_id

            return self._calendar_fact_floor.get(normalize_calendar_id(calendar_id))
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MemoryDataProvider:
    """Deterministic in-memory implementation of :class:`DataProvider`.

    The legacy fixture manifest declares ``CALENDARS``, ``BARS``, and
    ``COVERAGE``.  Its immutable instrument table also provides the explicit
    in-memory PIT universe used by task-15 internal-link tests.  Universe is
    intentionally handled as a separate opt-in request capability rather than
    being silently inferred for old fixed-only requests, so older fixtures
    retain their original manifest and admission behaviour.

    ``invalidate_revision()`` is a **test-only** control that advances the
    internal revision counter so outstanding chunk tokens expire; it must
    never become a runtime entry point for switching consistency modes.
    """

    def __init__(
        self,
        dataset: MemoryDataSet,
        *,
        capability_manifest_version: int = 1,
        fixtures: Sequence[InternalFixture] = (),
        internal_fixtures: Sequence[InternalFixture] = (),
        universe_provider: object | None = None,
        instrument_spec_provider: object | None = None,
        qualification_provider: object | None = None,
        spec_provider: object | None = None,
        candidate_provider: object | None = None,
        universe_query_provider: object | None = None,
        pit_source: object | None = None,
        universe_scope_resolver: object | None = None,
        scope_resolver: object | None = None,
        universe_scope_provider: object | None = None,
        coverage_qualification_provider: object | None = None,
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
        # Profile resolution is kept in the provider as a pure registry
        # lookup.  It never invokes preflight services or reads mutable
        # strategy state.
        self._profile_registry = PreflightProfileRegistry()
        # Admission reports do not currently carry a profile field in the
        # frozen DataRequest.  Keep the selected profile keyed by the
        # immutable report hash so the same provider can reproduce an
        # internal-link session re-check without silently downgrading it to
        # formal@1.  Unknown hashes intentionally fall back to formal.
        self._admission_profiles: dict[str, PreflightProfile] = {}
        supplied_fixtures = tuple(fixtures) + tuple(internal_fixtures)
        if any(not isinstance(item, InternalFixture) for item in supplied_fixtures):
            raise InvalidDataRequestError(
                "fixtures entries must be InternalFixture instances"
            )
        combined_fixtures = tuple(dataset.fixtures) + supplied_fixtures
        by_fixture_key: dict[tuple[str, object, str], InternalFixture] = {}
        for fixture in combined_fixtures:
            key = (fixture.fixture_key, fixture.fixture_version, fixture.capability)
            if key in by_fixture_key and by_fixture_key[key] != fixture:
                raise InvalidDataRequestError(
                    "duplicate internal fixture key/version/capability"
                )
            by_fixture_key[key] = fixture
        self._fixtures = tuple(
            sorted(
                by_fixture_key.values(),
                key=lambda item: (
                    item.capability,
                    item.fixture_key,
                    str(item.fixture_version),
                ),
            )
        )
        self._universe_read_count = 0
        # Dynamic candidate reads require an explicit PIT source.  The local
        # ``MemoryDataSet.instruments`` table is intentionally *not* a current
        # catalogue fallback: one current ``InstrumentSpec`` cannot answer a
        # historical effective-date/data-cutoff query.  Keep the source roles
        # separate so a single-instrument qualification port is never
        # mistaken for a universe enumerator.
        self._universe_provider = next(
            (
                value
                for value in (
                    universe_provider,
                    universe_query_provider,
                    candidate_provider,
                    pit_source,
                    qualification_provider
                    if callable(getattr(qualification_provider, "query", None))
                    else None,
                )
                if value is not None
            ),
            None,
        )
        self._pit_spec_provider = (
            instrument_spec_provider
            if instrument_spec_provider is not None
            else spec_provider
            if spec_provider is not None
            else (
                self._universe_provider
                if any(
                    callable(getattr(self._universe_provider, name, None))
                    for name in (
                        "resolve_spec",
                        "resolve_instrument",
                        "resolve_identity",
                    )
                )
                else None
            )
        )
        self._coverage_qualification_provider = (
            coverage_qualification_provider
            if coverage_qualification_provider is not None
            else (
                qualification_provider
                if qualification_provider is not None
                else self._universe_provider
                if self._universe_provider is not None
                and any(
                    callable(getattr(self._universe_provider, name, None))
                    for name in (
                        "qualify_instrument",
                        "qualify",
                        "coverage_qualification",
                        "resolve_qualification",
                    )
                )
                else None
            )
        )
        self._universe_scope_provider = (
            universe_scope_resolver
            if universe_scope_resolver is not None
            else scope_resolver
            if scope_resolver is not None
            else universe_scope_provider
            if universe_scope_provider is not None
            else self._universe_provider
            if self._universe_provider is not None
            and any(
                callable(getattr(self._universe_provider, name, None))
                for name in (
                    "resolve_dynamic_universe_scope",
                    "resolve_scope",
                    "scope_resolution",
                )
            )
            else None
        )
        self._universe_supported = self._has_universe_query_method(
            self._universe_provider
        )

        asset_classes = (
            sorted({spec.asset_class for spec in dataset.instruments}) or ["equity"]
        )
        served_base = (
            DataCapability.CALENDARS,
            DataCapability.BARS,
            DataCapability.COVERAGE,
        )
        # Preserve the historical fixture manifest for callers that only use
        # fixed bars.  A caller supplying a concrete PIT universe source opts
        # into the explicit task-15 capability declaration.
        served = (
            (*served_base, DataCapability.UNIVERSE)
            if self._universe_supported
            else served_base
        )
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
                capability: (
                    PitSupport.NON_STRICT
                    if capability is DataCapability.BARS
                    else PitSupport.STRICT
                )
                for capability in served
            },
            consistency_modes=(
                ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
                ConsistencyMode.TRANSITIONAL_REPEATABLE_READ,
            ),
            consistency_token_contracts=(CHUNK_TOKEN_CONTRACT,),
            supported_chunk_policies=(CHUNK_POLICY,),
            capabilities=served,
            capability_sources={
                **{
                    capability: CapabilitySource.FIXTURE for capability in served
                },
                # The in-memory universe is an explicit immutable fixture
                # source, not a production market catalogue.
                DataCapability.UNIVERSE: CapabilitySource.FIXTURE,
                **{
                    DataCapability.ACTIONS: CapabilitySource.FIXTURE
                    for fixture in self._fixtures
                    if fixture.capability
                    == InternalFixtureCapability.QUANTITY_ACTION_COVERAGE.value
                },
                **{
                    DataCapability.STATUS: CapabilitySource.FIXTURE
                    for fixture in self._fixtures
                    if fixture.capability
                    == InternalFixtureCapability.TRADING_STATUS.value
                },
            },
        )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def dataset(self) -> MemoryDataSet:
        """The immutable backing dataset."""

        return self._dataset

    @property
    def fixtures(self) -> tuple[InternalFixture, ...]:
        """Explicit internal substitute facts attached to this fixture provider."""

        return self._fixtures

    @property
    def read_count(self) -> int:
        """How many times the bar index has been accessed (test observability)."""

        return self._read_count

    @property
    def universe_read_count(self) -> int:
        """How many PIT universe scans the fixture has performed."""

        return self._universe_read_count

    @staticmethod
    def _has_universe_query_method(source: object | None) -> bool:
        """Return whether ``source`` explicitly enumerates PIT candidates."""

        if source is None:
            return False
        if callable(source):
            return True
        return any(
            callable(getattr(source, name, None))
            for name in (
                "query_candidates",
                "query",
                "candidates",
                "resolve_candidates",
                "resolve_universe",
            )
        )

    @staticmethod
    def _has_coverage_qualification_method(source: object | None) -> bool:
        """Return whether ``source`` exposes the typed 16A coverage port."""

        if source is None:
            return False
        return any(
            callable(getattr(source, name, None))
            for name in (
                "qualify_instrument",
                "coverage_qualification",
                "qualify",
                "resolve_qualification",
            )
        )

    def supports_universe(self) -> bool:
        """Return the fixture's explicit PIT-universe capability declaration."""

        return self._universe_supported

    def resolve_dynamic_universe_scope(
        self,
        request: DataPreflightRequest,
        *,
        profile: PreflightProfile | str | ContractRef | None = None,
    ):
        """Resolve the finite named calendar scope for dynamic/hybrid runs.

        This is an admission-only operation.  It never enumerates candidates
        through strategy code and never performs network I/O.  The richer
        ``UniverseScopeResolution`` value object is imported lazily so the
        memory fixture remains usable during staged task-package imports.
        """

        if not isinstance(request, DataPreflightRequest):
            raise InvalidDataRequestError("request must be a DataPreflightRequest")
        selected_profile = self._profile_registry.resolve(
            profile if profile is not None else FORMAL_PROFILE
        )
        internal_profile = (
            selected_profile.reference == INTERNAL_LINK_ACCEPTANCE_PROFILE
        )
        from app.backtesting.data.universe import (
            UniverseScopeIssue,
            UniverseScopeResolution,
            UniverseScopeStatus,
        )

        mode = request.instrument_scope_mode
        def canonical_ids(values: Sequence[str]) -> tuple[str, ...]:
            try:
                return tuple(sorted({normalize_calendar_id(value) for value in values}))
            except Exception as exc:
                raise UniverseScopeUnresolvedError(
                    "the dynamic scope contains an invalid calendar id"
                ) from exc

        fixed_ids = _fixed_authorized_ids(request)
        fixed_calendar_ids = {
            spec.calendar_id
            for instrument_id in fixed_ids
            if (spec := self._dataset.instrument(instrument_id)) is not None
        }
        if mode is InstrumentScopeMode.FIXED:
            calendar_ids = canonical_ids(tuple(fixed_calendar_ids))
        elif not self._universe_supported or DataCapability.UNIVERSE not in request.required_capabilities:
            issue = UniverseScopeIssue(
                code="universe_capability_missing",
                message="Provider 未提供动态候选查询和资格证明能力。",
                field="provider",
                details={
                    "capability": DataCapability.UNIVERSE.value,
                    "declared": DataCapability.UNIVERSE in request.required_capabilities,
                },
            )
            return UniverseScopeResolution(
                status=UniverseScopeStatus.BLOCKED,
                market_scope=request.market_scope,
                universe_query_policy=request.universe_query_policy,
                rule_package_reference=request.rule_package,
                rule_exception_set_reference=request.rule_exception_set,
                qualification_policy_version=request.qualification_policy_version,
                resolved_calendar_ids=canonical_ids(tuple(fixed_calendar_ids)),
                capability_summary={"universe": "missing"},
                source_evidence={"provider_key": self._dataset.provider_key},
                issues=(issue,),
                scope_mode=mode,
                data_cutoff=request.query_boundary.data_cutoff,
            )
        else:
            source = self._universe_provider
            if not self._universe_supported:
                issue = UniverseScopeIssue(
                    code="universe_capability_missing",
                    message="Provider 未提供动态候选查询能力。",
                    field="provider",
                    details={
                        "provider_type": (
                            type(source).__name__ if source is not None else None
                        )
                    },
                )
                return UniverseScopeResolution(
                    status=UniverseScopeStatus.BLOCKED,
                    market_scope=request.market_scope,
                    universe_query_policy=request.universe_query_policy,
                    rule_package_reference=request.rule_package,
                    rule_exception_set_reference=request.rule_exception_set,
                    qualification_policy_version=request.qualification_policy_version,
                    resolved_calendar_ids=canonical_ids(tuple(fixed_calendar_ids)),
                    capability_summary={"universe": "missing"},
                    source_evidence={"provider_key": self._dataset.provider_key},
                    issues=(issue,),
                    scope_mode=mode,
                    data_cutoff=request.query_boundary.data_cutoff,
                )
            if not self._has_coverage_qualification_method(
                self._coverage_qualification_provider
            ) and not self._has_coverage_qualification_method(source):
                issue = UniverseScopeIssue(
                    code="universe_capability_missing",
                    message="Provider 未提供动态候选查询能力。",
                    field="provider",
                    details={"provider_type": type(source).__name__},
                )
                return UniverseScopeResolution(
                    status=UniverseScopeStatus.BLOCKED,
                    market_scope=request.market_scope,
                    universe_query_policy=request.universe_query_policy,
                    rule_package_reference=request.rule_package,
                    rule_exception_set_reference=request.rule_exception_set,
                    qualification_policy_version=request.qualification_policy_version,
                    resolved_calendar_ids=canonical_ids(tuple(fixed_calendar_ids)),
                    capability_summary={"universe": "missing"},
                    source_evidence={"provider_key": self._dataset.provider_key},
                    issues=(issue,),
                    scope_mode=mode,
                    data_cutoff=request.query_boundary.data_cutoff,
                )
            try:
                dynamic_calendar_ids = self._dynamic_scope_calendar_ids(request)
            except UniverseScopeUnresolvedError as exc:
                issue = UniverseScopeIssue(
                    code=exc.code,
                    message="动态候选范围必须由显式 scope resolver 返回具名日历。",
                    field="resolved_calendar_ids",
                    details=dict(getattr(exc, "details", {}) or {}),
                )
                return UniverseScopeResolution(
                    status=UniverseScopeStatus.BLOCKED,
                    market_scope=request.market_scope,
                    universe_query_policy=request.universe_query_policy,
                    rule_package_reference=request.rule_package,
                    rule_exception_set_reference=request.rule_exception_set,
                    qualification_policy_version=request.qualification_policy_version,
                    resolved_calendar_ids=canonical_ids(tuple(fixed_calendar_ids)),
                    capability_summary={"universe": "available"},
                    source_evidence={"provider_key": self._dataset.provider_key},
                    issues=(issue,),
                    scope_mode=mode,
                    data_cutoff=request.query_boundary.data_cutoff,
                )
            calendar_ids = canonical_ids(
                tuple(fixed_calendar_ids | set(dynamic_calendar_ids))
            )
        if not calendar_ids:
            issue = UniverseScopeIssue(
                code="universe_scope_unresolved",
                message="动态候选范围无法解析出有限具名交易日历。",
                field="resolved_calendar_ids",
                details={"provider_key": self._dataset.provider_key},
            )
            return UniverseScopeResolution(
                status=UniverseScopeStatus.BLOCKED,
                market_scope=request.market_scope,
                universe_query_policy=request.universe_query_policy,
                rule_package_reference=request.rule_package,
                rule_exception_set_reference=request.rule_exception_set,
                qualification_policy_version=request.qualification_policy_version,
                resolved_calendar_ids=(),
                capability_summary={"universe": "available"},
                source_evidence={"provider_key": self._dataset.provider_key},
                issues=(issue,),
                scope_mode=mode,
                data_cutoff=request.query_boundary.data_cutoff,
            )
        # A named scope is not sufficient for admission: task-11's strict
        # compatibility resolver must prove the participating calendar axis
        # before this scope can be marked ready.  This call consumes the
        # explicit ids returned above and never discovers calendars from rows.
        axis = resolve_calendar_axis(
            self._dataset.calendar_axis_provider,
            policy_key=POLICY_KEY_STRICT_COMPATIBLE,
            policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
            start_date=request.requested_window.start_date,
            end_date=request.requested_window.end_date,
            calendar_ids=calendar_ids,
        )
        axis_ready = (
            axis.status is CalendarAxisStatus.COMPATIBLE
            and bool(axis.session_signature)
            and not axis.differences
        )
        axis_issues = () if axis_ready else (
            UniverseScopeIssue(
                code="universe_scope_unresolved",
                message="动态范围缺少任务包 11 strict_compatible@1 日历兼容性证明。",
                field="calendar_axis",
                details={
                    "resolved_calendar_ids": calendar_ids,
                    "differences": tuple(
                        difference.evidence()
                        for difference in axis.differences
                    ),
                },
            ),
        )
        formal_issues = () if internal_profile else (
            UniverseScopeIssue(
                code="universe_capability_missing",
                message="formal 动态候选生产能力尚未完整交付，已阻断请求。",
                field="preflight_profile",
                details={"preflight_profile": selected_profile.profile},
            ),
        )
        return UniverseScopeResolution(
            status=(
                UniverseScopeStatus.READY
                if axis_ready and internal_profile
                else UniverseScopeStatus.BLOCKED
            ),
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            rule_package_reference=request.rule_package,
            rule_exception_set_reference=request.rule_exception_set,
            qualification_policy_version=request.qualification_policy_version,
            resolved_calendar_ids=calendar_ids,
            capability_summary={
                "universe": "available",
                "pit_identity": "available",
                "identity": "available",
                "mapping": "available",
                "rules": "available",
                "market_data": "available",
                "qualification": "explicit_single_instrument_port",
            },
            source_evidence={
                "provider_key": self._dataset.provider_key,
                "fixture_revision": self._dataset.fixture_revision,
                "preflight_profile": selected_profile.profile,
            },
            scope_mode=mode,
            data_cutoff=request.query_boundary.data_cutoff,
            calendar_session_signature=(axis.session_signature if axis_ready else None),
            calendar_axis_resolution=axis,
            issues=(*axis_issues, *formal_issues),
        )

    @staticmethod
    def _invoke_universe_method(method, query: UniverseQuery):
        """Invoke a candidate source using only the arguments it declares.

        Internal-link providers intentionally have small, dependency-free
        interfaces.  Signature binding lets the fixture consume either the
        canonical ``query(query)`` shape or an equivalent keyword-oriented
        port without catching and hiding a provider's own runtime errors.
        """

        values = {
            "query": query,
            "universe_query": query,
            "effective_date": query.effective_date,
            "data_cutoff": query.boundary.data_cutoff,
            "boundary": query.boundary,
            "market_scope": query.market_scope,
            "rule": query.rule,
            "rule_package": query.rule,
            "universe_query_policy": query.universe_query_policy,
            "rule_exception_set": query.rule_exception_set,
            "qualification_policy_version": query.qualification_policy_version,
            "allowed_calendar_ids": query.allowed_calendar_ids,
            "scope_mode": query.scope_mode,
        }
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return method(query)
        parameters = signature.parameters
        if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
            return method(**values)
        kwargs = {
            name: values[name]
            for name, parameter in parameters.items()
            if name in values
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        required_positional = [
            parameter
            for parameter in parameters.values()
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            or (
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and parameter.default is inspect.Parameter.empty
                and parameter.name not in kwargs
            )
        ]
        if required_positional:
            return method(query)
        return method(**kwargs)

    def _universe_source_rows(self, query: UniverseQuery) -> tuple[object, ...]:
        """Read one immutable candidate source without network access."""

        self._universe_read_count += 1
        source = self._universe_provider
        if source is None:
            # ``MemoryDataSet.instruments`` is the fixed-spec fixture, not a
            # PIT universe catalogue.  Returning it here would make a
            # dynamic query silently use today's/current rows and would make
            # future identity facts visible at historical effective dates.
            raise UniverseCapabilityMissingError(
                "a dynamic PIT universe requires an explicitly injected source",
                details={"reason_code": "pit_universe_source_missing"},
            )
        method = None
        for name in (
            "query_candidates",
            "query",
            "candidates",
            "resolve_candidates",
            "resolve_universe",
        ):
            candidate = getattr(source, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None and callable(source):
            method = source
        if method is None:
            # A single-instrument qualification port cannot enumerate a
            # universe.  Iterating ``dataset.instruments`` here would turn a
            # current fixed-spec table into an implicit dynamic catalogue and
            # would make the result depend on the table's physical rows.
            raise UniverseCapabilityMissingError(
                "the configured PIT source does not expose a universe query",
                details={"provider_type": type(source).__name__},
            )
        if method is None:
            raise UniverseCapabilityMissingError(
                "the configured universe provider does not expose a query method",
                details={"provider_type": type(source).__name__},
            )
        try:
            result = self._invoke_universe_method(method, query)
        except (UniverseCapabilityMissingError, UniverseScopeUnresolvedError):
            raise
        except UnsupportedCapabilityError as exc:
            raise UniverseCapabilityMissingError(
                "the configured universe provider does not support PIT universe queries",
                details={
                    "provider_type": type(source).__name__,
                    "cause_code": getattr(exc, "code", "unsupported_capability"),
                },
            ) from exc
        except Exception as exc:
            raise UniverseProviderContractViolationError(
                "the configured universe provider failed while reading a PIT query",
                details={
                    "provider_type": type(source).__name__,
                    "error_type": type(exc).__name__,
                },
            ) from exc
        if result is None:
            return ()
        if isinstance(result, Mapping):
            result = tuple(result.values())
        else:
            for name in (
                "candidates",
                "eligible_candidates",
                "specs",
                "rows",
                "results",
            ):
                nested = getattr(result, name, None)
                if nested is not None and not isinstance(result, (str, bytes)):
                    result = nested
                    break
        if isinstance(result, (str, bytes)):
            raise UniverseProviderContractViolationError(
                "the configured universe provider returned text instead of candidate rows"
            )
        try:
            rows = tuple(result)
        except (UniverseCapabilityMissingError, UniverseScopeUnresolvedError):
            raise
        except Exception as exc:
            raise UniverseProviderContractViolationError(
                "the configured universe provider returned a non-iterable result"
                if isinstance(exc, TypeError)
                else "the configured universe provider failed while iterating rows",
                details={"provider_type": type(source).__name__, "error_type": type(exc).__name__},
            ) from exc
        # An explicit PIT source may enumerate stable ids while a separate
        # task-13 spec provider resolves the versioned identity/display/spec.
        # Materialize only through that injected resolver; never use the
        # legacy dataset instrument table as a historical fallback.
        if self._pit_spec_provider is not None:
            materialized: list[object] = []
            for row in rows:
                row_spec = (
                    row
                    if isinstance(row, InstrumentSpec)
                    else getattr(row, "spec", None)
                )
                if isinstance(row_spec, InstrumentSpec):
                    materialized.append(row)
                    continue
                instrument_id = (
                    row if isinstance(row, UUID) else getattr(row, "instrument_id", None)
                )
                if not isinstance(instrument_id, UUID):
                    materialized.append(row)
                    continue
                try:
                    resolved = self.resolve_pit_spec(
                        instrument_id,
                        effective_at=datetime.combine(
                            query.effective_date,
                            time.min,
                            tzinfo=UTC,
                        ),
                        data_cutoff=query.boundary.data_cutoff,
                    )
                except Exception as exc:
                    raise UniverseProviderContractViolationError(
                        "the PIT spec provider failed while resolving a universe row",
                        details={
                            "instrument_id": str(instrument_id),
                            "error_type": type(exc).__name__,
                        },
                    ) from exc
                if resolved is not None:
                    materialized.append((resolved, row))
                else:
                    materialized.append(row)
            rows = tuple(materialized)
        return rows

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

    def preflight(
        self,
        request: DataPreflightRequest,
        *,
        profile: PreflightProfile | str | ContractRef | None = None,
        fixtures: Sequence[InternalFixture] = (),
    ) -> DataPreflightReport:
        """Run admission preflight and return the frozen report.

        ``profile``/``fixtures`` are optional compatibility hooks for the
        Phase-2a internal-link acceptance path.  Existing callers retain the
        original formal preflight semantics when they omit both arguments.
        """

        if not isinstance(request, DataPreflightRequest):
            raise InvalidDataRequestError("request must be a DataPreflightRequest")
        selected_profile: PreflightProfile | None = None
        if profile is not None:
            if isinstance(profile, PreflightProfile):
                registered = self._profile_registry.resolve(profile.reference)
                if registered != profile:
                    raise InvalidDataRequestError(
                        "profile definition does not match the registered profile",
                        details={"reason_code": "internal_preflight_profile_mismatch"},
                    )
                selected_profile = registered
            else:
                selected_profile = self._profile_registry.resolve(profile)
        profile_fixtures = tuple(self._fixtures) + tuple(fixtures)
        if any(not isinstance(item, InternalFixture) for item in profile_fixtures):
            raise InvalidDataRequestError(
                "fixtures entries must be InternalFixture instances"
            )
        report = self._build_preflight_report(
            request,
            profile=selected_profile,
            fixtures=profile_fixtures,
        )
        self._admission_profiles[report.report_hash] = (
            selected_profile
            if selected_profile is not None
            else self._profile_registry.resolve(FORMAL_PROFILE)
        )
        return report

    def _profile_for_admission(self, admission_hash: str) -> PreflightProfile:
        """Return the profile used to create one report hash."""

        return self._admission_profiles.get(
            admission_hash,
            self._profile_registry.resolve(FORMAL_PROFILE),
        )

    def qualify(
        self, request: CoverageQualificationRequest
    ) -> InstrumentCoverageQualification:
        """Evaluate one typed coverage-qualification request.

        This is the protocol-object spelling used by task-15 adapters.  The
        implementation is deliberately limited to one stable instrument; it
        never enumerates a market scope or invokes a user strategy.
        """

        if not isinstance(request, CoverageQualificationRequest):
            raise InvalidDataRequestError(
                "request must be a CoverageQualificationRequest"
            )
        return self.qualify_instrument(request)

    def qualify_instrument(
        self,
        request: CoverageQualificationRequest | UUID | None = None,
        **kwargs: object,
    ) -> InstrumentCoverageQualification:
        """Return one immutable candidate qualification result.

        A ready ``CoverageQualificationRequest`` is preferred.  The keyword
        form exists for the task-15 protocol adapter and constructs exactly
        the same request object before any fixture/index access.  In either
        form this method accepts only a stable ``instrument_id`` and never a
        source code, strategy callback, or dynamic candidate collection.
        """

        if isinstance(request, CoverageQualificationRequest):
            if kwargs:
                raise InvalidDataRequestError(
                    "qualification request and keyword fields cannot be mixed"
                )
            qualification_request = request
        else:
            values = dict(kwargs)
            if request is not None:
                if not isinstance(request, UUID):
                    raise InvalidDataRequestError(
                        "instrument_id must be a UUID"
                    )
                values["instrument_id"] = request
            try:
                instrument_id = values["instrument_id"]
                effective_date = values["effective_date"]
                required_capabilities = values["required_capabilities"]
                query_boundary = values["query_boundary"]
                resolved_calendar_ids = values["resolved_calendar_ids"]
            except KeyError as exc:
                raise InvalidDataRequestError(
                    f"missing qualification field: {exc.args[0]}"
                ) from exc
            requested_window = values.get("requested_window")
            if not isinstance(requested_window, DateRange):
                raise InvalidDataRequestError(
                    "requested_window must be a DateRange"
                )
            formal_envelope = values.get("formal_envelope", requested_window)
            warmup_envelope = values.get("warmup_envelope")
            history_envelope = values.get("history_envelope") or formal_envelope
            if not isinstance(formal_envelope, DateRange):
                raise InvalidDataRequestError("formal_envelope must be a DateRange")
            if warmup_envelope is not None and not isinstance(warmup_envelope, DateRange):
                raise InvalidDataRequestError(
                    "warmup_envelope must be a DateRange or None"
                )
            if not isinstance(history_envelope, DateRange):
                raise InvalidDataRequestError(
                    "history_envelope must be a DateRange"
                )
            qualification_request = CoverageQualificationRequest(
                instrument_id=instrument_id,
                effective_date=effective_date,
                requested_window=requested_window,
                formal_envelope=formal_envelope,
                warmup_envelope=warmup_envelope,
                history_envelope=history_envelope,
                required_capabilities=required_capabilities,
                query_boundary=query_boundary,
                preflight_profile=values.get(
                    "preflight_profile", INTERNAL_LINK_ACCEPTANCE_PROFILE
                ),
                resolved_calendar_ids=resolved_calendar_ids,
                run_kind=values.get("run_kind"),
                rule_package=values.get("rule_package"),
                rule_exception_set=values.get("rule_exception_set"),
                market_scope=values.get("market_scope"),
                universe_query_policy=values.get("universe_query_policy"),
                qualification_policy_version=values.get(
                    "qualification_policy_version"
                ),
                required_fixture_capabilities=values.get(
                    "required_fixture_capabilities", ()
                ),
                fixtures=values.get("fixtures", ()),
                frequency=values.get("frequency", "1d"),
            )

        profile = self._profile_registry.resolve(
            qualification_request.preflight_profile
        )
        fixtures = self._fixtures + tuple(qualification_request.fixtures)
        profile.validate_request(qualification_request)
        self._validate_qualification_profile(profile, qualification_request, fixtures)
        instrument_id = qualification_request.instrument_id
        spec = self._dataset.instrument_at(
            instrument_id,
            datetime.combine(
                qualification_request.effective_date,
                time.min,
                tzinfo=UTC,
            ),
            qualification_request.query_boundary.data_cutoff,
        )
        if spec is None:
            return InstrumentCoverageQualification(
                instrument_id=instrument_id,
                eligible=False,
                coverage_reports=(),
                reason_codes=(ISSUE_INSTRUMENT_NOT_FOUND,),
                evidence_summary={
                    "profile": profile.profile,
                    "request": qualification_request.machine_content(),
                },
            )

        reasons: list[str] = []
        if spec.calendar_id not in qualification_request.resolved_calendar_ids:
            reasons.append("calendar_not_preflighted")
        if not self._scope_contains_spec(qualification_request.market_scope, spec):
            reasons.append("instrument_outside_scope")
        valid_from = getattr(spec, "valid_from", None)
        valid_to = getattr(spec, "valid_to", None)
        if isinstance(valid_from, datetime):
            valid_from = valid_from.date()
        if isinstance(valid_to, datetime):
            valid_to = valid_to.date()
        if (
            isinstance(valid_from, date)
            and qualification_request.effective_date < valid_from
        ) or (
            isinstance(valid_to, date)
            and qualification_request.effective_date >= valid_to
        ):
            reasons.append("instrument_not_effective")

        reports: list[DataCoverageReport] = []
        # Record every validated fixture, including one that substitutes a
        # capability not explicitly requested by this single-instrument call;
        # profile evidence must still affect the qualification hash.
        fixture_evidence: list[Mapping[str, object]] = [
            fixture.as_dict() for fixture in fixtures
        ]
        for capability in qualification_request.required_capabilities:
            fixture = self._fixture_for_capability(
                capability, qualification_request, fixtures
            )
            if capability not in self._manifest.capabilities and fixture is None:
                # This is a request/provider contract failure rather than a
                # candidate-level missing row: the provider cannot answer the
                # requested dimension for any instrument.
                raise UnsupportedCapabilityError(
                    "the memory provider cannot qualify the requested capability",
                    details={
                        "capability": capability.value,
                        "reason_code": "coverage_provider_capability_missing",
                    },
                )
            if fixture is not None:
                reports.append(
                    self._fixture_coverage_report(
                        capability, qualification_request, fixture
                    )
                )
                continue
            if capability in (
                DataCapability.BARS,
                DataCapability.COVERAGE,
                DataCapability.CALENDARS,
            ):
                reports.append(
                    self._memory_coverage_report(
                        instrument_id, capability, qualification_request
                    )
                )
            else:
                # The memory fixture has no source rows for other declared
                # dimensions.  Reaching this branch would mean a manifest
                # accidentally advertised an unsupported family.
                raise UnsupportedCapabilityError(
                    "the memory provider advertised an unimplemented capability",
                    details={"capability": capability.value},
                )

        for report in reports:
            if report.quality_status is QualityStatus.PARTIAL:
                reasons.append("coverage_incomplete")
            elif report.quality_status is QualityStatus.INVALID:
                reasons.append("coverage_invalid")
            elif report.quality_status is QualityStatus.UNAVAILABLE:
                reasons.append("coverage_unavailable")

        evidence_summary = {
            "profile": profile.profile,
            "run_kind": profile.run_kind,
            "instrument_id": str(instrument_id),
            "calendar_id": spec.calendar_id,
            "requested_window": {
                "start_date": qualification_request.requested_window.start_date,
                "end_date": qualification_request.requested_window.end_date,
            },
            "fixtures": fixture_evidence,
            "capability_sources": {
                capability.value: (
                    CapabilitySource.FIXTURE.value
                    if self._fixture_for_capability(capability, qualification_request, fixtures)
                    is not None
                    else self._manifest.capability_source(capability).value
                )
                for capability in qualification_request.required_capabilities
            },
        }
        return InstrumentCoverageQualification(
            instrument_id=instrument_id,
            eligible=not reasons and all(
                report.quality_status is QualityStatus.COMPLETE for report in reports
            ),
            coverage_reports=tuple(reports),
            reason_codes=tuple(sorted(set(reasons))),
            evidence_summary=evidence_summary,
            request=qualification_request,
        )

    def _validate_qualification_profile(
        self,
        profile: PreflightProfile,
        request: CoverageQualificationRequest,
        fixtures: Sequence[InternalFixture],
    ) -> None:
        """Enforce profile/run-kind and explicit fixture boundaries."""

        if request.preflight_profile != profile.reference:
            raise InvalidDataRequestError(
                "qualification request profile does not match the registry",
                details={"reason_code": "internal_preflight_profile_mismatch"},
            )
        # The profile owns the run-kind mapping; clients cannot select an
        # alternate run kind through a free-form qualification argument.
        if profile.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY and not profile.allow_fixture_only:
            raise InvalidDataRequestError(
                "internal_link_acceptance@1 must permit only explicit fixtures",
                details={"reason_code": "internal_preflight_profile_mismatch"},
            )
        fixtures = tuple(fixtures)
        if profile.key == FORMAL_PROFILE_KEY and fixtures:
            raise InvalidDataRequestError(
                "formal@1 rejects fixture_only facts",
                details={"reason_code": "formal_fixture_not_allowed"},
            )
        for fixture in fixtures:
            if not isinstance(fixture, InternalFixture):
                raise InvalidDataRequestError("fixtures entries must be InternalFixture")
            if not profile.accepts_fixture(fixture):
                raise InvalidDataRequestError(
                    "fixture is not registered for the selected profile",
                    details={
                        "reason_code": "internal_preflight_fixture_missing",
                        "fixture_key": fixture.fixture_key,
                        "fixture_version": fixture.fixture_version,
                        "capability": fixture.capability,
                    },
                )
            if not fixture.covers(request):
                raise InvalidDataRequestError(
                    "fixture does not cover the qualification request",
                    details={
                        "reason_code": "internal_preflight_fixture_out_of_scope",
                        "fixture_key": fixture.fixture_key,
                        "fixture_version": fixture.fixture_version,
                        "instrument_id": str(request.instrument_id),
                    },
                )
        missing_fixture_capabilities = set(request.required_fixture_capabilities) - {
            fixture.capability for fixture in fixtures
        }
        if missing_fixture_capabilities:
            raise InvalidDataRequestError(
                "a named internal fixture is required but was not supplied",
                details={
                    "reason_code": "internal_preflight_fixture_missing",
                    "capabilities": sorted(missing_fixture_capabilities),
                },
            )

    @staticmethod
    def _scope_contains_spec(scope: object, spec: InstrumentSpec) -> bool:
        """Apply only explicit scope axes; never infer a missing axis."""

        if scope is None:
            return True
        return (
            (not scope.markets or getattr(spec, "market", None) in scope.markets)
            and (not scope.exchanges or spec.exchange in scope.exchanges)
            and (not scope.asset_classes or spec.asset_class in scope.asset_classes)
            and (not scope.currencies or spec.currency in scope.currencies)
        )

    @staticmethod
    def _fixture_for_capability(
        capability: DataCapability,
        request: CoverageQualificationRequest,
        fixtures: Sequence[InternalFixture] = (),
    ) -> InternalFixture | None:
        """Find one explicitly required fixture for a generic capability."""

        aliases = {
            DataCapability.ACTIONS: InternalFixtureCapability.QUANTITY_ACTION_COVERAGE.value,
            DataCapability.STATUS: InternalFixtureCapability.TRADING_STATUS.value,
        }
        expected = aliases.get(capability)
        if expected is None:
            return None
        candidates = tuple(fixtures) if fixtures else tuple(request.fixtures)
        for fixture in candidates:
            if expected is None or fixture.capability == expected:
                return fixture
        return None

    def _qualification_session_dates(
        self, request: CoverageQualificationRequest
    ) -> tuple[date, ...]:
        """Use only explicitly resolved open calendar sessions."""

        envelope = request.history_envelope or request.formal_envelope
        allowed = set(request.resolved_calendar_ids)
        dates = {
            fact.session_date
            for fact in self._dataset.calendar_facts
            if fact.calendar_id in allowed
            and fact.is_open
            and envelope.start_date <= fact.session_date <= envelope.end_date
        }
        return tuple(sorted(dates))

    def _memory_coverage_report(
        self,
        instrument_id: UUID,
        capability: DataCapability,
        request: CoverageQualificationRequest,
    ) -> DataCoverageReport:
        """Project memory Bar/session fixtures into the existing report DTO."""

        days = self._qualification_session_dates(request)
        complete = partial = invalid = unavailable = 0
        missing: list[date] = []
        revisions: dict[str, set[str]] = {}
        for day in days:
            if capability is DataCapability.CALENDARS:
                calendar_facts = tuple(
                    fact
                    for fact in self._dataset.calendar_facts
                    if fact.session_date == day
                    and fact.calendar_id in request.resolved_calendar_ids
                    and fact.is_open
                )
                if calendar_facts:
                    complete += 1
                else:
                    unavailable += 1
                    missing.append(day)
                continue
            bar = self._dataset.bar_at(
                instrument_id, request.frequency, PriceBasis.RAW, day
            )
            if bar is None:
                unavailable += 1
                missing.append(day)
                continue
            status = bar.evidence.quality_status
            if status is QualityStatus.COMPLETE:
                complete += 1
            elif status is QualityStatus.PARTIAL:
                partial += 1
                missing.append(day)
            elif status is QualityStatus.INVALID:
                invalid += 1
                missing.append(day)
            else:
                unavailable += 1
                missing.append(day)
            revision = bar.evidence.source_revision
            if revision:
                revisions.setdefault(bar.evidence.source, set()).add(revision)
        expected = len(days)
        if invalid:
            quality = QualityStatus.INVALID
        elif partial or unavailable:
            quality = QualityStatus.PARTIAL if complete or partial else QualityStatus.UNAVAILABLE
        elif expected:
            quality = QualityStatus.COMPLETE
        else:
            quality = QualityStatus.UNAVAILABLE
        return DataCoverageReport(
            requested_window=(request.history_envelope or request.formal_envelope),
            capability=capability,
            instrument_ids=(instrument_id,),
            expected_count=expected,
            complete_count=complete,
            partial_count=partial,
            invalid_count=invalid,
            unavailable_count=unavailable,
            quality_status=quality,
            missing_ranges=_merge_missing_ranges(missing),
            source_revisions={
                source: ",".join(sorted(values))
                for source, values in sorted(revisions.items())
            },
        )

    @staticmethod
    def _fixture_coverage_report(
        capability: DataCapability,
        request: CoverageQualificationRequest,
        fixture: InternalFixture,
    ) -> DataCoverageReport:
        """Represent a named fixture as explicit complete evidence only."""

        return DataCoverageReport(
            requested_window=(request.history_envelope or request.formal_envelope),
            capability=capability,
            instrument_ids=(request.instrument_id,),
            expected_count=1,
            complete_count=1,
            partial_count=0,
            invalid_count=0,
            unavailable_count=0,
            quality_status=QualityStatus.COMPLETE,
            source_revisions={fixture.fixture_key: str(fixture.fixture_version)},
        )

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

    def _has_canonical_calendar_metadata(self) -> bool:
        """Identify a task-11 calendar dataset before touching its indexes."""

        if self._dataset.calendar_registries:
            return True
        return bool(
            any(
                definition.valid_from is not None
                or definition.registry_fact_id is not None
                or definition.known_at is not None
                for definition in self._dataset.calendar_definitions
            )
            or any(
                fact.known_at is not None
                or fact.registry_fact_id is not None
                or fact.definition_fact_id is not None
                for fact in self._dataset.calendar_facts
            )
        )

    def _collect_common_preflight_issues(
        self,
        request: DataPreflightRequest,
        *,
        profile: PreflightProfile | None = None,
        fixtures: Sequence[InternalFixture] = (),
    ) -> tuple[list[PreflightIssue], tuple[UUID, ...]]:
        """Run provider/request admission gates shared by both calendar paths."""

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
            # The transitional mode issues no logical tokens, so a configured
            # token contract can never be honored.
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_TOKEN_CONTRACT,
                    severity=IssueSeverity.ERROR,
                    scope="consistency",
                    message="过渡一致性模式不签发逻辑 token，不能配置 token 契约",
                    field="consistency_token_contract",
                )
            )
        dynamic_mode = request.instrument_scope_mode is not InstrumentScopeMode.FIXED
        universe_requested = DataCapability.UNIVERSE in request.required_capabilities
        for capability in request.required_capabilities:
            # ``UNIVERSE`` is served by the explicit immutable instrument
            # source below even though the legacy manifest predates task 15.
            # All other capability declarations remain manifest-driven.
            if capability not in self._manifest.capabilities and not (
                capability is DataCapability.UNIVERSE
                and self._universe_supported
            ) and not (
                capability
                in {
                    DataCapability.ACTIONS,
                    DataCapability.STATUS,
                }
                and self._has_coverage_qualification_method(
                    self._coverage_qualification_provider
                )
            ) and not self._fixture_satisfies_capability(
                capability, request, profile, fixtures
            ):
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
        # Dynamic/hybrid requests are admitted only when the caller explicitly
        # declares the universe capability.  This keeps the legacy fixed-only
        # fixture contract deterministic while ensuring a missing Provider
        # capability blocks the complete request rather than filtering every
        # candidate away.
        if dynamic_mode and (not universe_requested or not self._universe_supported):
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_CAPABILITY,
                    severity=IssueSeverity.ERROR,
                    scope="instrument_scope",
                    message=(
                        "动态候选范围要求 Provider 显式提供 PIT Universe 能力，"
                        "当前请求未满足能力门禁"
                    ),
                    field="instrument_scope_mode",
                    details={
                        "scope_mode": request.instrument_scope_mode.value,
                        "capability": DataCapability.UNIVERSE.value,
                        "capability_declared": universe_requested,
                    },
                )
            )
            # Keep the legacy ``unsupported_capability`` issue for existing
            # consumers, while publishing the task-15 stable request-level
            # code that distinguishes a missing universe provider from a
            # single filtered candidate.
            issues.append(
                PreflightIssue(
                    code="universe_capability_missing",
                    severity=IssueSeverity.ERROR,
                    scope="instrument_scope",
                    message="动态候选 Provider 能力缺失，已阻断请求。",
                    field="required_capabilities",
                    details={
                        "cause_code": "universe_capability_missing",
                        "capability": DataCapability.UNIVERSE.value,
                    },
                )
            )
        if request.frequency not in self._manifest.supported_frequencies:
            issues.append(
                PreflightIssue(
                    code=ISSUE_UNSUPPORTED_FREQUENCY,
                    severity=IssueSeverity.ERROR,
                    scope="frequency",
                    message=f"请求频率 {request.frequency} 不受内存 Provider 支持",
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

        non_strict = self._non_strict_capabilities(request)
        if request.query_boundary.knowledge_as_of is not None and non_strict:
            issues.append(
                PreflightIssue(
                    code="strict_pit_unavailable",
                    severity=IssueSeverity.ERROR,
                    scope="pit",
                    message="请求要求严格历史认知，但内存 Provider 的事实缺少可验证认知时点，已阻断回测。",
                    field="non_strict_pit_capabilities",
                    details={
                        "knowledge_as_of": request.query_boundary.knowledge_as_of.isoformat(),
                        "capabilities": tuple(item.value for item in non_strict),
                    },
                )
            )
        scope_ids = tuple(sorted(_fixed_authorized_ids(request), key=str))
        known_ids = {spec.instrument_id for spec in self._dataset.instruments}
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
        return issues, scope_ids

    @staticmethod
    def _fixture_scope_covers_request(
        fixture: InternalFixture, request: DataPreflightRequest
    ) -> bool:
        """Check fixture bounds against fixed request subjects and dates."""

        fixed_ids = _fixed_authorized_ids(request)
        if fixed_ids and fixture.instrument_ids:
            if not set(fixed_ids).issubset(set(fixture.instrument_ids)):
                return False
        elif fixed_ids and fixture.scope:
            scoped_ids = fixture.scope.get("instrument_ids", ())
            if scoped_ids and not set(str(item) for item in fixed_ids).issubset(
                set(str(item) for item in scoped_ids)
            ):
                return False
        return (
            fixture.start_date <= request.requested_window.start_date
            and fixture.end_date >= request.requested_window.end_date
        )

    @staticmethod
    def _fixture_capability_for_data_capability(
        capability: DataCapability,
    ) -> str | None:
        """Map only profile-approved substitute dimensions."""

        return {
            DataCapability.ACTIONS: InternalFixtureCapability.QUANTITY_ACTION_COVERAGE.value,
            DataCapability.STATUS: InternalFixtureCapability.TRADING_STATUS.value,
        }.get(capability)

    def _fixture_satisfies_capability(
        self,
        capability: DataCapability,
        request: DataPreflightRequest,
        profile: PreflightProfile | None,
        fixtures: Sequence[InternalFixture],
    ) -> bool:
        """Whether a valid internal fixture substitutes one missing family."""

        if profile is None or not profile.allow_fixture_only:
            return False
        expected = self._fixture_capability_for_data_capability(capability)
        if expected is None:
            return False
        return any(
            fixture.capability == expected
            and profile.accepts_fixture(fixture)
            and self._fixture_scope_covers_request(fixture, request)
            for fixture in fixtures
        )

    def _profile_issues(
        self,
        profile: PreflightProfile,
        request: DataPreflightRequest,
        fixtures: Sequence[InternalFixture],
    ) -> list[PreflightIssue]:
        """Project profile/fixture contract failures into report issues."""

        issues: list[PreflightIssue] = []
        if profile.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY:
            if profile.reference != INTERNAL_LINK_ACCEPTANCE_PROFILE:
                issues.append(
                    PreflightIssue(
                        code="internal_preflight_profile_mismatch",
                        severity=IssueSeverity.ERROR,
                        scope="profile",
                        message="内部链路验收仅允许精确的 internal_link_acceptance@1 profile。",
                        field="preflight_profile",
                        details={
                            "expected": f"{INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY}@1",
                            "actual": profile.profile,
                        },
                    )
                )
            if profile.run_kind != INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY:
                issues.append(
                    PreflightIssue(
                        code="internal_preflight_profile_mismatch",
                        severity=IssueSeverity.ERROR,
                        scope="profile",
                        message="内部链路验收 profile 的 run kind 不匹配，已阻断预检。",
                        field="run_kind",
                        details={"expected": INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY, "actual": profile.run_kind},
                    )
                )
            if profile.allow_degraded:
                issues.append(
                    PreflightIssue(
                        code="internal_preflight_degraded_forbidden",
                        severity=IssueSeverity.ERROR,
                        scope="profile",
                        message="内部链路验收不允许 degraded 状态，已阻断预检。",
                        field="allow_degraded",
                    )
                )
        elif profile.key == FORMAL_PROFILE_KEY and fixtures:
            issues.append(
                PreflightIssue(
                    code="internal_preflight_profile_mismatch",
                    severity=IssueSeverity.ERROR,
                    scope="profile",
                    message="formal@1 不接受 fixture_only 内部事实，已阻断预检。",
                    field="fixtures",
                    details={"reason_code": "formal_fixture_not_allowed"},
                )
            )
        for fixture in fixtures:
            if not profile.accepts_fixture(fixture):
                issues.append(
                    PreflightIssue(
                        code="internal_preflight_fixture_missing",
                        severity=IssueSeverity.ERROR,
                        scope="fixtures",
                        message="内部替代事实未获当前 profile 具名许可，已阻断预检。",
                        field="fixture_key",
                        details={
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                            "capability": fixture.capability,
                        },
                    )
                )
            elif not self._fixture_scope_covers_request(fixture, request):
                issues.append(
                    PreflightIssue(
                        code="internal_preflight_fixture_out_of_scope",
                        severity=IssueSeverity.ERROR,
                        scope="fixtures",
                        message="内部替代事实的标的或日期范围未完整覆盖请求，已阻断预检。",
                        field="scope",
                        details={
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                            "scope": fixture.scope,
                            "requested": {
                                "instrument_ids": [
                                    str(item) for item in _fixed_authorized_ids(request)
                                ],
                                "start_date": request.requested_window.start_date.isoformat(),
                                "end_date": request.requested_window.end_date.isoformat(),
                            },
                        },
                    )
                )
            else:
                # A valid fixture is audit evidence, not a blocker.  Keeping
                # its exact key/version/scope in the existing issue payload
                # makes the report hash sensitive to fixture changes without
                # creating a second report or coverage table.
                issues.append(
                    PreflightIssue(
                        code="internal_fixture_used",
                        severity=IssueSeverity.WARNING,
                        scope="fixtures",
                        message="内部链路验收使用了已具名、范围完整的内部替代事实。",
                        field="fixture_key",
                        details=json.loads(canonical_json(fixture.machine_content())),
                    )
                )

        # A missing approved fixture is a distinct profile failure.  It is
        # intentionally emitted only for substitute dimensions that are
        # explicitly requested; an empty actions table is never interpreted as
        # a negative proof.
        if profile.allow_fixture_only:
            for capability in request.required_capabilities:
                expected = self._fixture_capability_for_data_capability(capability)
                if expected is None:
                    continue
                if not self._fixture_satisfies_capability(
                    capability, request, profile, fixtures
                ):
                    issues.append(
                        PreflightIssue(
                            code="internal_preflight_fixture_missing",
                            severity=IssueSeverity.ERROR,
                            scope="fixtures",
                            message="请求所需的具名内部替代事实缺失或范围不足，已阻断预检。",
                            field="fixtures",
                            details={
                                "capability": expected,
                                "requested_window": {
                                    "start_date": request.requested_window.start_date.isoformat(),
                                    "end_date": request.requested_window.end_date.isoformat(),
                                },
                            },
                        )
                    )
        return issues

    def _dynamic_scope_calendar_ids(
        self, request: DataPreflightRequest
    ) -> tuple[str, ...]:
        """Read only the explicit dynamic-scope resolver result.

        Calendar ids are an admission fact, not something a memory adapter
        may discover by scanning candidate rows or all calendar definitions.
        A source without a resolver therefore fails closed at request level.
        """

        resolver = self._universe_scope_provider
        if resolver is None:
            raise UniverseScopeUnresolvedError(
                "the dynamic universe requires an explicit scope resolver",
                details={
                    "provider_type": (
                        type(self._universe_provider).__name__
                        if self._universe_provider is not None
                        else None
                    )
                },
            )
        method = resolver if callable(resolver) else None
        if method is None:
            for name in (
                "resolve_dynamic_universe_scope",
                "resolve_scope",
                "scope_resolution",
            ):
                candidate = getattr(resolver, name, None)
                if callable(candidate):
                    method = candidate
                    break
        if method is None:
            raise UniverseScopeUnresolvedError(
                "the dynamic universe scope provider has no resolver method",
                details={"provider_type": type(resolver).__name__},
            )
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        try:
            if any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ) or "request" in parameters:
                resolution = method(request=request)
            else:
                resolution = method(request)
        except (UniverseScopeUnresolvedError, UniverseCapabilityMissingError):
            raise
        except Exception as exc:
            raise UniverseScopeUnresolvedError(
                "the dynamic universe scope resolver failed",
                details={"error_type": type(exc).__name__},
            ) from exc
        if resolution is None:
            raise UniverseScopeUnresolvedError(
                "the dynamic universe scope resolver returned no resolution"
            )
        status = (
            resolution.get("status")
            if isinstance(resolution, Mapping)
            else getattr(resolution, "status", None)
        )
        status_value = getattr(status, "value", status)
        if status_value not in (None, "ready"):
            issue_code = (
                resolution.get("primary_issue_code")
                if isinstance(resolution, Mapping)
                else getattr(resolution, "primary_issue_code", None)
            )
            if issue_code == "universe_capability_missing":
                raise UniverseCapabilityMissingError(
                    "the dynamic universe scope provider reported missing capability"
                )
            raise UniverseScopeUnresolvedError(
                "the dynamic universe scope provider reported a blocked resolution",
                details={"issue_code": issue_code},
            )
        resolved = (
            resolution.get("resolved_calendar_ids")
            if isinstance(resolution, Mapping)
            else getattr(resolution, "resolved_calendar_ids", None)
        )
        if resolved is None:
            raise UniverseScopeUnresolvedError(
                "the dynamic universe scope did not provide named calendars"
            )
        try:
            normalized = tuple(
                sorted({normalize_calendar_id(item) for item in resolved})
            )
        except Exception as exc:
            raise UniverseScopeUnresolvedError(
                "the dynamic universe scope returned invalid calendar ids"
            ) from exc
        if not normalized:
            raise UniverseScopeUnresolvedError(
                "the dynamic universe scope returned an empty calendar set"
            )
        return normalized

    @staticmethod
    def _universe_report_summary(
        request: DataPreflightRequest,
        *,
        calendar_ids: Sequence[str],
        resolution: object | None,
        filtered_reason_counts: Mapping[str, int] | None = None,
    ) -> Mapping[str, object]:
        """Build the JSON-safe report projection for candidate scope facts."""

        def reference(value: object) -> object:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            return {"key": value.key, "version": value.version}

        scope = request.market_scope
        return {
            "scope_mode": request.instrument_scope_mode.value,
            "market_scope": {
                "markets": scope.markets,
                "exchanges": scope.exchanges,
                "asset_classes": scope.asset_classes,
                "currencies": scope.currencies,
            },
            "universe_query_policy": [
                reference(item)
                for item in request.universe_query_policy.candidate_set_rules
            ],
            "qualification_policy": reference(
                getattr(request, "qualification_policy_version", None)
            ),
            "resolved_calendar_ids": tuple(calendar_ids),
            "provider_capability_status": (
                dict(getattr(resolution, "capability_summary", {}) or {})
                if resolution is not None
                else {}
            ),
            "filtered_reason_counts": dict(filtered_reason_counts or {}),
            "scope_snapshot_hash": (
                getattr(resolution, "snapshot_hash", None)
                if resolution is not None
                else getattr(request, "universe_scope_snapshot_hash", None)
            ),
        }

    def _build_preflight_report(
        self,
        request: DataPreflightRequest,
        *,
        frozen_calendar_ids: tuple[str, ...] | None = None,
        profile: PreflightProfile | None = None,
        fixtures: Sequence[InternalFixture] = (),
    ) -> DataPreflightReport:
        issues, scope_ids = self._collect_common_preflight_issues(
            request, profile=profile, fixtures=fixtures
        )
        if profile is not None:
            issues.extend(self._profile_issues(profile, request, fixtures))
        universe_scope_resolution = None
        if request.instrument_scope_mode is not InstrumentScopeMode.FIXED:
            try:
                universe_scope_resolution = self.resolve_dynamic_universe_scope(
                    request,
                    profile=profile,
                )
                if (
                    frozen_calendar_ids is not None
                    and tuple(sorted(set(frozen_calendar_ids)))
                    != tuple(universe_scope_resolution.resolved_calendar_ids)
                ):
                    issues.append(
                        PreflightIssue(
                            code="universe_preflight_hash_mismatch",
                            severity=IssueSeverity.ERROR,
                            scope="instrument_scope",
                            message="动态候选范围日历集合与冻结快照不一致，已阻断请求。",
                            field="resolved_calendar_ids",
                            details={
                                "expected": tuple(sorted(set(frozen_calendar_ids))),
                                "actual": universe_scope_resolution.resolved_calendar_ids,
                            },
                        )
                    )
                for scope_issue_item in universe_scope_resolution.issues:
                    issues.append(
                        PreflightIssue(
                            code=scope_issue_item.code,
                            severity=IssueSeverity.ERROR,
                            scope="instrument_scope",
                            message=scope_issue_item.message,
                            field=scope_issue_item.field,
                            details=dict(scope_issue_item.details),
                        )
                    )
            except (UniverseScopeUnresolvedError, UniverseCapabilityMissingError) as exc:
                issues.append(
                    PreflightIssue(
                        code=exc.code,
                        severity=IssueSeverity.ERROR,
                        scope="instrument_scope",
                        message="动态候选范围预检无法完成，已阻断请求。",
                        field="provider",
                        details=dict(getattr(exc, "details", {}) or {}),
                    )
                )

        # Task-11's canonical PIT path is opt-in for datasets that carry the
        # versioned calendar metadata (registry/definition/fact provenance).
        # Legacy task-02 fixtures still carry an explicit QueryBoundary after
        # the request-contract migration, but intentionally have no such
        # metadata and therefore continue through the compatibility resolver.
        # This keeps the migration boundary one-way without weakening the
        # strict provider path for canonical datasets.
        if self._has_canonical_calendar_metadata():
            if request.query_boundary is None:
                return self._build_cutoff_required_report(request)
            return self._build_calendar_preflight_report(
                request,
                frozen_calendar_ids=frozen_calendar_ids,
                initial_issues=issues,
                scope_ids=scope_ids,
                universe_scope_resolution=universe_scope_resolution,
            )
        # A dataset that carries canonical task-11 metadata is a strict
        # provider.  It must not silently fall back to the legacy UTC-date
        # path when the single authority (query_boundary.data_cutoff) is
        # absent.  The metadata check intentionally happens before any
        # instrument/calendar lookup so a missing cutoff cannot trigger a
        # hidden memory read or be inferred from ``knowledge_as_of``.
        # ``_collect_common_preflight_issues`` is shared with the canonical
        # snapshot path above; the legacy resolver continues below with the
        # same request/provider admission gates.

        # The authoritative calendar set is the one frozen into an admitted
        # ``DataRequest``; a raw intent (provider-level preflight) has its
        # axis derived strictly from the in-scope instruments' named
        # calendars.  Callers never pick calendars freely.
        if frozen_calendar_ids is not None:
            calendar_ids = tuple(sorted(set(frozen_calendar_ids)))
        else:
            fixed_calendar_ids = {
                spec.calendar_id
                for instrument_id in scope_ids
                if (spec := self._dataset.instrument(instrument_id)) is not None
            }
            if request.instrument_scope_mode is InstrumentScopeMode.FIXED:
                calendar_ids = tuple(sorted(fixed_calendar_ids))
            elif DataCapability.UNIVERSE in request.required_capabilities and self._universe_supported:
                # Dynamic calendars come only from the already-resolved,
                # explicit scope provider.  Do not invoke a second resolver
                # here (and never inspect candidate rows to discover ids).
                dynamic_ids = (
                    universe_scope_resolution.resolved_calendar_ids
                    if universe_scope_resolution is not None
                    and getattr(universe_scope_resolution, "status", None)
                    is not None
                    and getattr(
                        getattr(universe_scope_resolution, "status", None),
                        "value",
                        getattr(universe_scope_resolution, "status", None),
                    )
                    == "ready"
                    else ()
                )
                calendar_ids = tuple(sorted(fixed_calendar_ids | set(dynamic_ids)))
                if not calendar_ids:
                    issues.append(
                        PreflightIssue(
                            code="universe_scope_unresolved",
                            severity=IssueSeverity.ERROR,
                            scope="instrument_scope",
                            message="动态候选范围无法解析出有限具名交易日历，已阻断回测。",
                            field="resolved_calendar_ids",
                            details={"cause_code": "universe_scope_unresolved"},
                        )
                    )
            else:
                calendar_ids = tuple(sorted(fixed_calendar_ids))
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

        if (
            universe_scope_resolution is not None
            and axis is not None
            and axis.status is CalendarAxisStatus.COMPATIBLE
        ):
            # Scope discovery and strict calendar resolution are separate
            # reads, but a ready dynamic report must freeze both as one task-15
            # admission object.  Rebuilding the immutable value also refreshes
            # its snapshot hash with the authoritative session signature.
            universe_scope_resolution = replace(
                universe_scope_resolution,
                calendar_session_signature=axis.session_signature,
                calendar_axis_resolution=axis,
                snapshot_hash="",
            )
        expected_scope_hash = getattr(request, "universe_scope_snapshot_hash", None)
        if (
            expected_scope_hash is not None
            and universe_scope_resolution is not None
            and universe_scope_resolution.snapshot_hash != expected_scope_hash
        ):
            issues.append(
                PreflightIssue(
                    code="universe_preflight_hash_mismatch",
                    severity=IssueSeverity.ERROR,
                    scope="instrument_scope",
                    message="动态候选范围快照已变化，已阻断请求。",
                    field="universe_scope_snapshot_hash",
                    details={
                        "expected": expected_scope_hash,
                        "actual": universe_scope_resolution.snapshot_hash,
                    },
                )
            )

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
            knowledge_as_of=request.query_boundary.knowledge_as_of,
            non_strict_pit_capabilities=self._non_strict_capabilities(request),
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
            non_zero_initial_position_instrument_ids=getattr(
                request, "non_zero_initial_position_instrument_ids", ()
            ),
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
            query_boundary=request.query_boundary,
            qualification_policy_version=getattr(
                request, "qualification_policy_version", None
            ),
            universe_eligibility_policy_version=getattr(
                request, "qualification_policy_version", None
            ),
            universe_scope_snapshot_hash=(
                universe_scope_resolution.snapshot_hash
                if universe_scope_resolution is not None
                else getattr(request, "universe_scope_snapshot_hash", None)
            ),
            universe_eligibility_summary=(
                self._universe_report_summary(
                    request,
                    calendar_ids=(
                        universe_scope_resolution.resolved_calendar_ids
                        if universe_scope_resolution is not None
                        else calendar_ids
                    ),
                    resolution=universe_scope_resolution,
                )
                if universe_scope_resolution is not None
                else None
            ),
            universe_scope_resolution=universe_scope_resolution,
        )

    def _build_cutoff_required_report(
        self, request: DataPreflightRequest
    ) -> DataPreflightReport:
        """Return a pre-read @2 block for a missing strict data cutoff."""

        issue = PreflightIssue(
            code="DATA_CUTOFF_REQUIRED",
            severity=IssueSeverity.ERROR,
            scope=SCOPE_FORMAL,
            message="严格日历预检必须显式提供 data_cutoff，系统不会使用墙上时钟推断。",
            field="query_boundary.data_cutoff",
            details={"cause_code": "data_cutoff_required"},
        )
        return DataPreflightReport(
            status=PreflightStatus.BLOCKED,
            generated_at=self._dataset.clock,
            provider_key=request.provider_key,
            capability_manifest_version=self._manifest_version,
            requested_window=request.requested_window,
            scope_mode=request.instrument_scope_mode,
            resolved_calendar_ids=(),
            resolved_calendar_definitions=(),
            resolved_timezone=None,
            calendar_axis_policy=request.calendar_axis_policy,
            calendar_compatibility_status=CalendarAxisStatus.INCOMPATIBLE,
            calendar_session_signature="",
            resolved_sessions=(),
            warmup_sessions=(),
            max_lookback_sessions=request.max_lookback_sessions,
            knowledge_as_of=request.query_boundary.knowledge_as_of,
            non_strict_pit_capabilities=self._non_strict_capabilities(request),
            consistency_mode=request.consistency_mode,
            consistency_token_capability=False,
            consistency_token_contract=None,
            data_chunk_policy=request.data_chunk_policy,
            data_chunk_size_sessions=request.data_chunk_size_sessions,
            required_capabilities=request.required_capabilities,
            rule_package=request.rule_package,
            rule_exception_set=request.rule_exception_set,
            static_instrument_ids=request.static_instrument_ids,
            mandatory_instrument_ids=request.mandatory_instrument_ids,
            non_zero_initial_position_instrument_ids=getattr(
                request, "non_zero_initial_position_instrument_ids", ()
            ),
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
            issues=(issue,),
            query_boundary=request.query_boundary,
            hash_schema_version=1,
        )

    def _build_calendar_preflight_report(
        self,
        request: DataPreflightRequest,
        *,
        frozen_calendar_ids: tuple[str, ...] | None = None,
        initial_issues: Sequence[PreflightIssue] = (),
        scope_ids: Sequence[UUID] | None = None,
        universe_scope_resolution: object | None = None,
    ) -> DataPreflightReport:
        """Build the task-11 @2 report from one immutable snapshot attempt."""

        scope_ids = tuple(
            dict.fromkeys(
                scope_ids
                if scope_ids is not None
                else (
                    *request.static_instrument_ids,
                    *request.mandatory_instrument_ids,
                )
            )
        )
        if frozen_calendar_ids is not None:
            calendar_ids = tuple(sorted(set(frozen_calendar_ids)))
        else:
            fixed_calendar_ids = {
                spec.calendar_id
                for instrument_id in scope_ids
                if (spec := self._dataset.instrument(instrument_id)) is not None
            }
            if request.instrument_scope_mode is InstrumentScopeMode.FIXED:
                calendar_ids = tuple(sorted(fixed_calendar_ids))
            elif DataCapability.UNIVERSE in request.required_capabilities and self._universe_supported:
                # Reuse the explicit scope result computed by the outer
                # admission path.  A blocked/missing result contributes no
                # dynamic calendars and must not trigger a catalogue scan.
                dynamic_ids = (
                    universe_scope_resolution.resolved_calendar_ids
                    if universe_scope_resolution is not None
                    and getattr(
                        getattr(universe_scope_resolution, "status", None),
                        "value",
                        getattr(universe_scope_resolution, "status", None),
                    )
                    == "ready"
                    else ()
                )
                calendar_ids = tuple(sorted(fixed_calendar_ids | set(dynamic_ids)))
                if not calendar_ids:
                    issue = PreflightIssue(
                        code="universe_scope_unresolved",
                        severity=IssueSeverity.ERROR,
                        scope="instrument_scope",
                        message="动态候选范围无法解析出有限具名交易日历，已阻断回测。",
                        field="resolved_calendar_ids",
                        details={"cause_code": "universe_scope_unresolved"},
                    )
                    initial_issues = (*initial_issues, issue)
            else:
                calendar_ids = tuple(sorted(fixed_calendar_ids))
        issue_list: list[PreflightIssue] = list(initial_issues)
        snapshot: CalendarSnapshot | None = None
        axis = None
        # Request/provider admission errors are proven from local metadata and
        # must short-circuit the strict snapshot read.  This keeps the
        # prepare+batch budget intact and prevents an invalid request from
        # being accepted by the calendar-only branch.
        if not any(issue.severity is IssueSeverity.ERROR for issue in issue_list):
            try:
                snapshot_request = CalendarSnapshotRequest(
                    calendar_ids=calendar_ids,
                    formal_start=request.requested_window.start_date,
                    formal_end=request.requested_window.end_date,
                    warmup_sessions=request.warmup_sessions,
                    query_boundary=request.query_boundary,
                    instrument_ids=scope_ids,
                    provider_key=request.provider_key,
                    package_key=request.rule_package.key,
                    package_version=request.rule_package.version,
                )
                snapshot = self._dataset.calendar_axis_provider.open_calendar_snapshot(snapshot_request)
                axis = snapshot.resolution
            except CalendarPreflightResourceLimitExceededError:
                # Resource overruns are creation-gate failures.  Let the
                # coordinator project this stable error through
                # resource_limited_preflight_failure(); never turn it into an
                # ordinary blocked report that can be persisted or paged.
                raise
            except CalendarContractError as exc:
                issue_list.append(
                    PreflightIssue(
                        code=_calendar_issue_code(
                            getattr(exc, "code", "invalid_data_request"),
                            getattr(exc, "details", None),
                        ),
                        severity=IssueSeverity.ERROR,
                        scope=SCOPE_FORMAL,
                        message=f"交易日历快照无法打开：{getattr(exc, 'code', 'invalid_data_request')}。",
                        field="calendar_snapshot",
                        details={"cause_code": getattr(exc, "code", "invalid_data_request"), **dict(getattr(exc, "details", {}) or {})},
                    )
                )

        warmup_resolution: WarmupResolution | None = None
        capability_evidence: tuple[Mapping[str, object], ...] = ()
        if snapshot is not None and axis is not None:
            capability_issues, capability_evidence = evaluate_calendar_capability_gate(
                self._dataset.calendar_axis_provider,
                request,
                snapshot,
            )
            issue_list.extend(capability_issues)
            if axis.status is CalendarAxisStatus.INCOMPATIBLE:
                for difference in axis.differences:
                    issue_list.append(
                        PreflightIssue(
                            code={
                                "calendar_timezone_inconsistent": "CALENDAR_TIMEZONE_INCONSISTENT",
                                "calendar_timezone_mismatch": "CALENDAR_TIMEZONE_MISMATCH",
                                "calendar_timezone_unsupported": "CALENDAR_TIMEZONE_UNSUPPORTED",
                            }.get(
                                difference.error_code,
                                "CALENDAR_SESSION_INCOMPATIBLE",
                            ),
                            severity=IssueSeverity.ERROR,
                            scope=SCOPE_FORMAL,
                            message=f"{difference.date.isoformat()} 的参与日历会话语义不兼容，已阻断回测。",
                            field=difference.field.value,
                            details=difference.evidence(),
                        )
                    )
            elif not axis.resolved_sessions:
                issue_list.append(
                    PreflightIssue(
                        code=NO_FORMAL_SESSIONS,
                        severity=IssueSeverity.ERROR,
                        scope=SCOPE_FORMAL,
                        message="正式区间没有共同开市交易会话，无法启动回测。",
                        field="resolved_sessions",
                    )
                )
            elif request.warmup_sessions > 0:
                points = tuple(snapshot.warmup_sessions)
                if len(points) != request.warmup_sessions:
                    issue_list.append(
                        PreflightIssue(
                            code="WARMUP_COVERAGE_INSUFFICIENT",
                            severity=IssueSeverity.ERROR,
                            scope=SCOPE_WARMUP,
                            message=f"warmup 覆盖不足：请求 {request.warmup_sessions} 个会话，实际仅证明 {len(points)} 个。",
                            field="warmup_sessions",
                        )
                    )
                else:
                    anchor = axis.resolved_sessions[0].session_date
                    warmup_resolution = WarmupResolution(
                        requested_sessions=request.warmup_sessions,
                        first_formal_session=anchor,
                        status=WarmupStatus.READY,
                        coverage_status=WarmupCoverageStatus.PROVEN,
                        resolved_sessions=points,
                        # Preserve the immutable snapshot's complete natural-day
                        # envelope, including closed/gap days between the
                        # earliest warmup session and the formal anchor.
                        history_window=DateRange(
                            snapshot.envelope_start,
                            anchor - timedelta(days=1),
                        ),
                    )
            issue_list.extend(self._mandatory_coverage_issues(request, axis.resolved_sessions))

        # Warnings such as ``internal_fixture_used`` are audit evidence and
        # must not block the internal profile; only hard errors close the
        # request.  Existing formal gates all use error severity.
        blocked = any(issue.severity is IssueSeverity.ERROR for issue in issue_list)
        if blocked:
            # A blocked report may retain the warmup issue evidence in the
            # outer issue list, but never mounts a ready warmup resolution.
            warmup_resolution = None
        formal = () if blocked or axis is None or axis.status is CalendarAxisStatus.INCOMPATIBLE else axis.resolved_sessions
        warmup = () if blocked or warmup_resolution is None else warmup_resolution.resolved_sessions
        pit_context = snapshot.pit_context if snapshot is not None else (
            CalendarPITContext.from_query_boundary(request.query_boundary, "Asia/Shanghai")
            if request.query_boundary is not None else None
        )
        calendar_session_signature = "" if axis is None or axis.status is CalendarAxisStatus.INCOMPATIBLE else axis.session_signature
        if (
            universe_scope_resolution is not None
            and axis is not None
            and axis.status is CalendarAxisStatus.COMPATIBLE
        ):
            # Keep the canonical snapshot path aligned with the legacy memory
            # path: the report consumes one scope object carrying the exact
            # strict-axis result that produced its calendar signature.
            universe_scope_resolution = replace(
                universe_scope_resolution,
                calendar_session_signature=axis.session_signature,
                calendar_axis_resolution=axis,
                snapshot_hash="",
            )
        expected_scope_hash = getattr(request, "universe_scope_snapshot_hash", None)
        if (
            expected_scope_hash is not None
            and universe_scope_resolution is not None
            and universe_scope_resolution.snapshot_hash != expected_scope_hash
        ):
            issue_list.append(
                PreflightIssue(
                    code="universe_preflight_hash_mismatch",
                    severity=IssueSeverity.ERROR,
                    scope="instrument_scope",
                    message="动态候选范围快照已变化，已阻断请求。",
                    field="universe_scope_snapshot_hash",
                    details={
                        "expected": expected_scope_hash,
                        "actual": universe_scope_resolution.snapshot_hash,
                    },
                )
            )
            blocked = True
            formal = ()
            warmup = ()
            warmup_resolution = None
        revision_digest = axis.calendar_revision_digest if axis is not None else None
        snapshot_fingerprint = snapshot.snapshot_fingerprint if snapshot is not None else None
        semantic_signature = axis.calendar_semantic_signature if axis is not None else None
        warmup_signature = axis.warmup_session_signature if axis is not None else None
        usage = calendar_snapshot_usage(snapshot) if snapshot is not None else ()
        calendar_summary = {
            "policy": {"key": request.calendar_axis_policy.key, "version": request.calendar_axis_policy.version},
            "calendar_ids": calendar_ids,
            "requested_window": {
                "start_date": request.requested_window.start_date,
                "end_date": request.requested_window.end_date,
            },
            "pit_context": dict(pit_context.as_dict) if pit_context is not None else None,
            "data_cutoff": pit_context.as_dict["data_cutoff"] if pit_context is not None else None,
            "cutoff_local_date": pit_context.as_dict["cutoff_local_date"] if pit_context is not None else None,
            "include_cutoff_day": pit_context.as_dict["include_cutoff_day"] if pit_context is not None else None,
            "pit_profile": pit_context.as_dict["pit_profile"] if pit_context is not None else None,
            "profile_version": pit_context.as_dict["profile_version"] if pit_context is not None else None,
            "knowledge_as_of": pit_context.as_dict["knowledge_as_of"] if pit_context is not None else None,
            "non_strict_pit": axis.non_strict_pit if axis is not None else False,
            "non_strict_pit_capabilities": axis.non_strict_pit_capabilities if axis is not None else (),
            "compatibility_status": axis.status.value if axis is not None else "incompatible",
            "timezone": axis.timezone if axis is not None else None,
            "calendar_revision_digest": revision_digest,
            "revision_digest": revision_digest,
            "calendar_session_signature": calendar_session_signature,
            "warmup_session_signature": warmup_signature,
            "snapshot_fingerprint": snapshot_fingerprint,
            "resolved_calendar_bindings": dict(snapshot.resolved_calendar_bindings) if snapshot is not None else {},
            "resolved_calendar_definitions": [
                {
                    "calendar_id": item.calendar_id,
                    "registry_fact_id": item.registry_fact_id,
                    "registry_version": item.registry_version,
                    "definition_version": item.definition_version,
                    "definition_fact_id": item.fact_id,
                    "fact_version": item.fact_version,
                    "source": item.source,
                    "source_revision": item.source_revision,
                }
                for item in (snapshot.resolved_calendar_definitions if snapshot is not None else ())
            ],
            "differences": [difference.evidence() for difference in (axis.differences if axis is not None else ())],
            "envelope": {
                "start_date": snapshot.envelope_start if snapshot is not None else None,
                "end_date_exclusive": snapshot.envelope_end_exclusive if snapshot is not None else None,
            },
            "definition_usage_by_date": usage,
            "coverage": dict(snapshot.coverage) if snapshot is not None else None,
            "capabilities": capability_evidence,
        }
        calendar_summary = json.loads(canonical_json(calendar_summary))
        session_summary = {
            "pit_context": dict(pit_context.as_dict) if pit_context is not None else None,
            "formal_session_count": len(formal),
            "warmup_session_count": len(warmup),
            "formal_sessions": [
                {
                    "date": point.session_date,
                    "session_id": point.session_id,
                    "timezone": point.timezone,
                    "sessions": [window.semantic_payload() for window in point.sessions],
                }
                for point in formal
            ],
            "warmup_sessions": [
                {
                    "date": point.session_date,
                    "session_id": point.session_id,
                    "timezone": point.timezone,
                    "sessions": [window.semantic_payload() for window in point.sessions],
                }
                for point in warmup
            ],
            "calendar_session_signature": calendar_session_signature,
            "warmup_session_signature": warmup_resolution.resolution_signature if warmup_resolution is not None else warmup_signature,
            "snapshot_id": str(snapshot.snapshot_id) if snapshot is not None else None,
            "snapshot_fingerprint": snapshot_fingerprint,
        }
        session_summary = json.loads(canonical_json(session_summary))
        return DataPreflightReport(
            status=PreflightStatus.BLOCKED if blocked else PreflightStatus.READY,
            generated_at=self._dataset.clock,
            provider_key=request.provider_key,
            capability_manifest_version=self._manifest_version,
            requested_window=request.requested_window,
            scope_mode=request.instrument_scope_mode,
            resolved_calendar_ids=calendar_ids,
            resolved_calendar_definitions=(
                snapshot.resolved_calendar_definitions if snapshot is not None else ()
            ),
            resolved_timezone=axis.timezone if axis is not None else None,
            calendar_axis_policy=request.calendar_axis_policy,
            calendar_compatibility_status=axis.status if axis is not None else CalendarAxisStatus.INCOMPATIBLE,
            calendar_session_signature=calendar_session_signature,
            resolved_sessions=formal,
            warmup_sessions=warmup,
            max_lookback_sessions=request.max_lookback_sessions,
            knowledge_as_of=request.query_boundary.knowledge_as_of,
            non_strict_pit_capabilities=self._non_strict_capabilities(request),
            consistency_mode=request.consistency_mode,
            consistency_token_capability=(request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN and request.consistency_token_contract is not None),
            consistency_token_contract=request.consistency_token_contract if request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN else None,
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
            issues=tuple(issue_list),
            warmup_resolution=warmup_resolution,
            warmup_resolution_signature=warmup_resolution.resolution_signature if warmup_resolution is not None else None,
            calendar_axis_differences=axis.differences if axis is not None else (),
            query_boundary=request.query_boundary,
            hash_schema_version=2 if snapshot is not None else 1,
            pit_context=pit_context.as_dict if snapshot is not None and pit_context is not None else None,
            calendar_revision_digest=revision_digest,
            snapshot_fingerprint=snapshot_fingerprint,
            non_strict_pit=DataCapability.BARS in request.required_capabilities,
            calendar_semantic_signature=semantic_signature,
            warmup_session_signature=warmup_signature,
            definition_usage_by_date=usage,
            calendar_summary=calendar_summary,
            session_summary=session_summary,
            qualification_policy_version=getattr(
                request, "qualification_policy_version", None
            ),
            universe_eligibility_policy_version=getattr(
                request, "qualification_policy_version", None
            ),
            universe_scope_snapshot_hash=(
                getattr(universe_scope_resolution, "snapshot_hash", None)
                if universe_scope_resolution is not None
                else getattr(request, "universe_scope_snapshot_hash", None)
            ),
            universe_eligibility_summary=(
                self._universe_report_summary(
                    request,
                    calendar_ids=calendar_ids,
                    resolution=universe_scope_resolution,
                )
                if universe_scope_resolution is not None
                else None
            ),
            universe_scope_resolution=universe_scope_resolution,
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

    def _non_strict_capabilities(
        self, request: DataPreflightRequest
    ) -> tuple[DataCapability, ...]:
        """Return requested fact families without strict cognition evidence."""

        return tuple(
            capability
            for capability in request.required_capabilities
            if self._manifest.pit_support_by_capability.get(capability)
            is PitSupport.NON_STRICT
        )

    def _covers_fact_type(self, capability: DataCapability) -> bool:
        """Whether the dataset actually backs one declared fact type."""

        if capability is DataCapability.BARS:
            return bool(self._dataset.bars)
        if capability is DataCapability.CALENDARS:
            return bool(self._dataset.calendar_facts)
        if capability is DataCapability.COVERAGE:
            return True
        if capability is DataCapability.UNIVERSE:
            return self._universe_supported
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
        max_lookback_sessions: int = MAX_LOOKBACK_SESSIONS,
        data_cutoff: datetime | None = None,
        knowledge_as_of: datetime | None = None,
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
                "max_lookback_sessions": max_lookback_sessions,
            },
            "query_boundary": {
                "data_cutoff": data_cutoff,
                "knowledge_as_of": knowledge_as_of,
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

    def resolve_pit_spec(
        self,
        instrument_id: UUID,
        *,
        effective_at: datetime,
        data_cutoff: datetime,
    ) -> InstrumentSpec | None:
        """Resolve one complete spec from the explicitly injected PIT source."""

        if not isinstance(instrument_id, UUID):
            raise InvalidDataRequestError("instrument_id must be a UUID")
        effective = _aware_datetime(effective_at, "effective_at")
        cutoff = _aware_datetime(data_cutoff, "data_cutoff")
        source = self._pit_spec_provider
        if source is None:
            return self._dataset.instrument_at(instrument_id, effective, cutoff)
        method = None
        for name in (
            "resolve_spec",
            "resolve_instrument",
            "resolve_identity",
            "resolve",
        ):
            candidate = getattr(source, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            return None
        values = {
            "instrument_id": instrument_id,
            "effective_at": effective,
            "effective_date": effective.date(),
            "data_cutoff": cutoff,
        }
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            result = method(instrument_id)
        else:
            kwargs = {
                name: value
                for name, value in values.items()
                if name in parameters
                and parameters[name].kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            positional = [
                parameter
                for parameter in parameters.values()
                if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
                or (
                    parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                    and parameter.default is inspect.Parameter.empty
                    and parameter.name not in kwargs
                )
            ]
            result = method(instrument_id, **kwargs) if positional else method(**kwargs)
        if isinstance(result, InstrumentSpec):
            return result
        for name in ("spec", "instrument_spec", "candidate_spec"):
            nested = getattr(result, name, None)
            if isinstance(nested, InstrumentSpec):
                return nested
        return None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def _fixed_authorized_ids(request: object) -> frozenset[UUID]:
    """Return the immutable fixed authorization union for one run.

    Admission normally folds non-zero initial positions into
    ``mandatory_instrument_ids``.  The defensive aliases below also accept
    newer request objects that retain the source position ids explicitly; this
    keeps the permission boundary correct during the task-15 migration without
    introducing a second request model in this module.
    """

    values: set[UUID] = set()
    for field_name in (
        "static_instrument_ids",
        "mandatory_instrument_ids",
        "non_zero_initial_position_instrument_ids",
    ):
        raw = getattr(request, field_name, ()) or ()
        if isinstance(raw, Mapping):
            raw = raw.keys()
        for item in raw:
            if isinstance(item, UUID):
                values.add(item)
    initial_positions = getattr(request, "initial_positions", None)
    if isinstance(initial_positions, Mapping):
        for instrument_id, position in initial_positions.items():
            if not isinstance(instrument_id, UUID):
                continue
            quantity = getattr(position, "quantity", position)
            try:
                if quantity is not None and quantity != 0:
                    values.add(instrument_id)
            except Exception:
                # Malformed position inputs are validated by the run
                # admission layer; they must not broaden this data boundary.
                continue
    return frozenset(values)


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
        # Session authorization is split into an immutable fixed union and a
        # mutable-per-step dynamic set.  The latter can only be populated by a
        # successful universe query and is reset whenever a new bound query is
        # evaluated; it never changes the frozen request or calendar axis.
        self._fixed_authorized_instrument_ids = _fixed_authorized_ids(request)
        self._step_candidate_authorized_instrument_ids: frozenset[UUID] = frozenset()
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

    @property
    def fixed_authorized_instrument_ids(self) -> frozenset[UUID]:
        """The fixed IDs authorized for every chunk of this run."""

        return self._fixed_authorized_instrument_ids

    @property
    def fixed_authorized_ids(self) -> frozenset[UUID]:
        """Short audit alias for ``fixed_authorized_instrument_ids``."""

        return self.fixed_authorized_instrument_ids

    @property
    def step_candidate_authorized_instrument_ids(self) -> frozenset[UUID]:
        """Dynamic IDs authorized by the most recent step universe query."""

        return self._step_candidate_authorized_instrument_ids

    @property
    def step_candidate_authorized_ids(self) -> frozenset[UUID]:
        """Short audit alias for current-step dynamic authorization."""

        return self.step_candidate_authorized_instrument_ids

    @property
    def authorized_instrument_ids(self) -> frozenset[UUID]:
        """Read-only union exposed for audit and permission assertions."""

        return frozenset(
            self._fixed_authorized_instrument_ids
            | self._step_candidate_authorized_instrument_ids
        )

    def authorize_step_candidates(self, instrument_ids, *, query=None) -> None:
        """Set the current step's dynamic authorization after universe query.

        This method is intentionally narrow: it accepts only UUID identities,
        requires a completed preflight, and never mutates the fixed scope.  A
        chunk owns the final subset check, so strategies cannot forge an id by
        calling this implementation directly with an unrelated candidate.
        """

        self._assert_not_closed()
        if self._state is not DataSessionState.READY:
            raise InvalidDataRequestError(
                "step candidate authorization requires a ready data session"
            )
        values = tuple(instrument_ids or ())
        normalized = frozenset(item for item in values if isinstance(item, UUID))
        if len(normalized) != len(values):
            raise InvalidDataRequestError(
                "step candidate authorization requires UUID instrument ids"
            )
        if normalized and query is None:
            raise InvalidDataRequestError(
                "dynamic step candidates require the bound UniverseQuery result"
            )
        if query is not None:
            result_ids = getattr(query, "authorized_instrument_ids", None)
            if result_ids is None:
                result_ids = getattr(query, "candidate_ids", None)
            if result_ids is None:
                raise InvalidDataRequestError(
                    "dynamic step candidates require a query result with identities"
                )
            if not normalized.issubset(set(result_ids)):
                raise InvalidDataRequestError(
                    "step candidates are not contained in the bound universe result"
                )
        self._step_candidate_authorized_instrument_ids = normalized

    # Vocabulary aliases keep adapters independent of the concrete memory
    # fixture name while preserving one authorization implementation.
    bind_step_candidates = authorize_step_candidates
    authorize_step_candidate_ids = authorize_step_candidates

    def clear_step_candidate_authorization(self) -> None:
        """Remove dynamic access until the next successful universe query."""

        self._assert_not_closed()
        if self._state is not DataSessionState.READY:
            raise InvalidDataRequestError(
                "step candidate authorization requires a ready data session"
            )
        self._step_candidate_authorized_instrument_ids = frozenset()
        self._session._step_candidate_authorized_instrument_ids = frozenset()

    def begin_decision_step(self, step_key: object | None = None) -> None:
        """Start a decision-step authorization epoch for session-level audits."""

        self._assert_not_closed()
        if self._state is not DataSessionState.READY:
            raise InvalidDataRequestError(
                "decision-step authorization requires a ready data session"
            )
        self._step_candidate_authorized_instrument_ids = frozenset()
        del step_key

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
        try:
            report = self._provider._build_preflight_report(
                self._request,
                # The calendars frozen at admission are authoritative; the
                # re-check must not silently re-derive a different axis.
                frozen_calendar_ids=self._request.resolved_calendar_ids,
                profile=self._provider._profile_for_admission(
                    self._request.admission_preflight_hash
                ),
            )
        except CalendarPreflightResourceLimitExceededError:
            # A session-level resource overrun has no report, cursor, or
            # persistence lifecycle.  Keep the session terminal and bubble
            # the stable error to the creation coordinator.
            self._report = None
            self._resolved_sessions = ()
            self._warmup_sessions = ()
            self._revision_snapshot = None
            self._state = DataSessionState.BLOCKED
            raise
        self._report = report
        self._resolved_sessions = report.resolved_sessions
        self._warmup_sessions = report.warmup_sessions
        self._state = (
            DataSessionState.BLOCKED
            if report.status is PreflightStatus.BLOCKED
            else DataSessionState.READY
        )
        # Dynamic authorization starts empty for every authoritative
        # preflight.  A candidate can enter a chunk only after that chunk's
        # current-step universe query proves it eligible.
        self._step_candidate_authorized_instrument_ids = frozenset()
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
        # Type-check before any attribute access: a foreign object (or
        # None) must fail with the stable contract error instead of an
        # AttributeError, and can never bypass the DTO's own invariants
        # such as the non-negative chunk_index.
        if not isinstance(query, DataChunkQuery):
            raise InvalidDataRequestError("query must be a DataChunkQuery")
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
            "max_lookback_sessions": self._request.max_lookback_sessions,
            "data_cutoff": self._request.query_boundary.data_cutoff,
            "knowledge_as_of": self._request.query_boundary.knowledge_as_of,
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
            fixed_authorized_instrument_ids=self._fixed_authorized_instrument_ids,
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
        fixed_authorized_instrument_ids: frozenset[UUID] | None = None,
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
        self._fixed_authorized_instrument_ids = frozenset(
            fixed_authorized_instrument_ids
            if fixed_authorized_instrument_ids is not None
            else _fixed_authorized_ids(session._request)
        )
        self._step_candidate_authorized_instrument_ids: frozenset[UUID] = frozenset()
        self._universe_cache: dict[UniverseQuery, tuple[InstrumentSpec, ...]] = {}
        self._universe_specs: dict[UUID, InstrumentSpec] = {}
        self._universe_filter_reason_counts: dict[str, int] = {}
        self._universe_filter_records: list[Mapping[str, object]] = []
        self._universe_last_query_hash: str | None = None
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
        if self._closed:
            return
        self._closed = True
        # Chunk-scoped caches and authorization handles must not survive the
        # resource boundary; retaining them would create an unbounded,
        # cross-token cache and leak permissions into a later chunk.
        self._universe_cache.clear()
        self._universe_specs.clear()
        self._universe_filter_reason_counts.clear()
        self._universe_filter_records.clear()
        self._universe_last_query_hash = None
        self._step_candidate_authorized_instrument_ids = frozenset()

    def begin_decision_step(self, step_key: object | None = None) -> None:
        """Reset dynamic authorization for a new decision step.

        The fixed authorization remains immutable for the chunk lifetime.
        ``step_key`` is intentionally advisory: callers may pass a sequence or
        timestamp for diagnostics, but no user value can widen the scope.
        """

        self._assert_open()
        self._step_candidate_authorized_instrument_ids = frozenset()
        self._session._step_candidate_authorized_instrument_ids = frozenset()
        del step_key

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

    @property
    def fixed_authorized_instrument_ids(self) -> frozenset[UUID]:
        """Fixed identities allowed for every operation in this chunk."""

        return self._fixed_authorized_instrument_ids

    @property
    def fixed_authorized_ids(self) -> frozenset[UUID]:
        """Short audit alias for ``fixed_authorized_instrument_ids``."""

        return self.fixed_authorized_instrument_ids

    @property
    def step_candidate_authorized_instrument_ids(self) -> frozenset[UUID]:
        """Dynamic identities admitted by the current bound universe query."""

        return self._step_candidate_authorized_instrument_ids

    @property
    def step_candidate_authorized_ids(self) -> frozenset[UUID]:
        """Short audit alias for current-step dynamic authorization."""

        return self.step_candidate_authorized_instrument_ids

    @property
    def authorized_instrument_ids(self) -> frozenset[UUID]:
        """The read-only union of fixed and current-step identities."""

        return frozenset(
            self._fixed_authorized_instrument_ids
            | self._step_candidate_authorized_instrument_ids
        )

    @property
    def universe_filter_reason_counts(self) -> Mapping[str, int]:
        """Stable candidate-level filter counts from the latest query calls."""

        return MappingProxyType(dict(self._universe_filter_reason_counts))

    @property
    def universe_filter_records(self) -> tuple[Mapping[str, object], ...]:
        """JSON-safe candidate filter evidence for audit projections."""

        return tuple(self._universe_filter_records)

    @property
    def universe_last_query_hash(self) -> str | None:
        """Deterministic hash of the latest bound PIT universe query."""

        return self._universe_last_query_hash

    @property
    def universe_query_hash(self) -> str | None:
        """Compatibility alias for the latest bound universe query hash."""

        return self.universe_last_query_hash

    @property
    def candidate_filter_summary(self) -> Mapping[str, object]:
        """Return JSON-safe counts and row evidence for the latest query."""

        return MappingProxyType(
            {
                "reason_counts": MappingProxyType(
                    dict(self._universe_filter_reason_counts)
                ),
                "records": tuple(self._universe_filter_records),
                "query_hash": self._universe_last_query_hash,
            }
        )

    def authorize_step_candidates(self, instrument_ids, *, query=None) -> None:
        """Replace dynamic authorization with a validated current-step set.

        Only identities returned by the corresponding bound query may be
        supplied.  Fixed IDs remain independently authorized, and passing an
        empty set intentionally removes all dynamic access for the step.
        """

        self._guard_business_query("authorize_step_candidates")
        values = tuple(instrument_ids or ())
        if any(not isinstance(item, UUID) for item in values):
            raise InvalidDataRequestError(
                "step candidate authorization requires UUID instrument ids"
            )
        normalized = frozenset(values)
        if normalized and query is None:
            raise InvalidDataRequestError(
                "dynamic step candidates require the bound UniverseQuery result"
            )
        if query is not None:
            if not isinstance(query, UniverseQuery):
                raise InvalidDataRequestError("query must be a UniverseQuery")
            available = {
                spec.instrument_id
                for spec in self._universe_cache.get(query, ())
            }
            if not normalized.issubset(available):
                raise InvalidDataRequestError(
                    "step authorization includes an identity not returned by "
                    "the bound universe query",
                    details={
                        "unknown_instrument_ids": sorted(
                            str(item) for item in normalized - available
                        )
                    },
                )
        self._step_candidate_authorized_instrument_ids = normalized
        self._session._step_candidate_authorized_instrument_ids = normalized

    # Keep one implementation while accepting the vocabulary used by the
    # strategy/session adapters in task-15 acceptance tests.
    bind_step_candidates = authorize_step_candidates
    authorize_step_candidate_ids = authorize_step_candidates

    def clear_step_candidate_authorization(self) -> None:
        """Remove dynamic access until the next successful universe query."""

        self._guard_business_query("clear_step_candidate_authorization")
        self._step_candidate_authorized_instrument_ids = frozenset()
        self._session._step_candidate_authorized_instrument_ids = frozenset()

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
            covered_chunk_start=(
                self._chunk_index if status is ConsistencyValidation.VALID else None
            ),
            covered_chunk_end=(
                self._chunk_index + 1 if status is ConsistencyValidation.VALID else None
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
        """Return fixed plus current-step dynamic read authorization.

        The dataset may contain many candidate fixtures, but only fixed ids
        admitted at run creation and ids returned by the current bound
        universe query are readable through instrument/bar/coverage methods.
        """

        return frozenset(
            self._fixed_authorized_instrument_ids
            | self._step_candidate_authorized_instrument_ids
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
                    "frozen fixed/current-step scope",
                details={
                    "unauthorized_instrument_ids": [
                        str(instrument_id) for instrument_id in strangers
                    ],
                },
            )

    @staticmethod
    def _require_query_type(query: object, expected: type, operation: str) -> None:
        """Reject foreign query objects with the stable contract error.

        Attribute access on ``None`` or an arbitrary class would otherwise
        surface as AttributeError, and a forged duck-typed object could
        bypass the DTO's own invariant validation.
        """

        if not isinstance(query, expected):
            raise InvalidDataRequestError(
                f"{operation} query must be a {expected.__name__}"
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

    def _universe_query_cache_key(self, query: UniverseQuery) -> str:
        """Create a stable cache key from all frozen PIT query semantics."""

        request = self._session._request
        def reference(value: object) -> object:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            return {"key": value.key, "version": value.version}

        return canonical_json(
            {
                "rule": {
                    "key": query.rule.key,
                    "version": query.rule.version,
                },
                "market_scope": {
                    "markets": query.market_scope.markets,
                    "exchanges": query.market_scope.exchanges,
                    "asset_classes": query.market_scope.asset_classes,
                    "currencies": query.market_scope.currencies,
                },
                "effective_date": query.effective_date,
                "data_cutoff": query.boundary.data_cutoff,
                "knowledge_as_of": query.boundary.knowledge_as_of,
                "include_cutoff_day": query.boundary.include_cutoff_day,
                "allowed_calendar_ids": query.allowed_calendar_ids,
                "universe_query_policy": [
                    {
                        "key": item.key,
                        "version": item.version,
                    }
                    for item in query.universe_query_policy.candidate_set_rules
                ],
                "rule_exception_set": reference(query.rule_exception_set),
                "qualification_policy_version": reference(
                    query.qualification_policy_version
                ),
                "scope_mode": (
                    query.scope_mode.value if query.scope_mode is not None else None
                ),
                # Keep this field in the payload as a guard against a future
                # request implementation accidentally sharing a cache across
                # providers with different admission semantics.
                "request_provider": request.provider_key,
                # Include the owning chunk's frozen formal scope.  Although
                # caches are currently chunk-local, this guard prevents a
                # future adapter from accidentally reusing a query result
                # across chunks with different PIT/session boundaries.
                "chunk_scope": {
                    "chunk_index": self._chunk_index,
                    "first_session_id": self._sessions[0].session_id,
                    "last_session_id": self._sessions[-1].session_id,
                },
            }
        )

    def _validate_universe_query_boundary(self, query: UniverseQuery) -> None:
        """Reject any query that attempts to replace the run's frozen scope."""

        request = self._session._request
        expected_scope_mode = getattr(request, "instrument_scope_mode", None)
        if query.scope_mode is not None and query.scope_mode is not expected_scope_mode:
            raise UniversePitBoundaryViolationError(
                "universe query scope mode differs from the frozen request",
                details={
                    "expected": expected_scope_mode.value
                    if expected_scope_mode is not None
                    else None,
                    "actual": query.scope_mode.value,
                },
            )
        if query.rule != request.rule_package:
            raise UniversePitBoundaryViolationError(
                "universe query cannot replace the frozen rule package",
                details={
                    "expected": {
                        "key": request.rule_package.key,
                        "version": request.rule_package.version,
                    },
                    "actual": {"key": query.rule.key, "version": query.rule.version},
                },
            )
        if query.market_scope != request.market_scope:
            raise UniversePitBoundaryViolationError(
                "universe query cannot replace the frozen market scope"
            )
        expected_universe_policy = getattr(
            request, "universe_query_policy", UniverseQueryPolicy()
        )
        if query.universe_query_policy != expected_universe_policy:
            raise UniversePitBoundaryViolationError(
                "universe query cannot replace the frozen universe query policy",
                details={
                    "expected": [
                        {
                            "key": item.key,
                            "version": item.version,
                        }
                        for item in expected_universe_policy.candidate_set_rules
                    ],
                    "actual": [
                        {
                            "key": item.key,
                            "version": item.version,
                        }
                        for item in query.universe_query_policy.candidate_set_rules
                    ],
                },
            )
        if query.rule_exception_set != getattr(request, "rule_exception_set", None):
            raise UniversePitBoundaryViolationError(
                "universe query cannot replace the frozen rule exception set"
            )
        expected_policy = getattr(request, "qualification_policy_version", None)
        if query.qualification_policy_version != expected_policy:
            raise UniversePitBoundaryViolationError(
                "universe query cannot replace the frozen qualification policy"
            )
        expected_hash = getattr(request, "universe_scope_snapshot_hash", None)
        if expected_hash is not None and query.universe_scope_snapshot_hash != expected_hash:
            raise UniversePreflightHashMismatchError(
                "universe query scope snapshot hash differs from the frozen request",
                details={
                    "expected": expected_hash,
                    "actual": query.universe_scope_snapshot_hash,
                },
            )
        expected_calendars = set()
        for value in getattr(request, "resolved_calendar_ids", ()):
            try:
                from app.backtesting.calendar_axis import normalize_calendar_id

                expected_calendars.add(normalize_calendar_id(value))
            except Exception:
                expected_calendars.add(value)
        actual_calendars = set(query.allowed_calendar_ids)
        query_scope_mode = query.scope_mode or expected_scope_mode
        if (
            actual_calendars - expected_calendars
            or (
                query_scope_mode is not InstrumentScopeMode.FIXED
                and actual_calendars != expected_calendars
            )
        ):
            raise UniversePitBoundaryViolationError(
                "universe query cannot replace the frozen preflighted calendars",
                details={
                    "expected_calendar_ids": sorted(expected_calendars),
                    "actual_calendar_ids": sorted(actual_calendars),
                },
            )
        expected_boundary = request.query_boundary
        # A step query may narrow the run's visibility to the current
        # decision cutoff.  It may never move that cutoff later; omission of a
        # strict cognition cutoff is allowed only when the frozen request did
        # not declare one.
        if query.boundary.data_cutoff > expected_boundary.data_cutoff or (
            expected_boundary.knowledge_as_of is not None
            and (
                query.boundary.knowledge_as_of is None
                or query.boundary.knowledge_as_of
                > expected_boundary.knowledge_as_of
            )
        ):
            raise UniversePitBoundaryViolationError(
                "universe query cannot widen the frozen QueryBoundary",
                details={
                    "expected_data_cutoff": expected_boundary.data_cutoff.isoformat(),
                    "actual_data_cutoff": query.boundary.data_cutoff.isoformat(),
                },
            )
        if query.boundary.include_cutoff_day and not expected_boundary.include_cutoff_day:
            raise UniversePitBoundaryViolationError(
                "universe query cannot make an unproven cutoff day visible",
                details={
                    "cutoff_date": query.boundary.cutoff_date.isoformat(),
                    "include_cutoff_day": query.boundary.include_cutoff_day,
                },
            )
        if not (
            request.requested_window.start_date
            <= query.effective_date
            <= request.requested_window.end_date
        ):
            raise UniversePitBoundaryViolationError(
                "universe effective_date is outside the frozen run window",
                details={
                    "expected": {
                        "start_date": request.requested_window.start_date.isoformat(),
                        "end_date": request.requested_window.end_date.isoformat(),
                    },
                    "actual": query.effective_date.isoformat(),
                },
            )

    @staticmethod
    def _extract_universe_candidate(row: object) -> tuple[InstrumentSpec | None, tuple[str, ...]]:
        """Extract a spec and any precomputed qualification reasons."""

        if isinstance(row, InstrumentSpec):
            return row, ()
        if isinstance(row, tuple) and len(row) == 2:
            # A lightweight provider may return ``(spec, qualification)``;
            # normalize that shape without defining a second qualification
            # model in the memory adapter.
            left, right = row
            if isinstance(left, InstrumentSpec):
                if isinstance(right, (str, bytes, UUID)):
                    return left, ()
                try:
                    right_spec, right_reasons = MemoryDataChunkSession._extract_universe_candidate(right)
                except UniverseProviderContractViolationError:
                    # The right value can be a stable id used only as a
                    # resolver hint; it contributes no qualification reason.
                    return left, ()
                del right_spec
                return left, right_reasons
            if isinstance(right, InstrumentSpec):
                if isinstance(left, (str, bytes, UUID)):
                    return right, ()
                try:
                    left_spec, left_reasons = MemoryDataChunkSession._extract_universe_candidate(left)
                except UniverseProviderContractViolationError:
                    return right, ()
                del left_spec
                return right, left_reasons
        # Task-13's immutable qualification result intentionally remains the
        # source of truth.  Duck typing avoids importing that provider module
        # into the memory fixture and keeps this adapter usable with its
        # aliases (InstrumentEligibility/SingleInstrumentQualification).
        spec = getattr(row, "spec", None)
        if spec is None:
            for name in ("instrument_spec", "candidate_spec", "candidate"):
                candidate = getattr(row, name, None)
                if isinstance(candidate, InstrumentSpec):
                    spec = candidate
                    break
        reasons = getattr(row, "reason_codes", ())
        if not reasons:
            issues = getattr(row, "issues", ())
            reasons = tuple(
                getattr(issue, "code", str(issue))
                for issue in (issues or ())
            )
        if spec is not None and not isinstance(spec, InstrumentSpec):
            raise UniverseProviderContractViolationError(
                "universe provider qualification returned a non-InstrumentSpec"
            )
        eligible = getattr(row, "eligible", None)
        if spec is None and eligible is None and not hasattr(row, "status"):
            raise UniverseProviderContractViolationError(
                "universe provider returned neither an InstrumentSpec nor a qualification result"
            )
        if eligible is False and not reasons:
            reasons = ("candidate_ineligible",)
        return spec, tuple(sorted({str(reason) for reason in reasons if reason}))

    @staticmethod
    def _stable_source_row_key(row: object, spec: InstrumentSpec) -> tuple[str, ...]:
        """Build a value-only tie-break for duplicate stable identities."""

        display = getattr(spec, "display", None)
        reason_codes = tuple(
            sorted(
                str(value)
                for value in (getattr(row, "reason_codes", ()) or ())
            )
        )
        evidence = tuple(
            (
                name,
                str(getattr(row, name, "")),
            )
            for name in (
                "identity_evidence",
                "mapping_evidence",
                "rule_evidence",
                "market_data_evidence",
                "corporate_action_evidence",
                "quantity_action_coverage_evidence",
                "status_evidence",
            )
            if hasattr(row, name)
        )
        values = (
            getattr(display, "trading_code", "") if display is not None else "",
            getattr(display, "name", "") if display is not None else "",
            getattr(display, "display_name", "") if display is not None else "",
            getattr(spec, "asset_class", ""),
            getattr(spec, "exchange", ""),
            str(getattr(spec, "valid_from", "")),
            str(getattr(spec, "valid_to", "")),
            str(getattr(row, "known_at", "")),
            str(getattr(row, "effective_date", "")),
            str(getattr(row, "eligible", "")),
            str(reason_codes),
            str(evidence),
        )
        return tuple(str(value) for value in values)

    def _candidate_filter_reasons(
        self,
        spec: InstrumentSpec,
        query: UniverseQuery,
        precomputed_reasons: Sequence[str],
        row: object | None = None,
    ) -> tuple[str, ...]:
        """Apply only identity, scope, and PIT-boundary checks.

        Coverage, rule, action, and trading-status qualification belongs to
        the injected task-13/16A port.  Keeping this helper free of Bar or
        corporate-action reads is intentional: the memory chunk is an
        adapter, not a second fact-qualification implementation.
        """

        reasons = set(precomputed_reasons)
        request = self._session._request
        try:
            effective_at = datetime.combine(query.effective_date, time.min, tzinfo=UTC)
            if spec.valid_from > effective_at or (
                spec.valid_to is not None and effective_at >= spec.valid_to
            ):
                reasons.add("identity_not_valid_at_effective_date")
        except (TypeError, AttributeError):
            reasons.add("identity_fact_invalid")

        display = spec.display
        if any(
            type(value) is not str or not value.strip()
            for value in (display.trading_code, display.name, display.display_name)
        ):
            reasons.add("identity_mapping_incomplete")

        scope = query.market_scope
        if scope.exchanges and spec.exchange not in scope.exchanges:
            reasons.add("market_scope_exchange")
        if scope.asset_classes and spec.asset_class not in scope.asset_classes:
            reasons.add("market_scope_asset_class")
        if scope.currencies and spec.currency not in scope.currencies:
            reasons.add("market_scope_currency")
        if scope.markets and spec.exchange not in scope.markets:
            reasons.add("market_scope_market")

        try:
            calendar_id = normalize_calendar_id(spec.calendar_id)
        except Exception:
            reasons.add("identity_calendar_invalid")
            calendar_id = None
        scope_mode = query.scope_mode or getattr(
            request, "instrument_scope_mode", InstrumentScopeMode.FIXED
        )
        if scope_mode is not InstrumentScopeMode.FIXED:
            if not query.allowed_calendar_ids or calendar_id not in set(query.allowed_calendar_ids):
                reasons.add("universe_calendar_not_preflighted")

        if spec.rule_package_reference != query.rule:
            reasons.add("rule_package_mismatch")
        expected_exception = query.rule_exception_set
        if expected_exception is not None and spec.rule_exception_reference not in (
            None,
            expected_exception,
        ):
            reasons.add("rule_exception_mismatch")

        # Source rows may carry explicit PIT coordinates in addition to the
        # complete InstrumentSpec.  Reject future knowledge or a row resolved
        # for another effective date; never substitute the current spec.
        source = row if row is not None else spec
        effective_value = getattr(source, "effective_date", None)
        if effective_value is None:
            effective_at = getattr(source, "effective_at", None)
            if isinstance(effective_at, datetime):
                effective_value = effective_at.date()
        if effective_value is not None:
            if effective_value != query.effective_date:
                reasons.add("universe_pit_boundary_violation")
        provenance_rows = [source]
        for name in (
            "identity_fact",
            "display_fact",
            "mapping",
            "identity_resolution",
            "qualification",
        ):
            nested = getattr(source, name, None)
            if nested is not None:
                provenance_rows.append(nested)
        for provenance in provenance_rows:
            known_at = getattr(provenance, "known_at", None)
            if known_at is None:
                identity_evidence = getattr(provenance, "identity_evidence", None)
                if isinstance(identity_evidence, Mapping):
                    known_at = identity_evidence.get("known_at")
            if known_at is None:
                continue
            try:
                if isinstance(known_at, str):
                    known_at = datetime.fromisoformat(known_at)
                if not isinstance(known_at, datetime) or known_at.tzinfo is None:
                    reasons.add("universe_pit_boundary_violation")
                elif known_at > query.boundary.data_cutoff:
                    reasons.add("universe_pit_boundary_violation")
            except (TypeError, ValueError):
                reasons.add("universe_pit_boundary_violation")
        return tuple(sorted(reasons))

    @staticmethod
    def _qualification_report_evidence(report: object) -> Mapping[str, object]:
        """Project one existing coverage report into evaluator evidence."""

        get = report.get if isinstance(report, Mapping) else lambda name, default=None: getattr(report, name, default)
        quality = get("quality_status")
        quality_value = getattr(quality, "value", quality)
        expected = get("expected_count")
        complete_count = get("complete_count")
        complete = quality_value == "complete"
        if (
            not complete
            and isinstance(expected, int)
            and isinstance(complete_count, int)
            and expected > 0
        ):
            complete = complete_count >= expected
        return {
            "complete": bool(complete),
            "quality_status": quality_value,
            "expected_count": expected,
            "complete_count": complete_count,
            "partial_count": get("partial_count"),
            "invalid_count": get("invalid_count"),
            "unavailable_count": get("unavailable_count"),
            "missing_ranges": get("missing_ranges", ()),
        }

    @classmethod
    def _qualification_evidence(
        cls,
        result: object,
        required_capabilities: Sequence[DataCapability],
        spec: InstrumentSpec,
    ) -> dict[str, Mapping[str, object]]:
        """Map a port result to the evaluator's named evidence dimensions."""

        evidence: dict[str, Mapping[str, object]] = {}
        reports_value = (
            result.get("coverage_reports", ())
            if isinstance(result, Mapping)
            else getattr(result, "coverage_reports", ())
        )
        reports = tuple(reports_value or ())
        for report in reports:
            capability = (
                report.get("capability")
                if isinstance(report, Mapping)
                else getattr(report, "capability", None)
            )
            capability_value = getattr(capability, "value", capability)
            projected = cls._qualification_report_evidence(report)
            if capability_value in ("bars", "coverage"):
                evidence["market_data_evidence"] = projected
            elif capability_value == "actions":
                evidence["corporate_action_evidence"] = projected
                evidence["quantity_action_coverage_evidence"] = projected
            elif capability_value == "status":
                evidence["status_evidence"] = projected
            elif capability_value == "rules":
                evidence["rule_evidence"] = projected
            elif capability_value == "mappings":
                evidence["mapping_evidence"] = projected

        summary = (
            result.get("evidence_summary")
            if isinstance(result, Mapping)
            else getattr(result, "evidence_summary", None)
        )
        if isinstance(summary, Mapping):
            for name in (
                "identity_evidence",
                "mapping_evidence",
                "rule_evidence",
                "market_data_evidence",
                "corporate_action_evidence",
                "quantity_action_coverage_evidence",
                "status_evidence",
            ):
                value = summary.get(name)
                if isinstance(value, Mapping):
                    evidence.setdefault(name, value)
            # Some existing 16A ports publish one nested coverage summary
            # instead of one mapping per capability.  It remains a source
            # result, not a reason to inspect Bars here.
            for name in ("coverage", "bars", "market_data"):
                value = summary.get(name)
                if isinstance(value, Mapping):
                    evidence.setdefault("market_data_evidence", value)

        capabilities = getattr(spec, "capabilities", None)
        action_requirement = getattr(capabilities, "corporate_action_requirement", None)
        action_value = getattr(action_requirement, "value", action_requirement)
        if action_value == "not_applicable":
            evidence.setdefault(
                "corporate_action_evidence",
                {"applicability": "not_applicable", "explicit": True},
            )
            evidence.setdefault(
                "quantity_action_coverage_evidence",
                {"applicability": "not_applicable", "explicit": True},
            )
        status_policy = getattr(spec, "trading_status_policy", None)
        if isinstance(status_policy, Mapping) and status_policy:
            values = {
                getattr(value, "value", value)
                for value in status_policy.values()
            }
            if values and values <= {"not_applicable"}:
                evidence.setdefault(
                    "status_evidence",
                    {"applicability": "not_applicable", "explicit": True},
                )
        return evidence

    @staticmethod
    def _qualification_result_reason_codes(result: object) -> tuple[str, ...]:
        """Read stable reason codes from an existing qualification result."""

        values: list[str] = []
        result_reason_codes = (
            result.get("reason_codes", ())
            if isinstance(result, Mapping)
            else getattr(result, "reason_codes", ())
        )
        for item in result_reason_codes or ():
            value = getattr(item, "value", item)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
        result_issues = (
            result.get("issues", ()) if isinstance(result, Mapping) else getattr(result, "issues", ())
        )
        for issue in result_issues or ():
            value = issue.get("code") if isinstance(issue, Mapping) else getattr(issue, "code", None)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
        return tuple(sorted(set(values)))

    @staticmethod
    def _required_qualification_reasons(
        result: object,
        evidence: Mapping[str, Mapping[str, object]],
        request: DataRequest,
        spec: InstrumentSpec,
    ) -> tuple[str, ...]:
        """Require explicit evidence for every requested fact dimension."""

        from app.backtesting.data.universe import (
            _candidate_status_policy_requirements,
        )

        reasons: set[str] = set()
        required = set(request.required_capabilities)
        capabilities = getattr(spec, "capabilities", None)
        action_requirement = getattr(
            capabilities, "corporate_action_requirement", None
        )
        if getattr(action_requirement, "value", action_requirement) == "required":
            required.add(DataCapability.ACTIONS)
        required_status_dimensions, declaration_valid, declaration_present = (
            _candidate_status_policy_requirements(None, spec)
        )
        if declaration_present and declaration_valid and not required_status_dimensions:
            # A valid all-N/A candidate does not need status evidence merely
            # because another candidate caused the run to request STATUS.
            required.discard(DataCapability.STATUS)

        def proven(value: Mapping[str, object] | None) -> bool:
            if not isinstance(value, Mapping):
                return False
            applicability = value.get("applicability")
            if (
                getattr(applicability, "value", applicability) == "not_applicable"
                and value.get("explicit") is True
            ):
                return True
            for name in ("complete", "covered", "available", "valid", "eligible"):
                if value.get(name) is True:
                    return True
            quality = value.get("quality_status")
            return getattr(quality, "value", quality) == "complete"

        if DataCapability.BARS in required and not proven(
            evidence.get("market_data_evidence")
        ):
            reasons.add("candidate_market_data_incomplete")
        if DataCapability.COVERAGE in required and not proven(
            evidence.get("market_data_evidence")
        ):
            reasons.add("candidate_market_data_incomplete")
        if DataCapability.ACTIONS in required and not proven(
            evidence.get("corporate_action_evidence")
        ):
            reasons.add("candidate_corporate_action_incomplete")
        if DataCapability.ACTIONS in required and not proven(
            evidence.get("quantity_action_coverage_evidence")
        ):
            reasons.add("candidate_quantity_action_coverage_incomplete")
        if DataCapability.STATUS in required and not proven(
            evidence.get("status_evidence")
        ):
            reasons.add("candidate_status_incomplete")
        if DataCapability.RULES in required and not proven(
            evidence.get("rule_evidence")
        ):
            reasons.add("candidate_rule_incomplete")
        if DataCapability.MAPPINGS in required and not proven(
            evidence.get("mapping_evidence")
        ):
            reasons.add("candidate_mapping_incomplete")
        return tuple(sorted(reasons))

    @staticmethod
    def _invoke_qualification_method(
        method,
        qualification_request: CoverageQualificationRequest,
    ) -> object:
        """Invoke one existing qualification port without guessing arguments."""

        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            return method(qualification_request)
        if "request" in parameters:
            parameter = parameters["request"]
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                return method(qualification_request)
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                return method(request=qualification_request)
        if "qualification_request" in parameters:
            parameter = parameters["qualification_request"]
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                return method(qualification_request)
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                return method(qualification_request=qualification_request)
        values = {
            name: getattr(qualification_request, name)
            for name in CoverageQualificationRequest.__dataclass_fields__
        }
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return method(**values)
        kwargs = {
            name: values[name]
            for name, parameter in parameters.items()
            if name in values
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        required_positional = [
            parameter
            for parameter in parameters.values()
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            or (
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and parameter.default is inspect.Parameter.empty
                and parameter.name not in kwargs
            )
        ]
        if required_positional:
            return method(qualification_request)
        return method(**kwargs)

    def _build_qualification_request(
        self,
        instrument_id: UUID,
        query: UniverseQuery,
        spec: InstrumentSpec | None = None,
    ) -> CoverageQualificationRequest:
        """Build a single immutable coverage request from the frozen query."""

        request = self._session._request
        required_values = {
            capability
            for capability in request.required_capabilities
            if capability is not DataCapability.UNIVERSE
        }
        # The rule declaration, not an empty event table, decides whether
        # action evidence is mandatory.  STATUS stays aligned with the
        # frozen request here: a candidate-only required declaration must be
        # reported as a capability mismatch, not used to widen this request.
        if spec is None:
            spec = self._candidate_spec_for_query(instrument_id, query)
        capabilities = getattr(spec, "capabilities", None)
        action_requirement = getattr(capabilities, "corporate_action_requirement", None)
        if getattr(action_requirement, "value", action_requirement) == "required":
            required_values.add(DataCapability.ACTIONS)
        required = tuple(sorted(required_values, key=lambda item: item.value))
        if not required:
            # A universe query still needs a qualification port invocation;
            # COVERAGE is the narrow generic dimension for an otherwise
            # metadata-only dynamic request.
            required = (DataCapability.COVERAGE,)
        profile = (
            INTERNAL_LINK_ACCEPTANCE_PROFILE
            if self._provider.fixtures
            else FORMAL_PROFILE
        )
        qualification_policy = query.qualification_policy_version
        if isinstance(qualification_policy, str):
            key, separator, version = qualification_policy.rpartition("@")
            if separator and key.strip() and version.isdigit():
                qualification_policy = ContractRef(key.strip(), int(version))
            else:
                raise InvalidDataRequestError(
                    "qualification_policy_version must be an exact key@version reference"
                )
        return CoverageQualificationRequest(
            instrument_id=instrument_id,
            effective_date=query.effective_date,
            requested_window=request.requested_window,
            formal_envelope=request.requested_window,
            warmup_envelope=None,
            history_envelope=request.requested_window,
            required_capabilities=required,
            query_boundary=query.boundary,
            preflight_profile=profile,
            resolved_calendar_ids=query.allowed_calendar_ids,
            rule_package=query.rule_package_reference or query.rule,
            rule_exception_set=query.rule_exception_set,
            market_scope=query.market_scope,
            universe_query_policy=query.universe_query_policy,
            qualification_policy_version=qualification_policy,
            required_fixture_capabilities=tuple(
                fixture.capability for fixture in self._provider.fixtures
            ),
            fixtures=self._provider.fixtures,
            frequency=request.frequency,
        )

    def _candidate_spec_for_query(
        self,
        instrument_id: UUID,
        query: UniverseQuery,
    ) -> InstrumentSpec | None:
        """Resolve a spec only from the explicit PIT source when available."""

        provider = self._provider._pit_spec_provider
        if provider is None:
            return self._provider.dataset.instrument_at(
                instrument_id,
                datetime.combine(query.effective_date, time.min, tzinfo=UTC),
                query.boundary.data_cutoff,
            )
        method = None
        for name in (
            "resolve_spec",
            "resolve_instrument",
            "resolve_identity",
            "resolve",
        ):
            candidate = getattr(provider, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            return None
        values = {
            "instrument_id": instrument_id,
            "effective_at": datetime.combine(
                query.effective_date, time.min, tzinfo=UTC
            ),
            "effective_date": query.effective_date,
            "data_cutoff": query.boundary.data_cutoff,
            "rule_package_reference": query.rule,
            "rule": query.rule,
        }
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            result = method(instrument_id)
        else:
            kwargs = {
                name: value
                for name, value in values.items()
                if name in parameters
                and parameters[name].kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            positional = [
                parameter
                for parameter in parameters.values()
                if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
                or (
                    parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                    and parameter.default is inspect.Parameter.empty
                    and parameter.name not in kwargs
                )
            ]
            result = method(instrument_id, **kwargs) if positional else method(**kwargs)
        if isinstance(result, InstrumentSpec):
            return result
        for name in ("spec", "instrument_spec", "candidate_spec"):
            nested = getattr(result, name, None)
            if isinstance(nested, InstrumentSpec):
                return nested
        return None

    def _resolve_instrument_qualification(
        self,
        instrument_id: UUID,
        query: UniverseQuery,
    ) -> object | None:
        """Consume the explicit task-13 single-instrument qualification port."""

        provider = self._provider._pit_spec_provider
        if provider is None:
            return None
        method = None
        for name in (
            "qualify",
            "qualify_instrument",
            "resolve_qualification",
        ):
            candidate = getattr(provider, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            return None
        effective_at = datetime.combine(query.effective_date, time.min, tzinfo=UTC)
        values = {
            "instrument_id": instrument_id,
            "effective_at": effective_at,
            "effective_date": query.effective_date,
            "data_cutoff": query.boundary.data_cutoff,
            "rule_package_reference": query.rule,
            "exception_set_reference": query.rule_exception_set,
            "rule_exception_set": query.rule_exception_set,
        }
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            return method(instrument_id)
        kwargs = {
            name: value
            for name, value in values.items()
            if name in parameters
            and parameters[name].kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        positional = [
            parameter
            for parameter in parameters.values()
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            or (
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and parameter.default is inspect.Parameter.empty
                and parameter.name not in kwargs
            )
        ]
        try:
            return method(instrument_id, **kwargs) if positional else method(**kwargs)
        except UnsupportedCapabilityError:
            raise
        except Exception as exc:
            raise UniverseProviderContractViolationError(
                "the task-13 instrument qualification port failed",
                details={
                    "instrument_id": str(instrument_id),
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def _evaluate_dynamic_candidate(
        self,
        row: object,
        spec: InstrumentSpec,
        query: UniverseQuery,
        precomputed_reasons: Sequence[str],
    ) -> tuple[bool, tuple[str, ...], object | None]:
        """Consume existing PIT qualification ports for one candidate.

        The chunk owns only request-bound identity/scope checks.  Coverage,
        corporate-action, quantity, and status evidence is supplied by the
        injected single-instrument qualification port; this method never
        scans the Bar or action tables itself.
        """

        from app.backtesting.data.universe import (
            CandidateEligibilityContext,
            evaluate_candidate,
        )

        request = self._session._request
        qualification_source = self._provider._coverage_qualification_provider
        method = None
        for name in (
            "qualify_instrument",
            "coverage_qualification",
            "qualify",
            "resolve_qualification",
        ):
            candidate = getattr(qualification_source, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None:
            raise UniverseCapabilityMissingError(
                "dynamic candidates require an explicit single-instrument qualification port",
                details={"instrument_id": str(spec.instrument_id)},
            )
        qualification_request = self._build_qualification_request(
            spec.instrument_id, query, spec
        )
        try:
            qualification = self._invoke_qualification_method(
                method, qualification_request
            )
        except (UniverseCapabilityMissingError, UnsupportedCapabilityError):
            raise UniverseCapabilityMissingError(
                "the configured qualification port cannot answer this PIT request",
                details={"instrument_id": str(spec.instrument_id)},
            )
        except Exception as exc:
            raise UniverseProviderContractViolationError(
                "the configured qualification port failed for a PIT candidate",
                details={
                    "instrument_id": str(spec.instrument_id),
                    "error_type": type(exc).__name__,
                },
            ) from exc
        if qualification is None:
            raise UniverseProviderContractViolationError(
                "the configured qualification port returned no result",
                details={"instrument_id": str(spec.instrument_id)},
            )
        result_id = (
            qualification.get("instrument_id", spec.instrument_id)
            if isinstance(qualification, Mapping)
            else getattr(qualification, "instrument_id", spec.instrument_id)
        )
        if result_id != spec.instrument_id:
            raise UniverseProviderContractViolationError(
                "qualification result instrument_id does not match the candidate",
                details={
                    "expected": str(spec.instrument_id),
                    "actual": str(result_id),
                },
            )
        instrument_qualification = None
        if self._provider._pit_spec_provider is not None:
            try:
                instrument_qualification = self._resolve_instrument_qualification(
                    spec.instrument_id, query
                )
            except UnsupportedCapabilityError:
                raise UniverseCapabilityMissingError(
                    "the task-13 instrument qualification port is unavailable",
                    details={"instrument_id": str(spec.instrument_id)},
                )
        qualification_results = tuple(
            value
            for value in (instrument_qualification, qualification)
            if value is not None
        )

        display = spec.display
        effective_at = datetime.combine(query.effective_date, time.min, tzinfo=UTC)
        identity_interval_valid = (
            spec.valid_from <= effective_at
            and (spec.valid_to is None or effective_at < spec.valid_to)
        )
        payload: dict[str, object] = {
            "spec": spec,
            "qualification": qualification,
            "coverage_qualification": qualification,
            "instrument_qualification": instrument_qualification,
            "reason_codes": tuple(precomputed_reasons)
            + tuple(
                reason
                for value in qualification_results
                for reason in self._qualification_result_reason_codes(value)
            ),
            "identity_evidence": {
                "complete": bool(
                    identity_interval_valid
                    and spec.calendar_id
                    and spec.asset_class
                    and spec.exchange
                    and spec.currency
                ),
                "calendar_id": spec.calendar_id,
            },
            "mapping_evidence": {
                "complete": all(
                    type(value) is str and value.strip()
                    for value in (
                        display.trading_code,
                        display.name,
                        display.display_name,
                    )
                )
            },
            "rule_evidence": {
                "valid": spec.rule_package_reference == query.rule,
                "rule_package_reference": spec.rule_package_reference,
            },
        }
        for name in (
            "calendar_id",
            "effective_date",
            "effective_at",
            "known_at",
            "eligible",
            "status",
            "identity_status",
            "mapping_status",
            "rule_status",
            "identity_evidence",
            "mapping_evidence",
            "rule_evidence",
            "market_data_evidence",
            "corporate_action_evidence",
            "quantity_action_coverage_evidence",
            "status_evidence",
        ):
            if name not in payload and hasattr(row, name):
                payload[name] = getattr(row, name)
        for source_result in qualification_results:
            payload.update(
                {
                    name: value
                    for name, value in self._qualification_evidence(
                        source_result,
                        qualification_request.required_capabilities,
                        spec,
                    ).items()
                    if name not in payload
                    or (
                        isinstance(payload[name], Mapping)
                        and not payload[name]
                    )
                }
            )
        context = CandidateEligibilityContext(
            effective_date=query.effective_date,
            data_cutoff=query.boundary.data_cutoff,
            market_scope=query.market_scope,
            universe_query_policy=query.universe_query_policy,
            rule_package_reference=query.rule,
            rule_exception_set_reference=query.rule_exception_set,
            qualification_policy_version=query.qualification_policy_version,
            resolved_calendar_ids=query.allowed_calendar_ids,
            scope_mode=query.scope_mode or request.instrument_scope_mode,
            required_capabilities=qualification_request.required_capabilities,
            requested_window=request.requested_window,
            query_boundary=query.boundary,
            universe_scope_snapshot_hash=query.universe_scope_snapshot_hash,
            fixed_authorized_instrument_ids=tuple(
                sorted(self._fixed_authorized_instrument_ids, key=str)
            ),
        )
        candidate_qualifier = getattr(qualification_source, "qualify_candidate", None)
        if callable(candidate_qualifier):
            try:
                parameters = inspect.signature(candidate_qualifier).parameters
                if "candidate" in parameters or "context" in parameters:
                    kwargs = {
                        name: value
                        for name, value in (
                            ("candidate", payload),
                            ("context", context),
                        )
                        if name in parameters
                    }
                    candidate_result = candidate_qualifier(**kwargs)
                else:
                    candidate_result = candidate_qualifier(payload, context)
            except Exception as exc:
                raise UniverseProviderContractViolationError(
                    "the configured candidate qualification provider failed",
                    details={"error_type": type(exc).__name__},
                ) from exc
            if candidate_result is None:
                raise UniverseProviderContractViolationError(
                    "the configured candidate qualification provider returned no result"
                )
            payload["candidate_qualification"] = candidate_result

        evaluation = evaluate_candidate(payload, context)
        combined_evidence: dict[str, Mapping[str, object]] = {}
        for source_result in qualification_results:
            for name, value in self._qualification_evidence(
                source_result,
                qualification_request.required_capabilities,
                spec,
            ).items():
                if name not in combined_evidence or not combined_evidence[name]:
                    combined_evidence[name] = value
        missing = self._required_qualification_reasons(
            qualification,
            combined_evidence,
            qualification_request,
            spec,
        )
        reasons = tuple(sorted(set(evaluation.reason_codes) | set(missing)))
        explicit_eligible = (
            qualification.get("eligible")
            if isinstance(qualification, Mapping)
            else getattr(qualification, "eligible", None)
        )
        instrument_eligible = (
            instrument_qualification.get("eligible")
            if isinstance(instrument_qualification, Mapping)
            else getattr(instrument_qualification, "eligible", None)
        )
        if (
            explicit_eligible is False or instrument_eligible is False
        ) and not reasons:
            reasons = ("candidate_qualification_unavailable",)
        return not reasons, reasons, evaluation

    def _record_universe_filter(
        self,
        row: object,
        reasons: Sequence[str],
        evaluation: object | None = None,
    ) -> None:
        """Accumulate stable candidate-level filter evidence."""

        for reason in reasons:
            self._universe_filter_reason_counts[reason] = (
                self._universe_filter_reason_counts.get(reason, 0) + 1
            )
        instrument_id = getattr(row, "instrument_id", None)
        spec = getattr(row, "spec", None)
        if instrument_id is None and spec is not None:
            instrument_id = getattr(spec, "instrument_id", None)
        record: dict[str, object] = {
            "instrument_id": str(instrument_id) if isinstance(instrument_id, UUID) else None,
            "reason_codes": tuple(reasons),
        }
        if evaluation is not None:
            for name in (
                "calendar_id",
                "failed_check",
                "expected",
                "actual",
                "evidence_summary",
                "qualification_hash",
                "required_status_dimensions",
            ):
                value = getattr(evaluation, name, None)
                if value is not None:
                    record[name] = value
        self._universe_filter_records.append(MappingProxyType(record))

    def instruments(self, query: InstrumentQuery) -> tuple[InstrumentSpec, ...]:
        """Resolve full specs for known identities valid at ``effective_at``."""

        self._guard_business_query("instruments")
        self._require_query_type(query, InstrumentQuery, "instruments")
        self._require_authorized_instruments(query.instrument_ids, "instruments")
        rows: list[InstrumentSpec] = []
        for instrument_id in query.instrument_ids:
            spec = self._universe_specs.get(instrument_id)
            if spec is None:
                spec = self._provider.resolve_pit_spec(
                    instrument_id,
                    effective_at=query.effective_at,
                    data_cutoff=query.boundary.data_cutoff,
                )
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
        """Return PIT-qualified candidates for the current decision step.

        The provider scans only immutable local facts.  Candidate-level
        incompleteness is accumulated in the filter summary and omitted from
        the result; malformed provider output or an unbound query remains a
        request-level contract error.  The selected rows are de-duplicated by
        stable ``instrument_id`` and returned in deterministic order.
        """

        self._guard_business_query("universe")
        self._require_query_type(query, UniverseQuery, "universe")
        request = self._session._request
        scope_mode = query.scope_mode or request.instrument_scope_mode
        # A static query reads only the already-admitted fixed set and does
        # not require a dynamic-universe fact token.  Dynamic/hybrid queries
        # must have declared UNIVERSE in the chunk token.
        if scope_mode is not InstrumentScopeMode.FIXED:
            self._require_declared_fact_type(DataCapability.UNIVERSE, "universe")
        self._validate_universe_query_boundary(query)

        if scope_mode is not InstrumentScopeMode.FIXED and not self._provider.supports_universe():
            raise UniverseCapabilityMissingError(
                "the memory provider cannot serve a dynamic PIT universe"
            )
        if query.effective_date > self._sessions[-1].session_date:
            raise UniversePitBoundaryViolationError(
                "universe effective_date is later than the current chunk",
                details={
                    "chunk_last_session_date": self._sessions[-1].session_date.isoformat(),
                    "effective_date": query.effective_date.isoformat(),
                },
            )

        cache_key = self._universe_query_cache_key(query)
        # UniverseQuery is frozen in the data contract.  A future provider may
        # carry a non-hashable extension field, so use a deterministic string
        # fallback rather than allowing cache internals to alter query errors.
        try:
            cached = self._universe_cache.get(query)
            cache_store_key = query
        except TypeError:
            cached = self._universe_cache.get(cache_key)
            cache_store_key = cache_key
        if cached is not None:
            self._step_candidate_authorized_instrument_ids = frozenset(
                spec.instrument_id
                for spec in cached
                if spec.instrument_id not in self._fixed_authorized_instrument_ids
            )
            self._session._step_candidate_authorized_instrument_ids = (
                self._step_candidate_authorized_instrument_ids
            )
            self._universe_last_query_hash = cache_key
            return cached

        # Each bound PIT query owns one audit summary.  Repeated calls hit the
        # immutable cache above; a new effective-date/cutoff query starts a new
        # summary instead of mixing counts from unrelated decision steps.
        self._step_candidate_authorized_instrument_ids = frozenset()
        self._session._step_candidate_authorized_instrument_ids = frozenset()
        self._universe_filter_reason_counts = {}
        self._universe_filter_records = []
        if scope_mode is InstrumentScopeMode.FIXED:
            # Fixed mode must not scan the dynamic source at all.  Read only
            # the identities admitted by the static request scope; mandatory
            # and opening-position ids stay independently authorized.
            source_rows = tuple(
                spec
                for instrument_id in request.static_instrument_ids
                if (
                    spec := self._provider.dataset.instrument_at(
                        instrument_id,
                        datetime.combine(
                            query.effective_date,
                            time.min,
                            tzinfo=UTC,
                        ),
                        query.boundary.data_cutoff,
                    )
                )
                is not None
            )
        else:
            dynamic_rows = self._provider._universe_source_rows(query)
            if scope_mode is InstrumentScopeMode.HYBRID:
                # Hybrid semantics are an explicit set union.  The dynamic
                # source is not required to echo static rows; fixed rows are
                # resolved from the run's frozen PIT source and then merged.
                fixed_rows = tuple(
                    spec
                    for instrument_id in request.static_instrument_ids
                    if (
                        spec := self._provider.dataset.instrument_at(
                            instrument_id,
                            datetime.combine(
                                query.effective_date,
                                time.min,
                                tzinfo=UTC,
                            ),
                            query.boundary.data_cutoff,
                        )
                    )
                    is not None
                )
                source_rows = fixed_rows + tuple(dynamic_rows)
            else:
                source_rows = tuple(dynamic_rows)
        # Sort before de-duplication so duplicate identity handling is
        # independent of source/database iteration order.  Malformed rows are
        # still rejected below rather than being hidden by the sort.
        keyed_rows = []
        for row in source_rows:
            candidate_id = getattr(row, "instrument_id", None)
            candidate_spec = row if isinstance(row, InstrumentSpec) else getattr(row, "spec", None)
            if candidate_id is None and isinstance(candidate_spec, InstrumentSpec):
                candidate_id = candidate_spec.instrument_id
            if candidate_id is None and isinstance(row, tuple) and len(row) == 2:
                left, right = row
                candidate_id = getattr(left, "instrument_id", None) or getattr(right, "instrument_id", None)
                if candidate_id is None:
                    candidate_spec = left if isinstance(left, InstrumentSpec) else right
                    candidate_id = getattr(candidate_spec, "instrument_id", None)
            if not isinstance(candidate_id, UUID):
                # Preserve the row for _extract_universe_candidate so its
                # provider-contract error remains the authoritative failure.
                keyed_rows.append(("", "", row))
                continue
            if not isinstance(candidate_spec, InstrumentSpec):
                try:
                    candidate_spec, _ = self._extract_universe_candidate(row)
                except UniverseProviderContractViolationError:
                    keyed_rows.append((str(candidate_id), type(row).__name__, row))
                    continue
            row_key = (
                self._stable_source_row_key(row, candidate_spec)
                if candidate_spec is not None
                else (type(row).__name__,)
            )
            effective_at = datetime.combine(query.effective_date, time.min, tzinfo=UTC)
            valid_from = getattr(candidate_spec, "valid_from", None)
            valid_to = getattr(candidate_spec, "valid_to", None)
            effective_match = bool(
                candidate_spec is not None
                and (valid_from is None or effective_at >= valid_from)
                and (valid_to is None or effective_at < valid_to)
            )
            known_at = getattr(row, "known_at", None)
            if known_at is None and candidate_spec is not None:
                known_at = getattr(candidate_spec, "known_at", None)
            known_match = True
            if known_at is not None:
                try:
                    if isinstance(known_at, str):
                        known_at = datetime.fromisoformat(known_at)
                    known_match = (
                        isinstance(known_at, datetime)
                        and known_at.tzinfo is not None
                        and known_at <= query.boundary.data_cutoff
                    )
                except (TypeError, ValueError):
                    known_match = False
            # Valid-at-date and known-before-cutoff versions win over rows
            # outside the requested PIT coordinate; the remaining fields are
            # a deterministic tie-break for overlapping/duplicate source rows.
            keyed_rows.append(
                (
                    str(candidate_id),
                    0 if effective_match else 1,
                    0 if known_match else 1,
                    *row_key,
                    row,
                )
            )
        keyed_rows.sort(key=lambda item: item[:-1])
        source_rows = tuple(item[-1] for item in keyed_rows)
        seen: set[UUID] = set()
        eligible: list[InstrumentSpec] = []
        for row in source_rows:
            spec, precomputed = self._extract_universe_candidate(row)
            if spec is None:
                # A qualification result without a complete spec is a
                # candidate-level failure.  Its reason codes remain auditable
                # and no placeholder spec is fabricated.
                self._record_universe_filter(
                    row, precomputed or ("candidate_ineligible",)
                )
                continue
            if spec.instrument_id in seen:
                # Code changes and duplicate provider rows must never create a
                # second identity.  Treat duplicate rows as candidate-level
                # evidence, retaining the first deterministic row only.
                self._record_universe_filter(spec, ("duplicate_instrument_id",))
                continue
            seen.add(spec.instrument_id)
            is_static_candidate = spec.instrument_id in request.static_instrument_ids
            is_fixed_only = (
                spec.instrument_id in self._fixed_authorized_instrument_ids
                and not is_static_candidate
            )
            if is_fixed_only:
                # Mandatory and non-zero opening-position identities are fixed
                # preflight subjects, not dynamic universe members.  They
                # remain readable as holdings through the fixed permission
                # layer but are never returned merely because the backing
                # fixture also contains their spec.
                self._record_universe_filter(spec, ("not_in_static_scope",))
                continue
            if scope_mode is InstrumentScopeMode.FIXED or (
                scope_mode is InstrumentScopeMode.HYBRID and is_static_candidate
            ):
                # Fixed ids have crossed the run-level complete preflight gate,
                # but their identity/display interval can still expire at a
                # later decision date.  Recheck only PIT identity, mapping,
                # scope, calendar, and rule references; fixed coverage facts
                # remain represented by the frozen preflight snapshot.
                reasons = self._candidate_filter_reasons(
                    spec, query, precomputed, row=row
                )
                evaluation = None
            else:
                reasons = self._candidate_filter_reasons(
                    spec, query, precomputed, row=row
                )
                if reasons:
                    eligible_flag, evaluation = False, None
                else:
                    eligible_flag, reasons, evaluation = self._evaluate_dynamic_candidate(
                        row, spec, query, precomputed
                    )
                if not eligible_flag and not reasons:
                    reasons = ("candidate_ineligible",)
            if reasons:
                self._record_universe_filter(spec, reasons, evaluation)
                continue
            # Fixed mode returns only the explicitly static candidate side;
            # mandatory/initial-position ids remain authorized independently
            # and are not silently turned into universe members.
            if scope_mode is InstrumentScopeMode.FIXED and not is_static_candidate:
                self._record_universe_filter(spec, ("not_in_static_scope",))
                continue
            eligible.append(spec)
            self._universe_specs[spec.instrument_id] = spec

        # In a hybrid run the source includes both static and dynamic rows;
        # all filtering above is still per-candidate, then stable identity
        # order defines the single merged result.
        result = tuple(sorted(eligible, key=lambda item: str(item.instrument_id)))
        self._universe_cache[cache_store_key] = result
        self._universe_last_query_hash = cache_key
        self._step_candidate_authorized_instrument_ids = frozenset(
            spec.instrument_id
            for spec in result
            if spec.instrument_id not in self._fixed_authorized_instrument_ids
        )
        self._session._step_candidate_authorized_instrument_ids = (
            self._step_candidate_authorized_instrument_ids
        )
        return result

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
        self._require_query_type(query, BarQuery, "bars")
        self._require_declared_fact_type(DataCapability.BARS, "bars")
        self._require_authorized_instruments(query.instrument_ids, "bars")
        if query.price_basis is not PriceBasis.RAW:
            raise UnsupportedCapabilityError(
                "the memory provider only serves raw bars",
                details={"price_basis": query.price_basis.value},
            )
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
            # The lookback's own end_at is a hard upper bound: the
            # include_cutoff_day proof covers the cutoff day only, so the
            # end day is admitted solely when it IS that proven cutoff
            # day -- never a later session beyond end_at.
            end_day_admitted = (
                boundary.include_cutoff_day
                and last_eligible_day == boundary.cutoff_date
            )
            eligible = [
                point
                for point in pool
                if point.session_date < last_eligible_day
                or (end_day_admitted and point.session_date == last_eligible_day)
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
        self._require_query_type(query, CoverageQuery, "coverage")
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
        known_at = bar.evidence.known_at
        if boundary.knowledge_as_of is not None:
            if known_at is None:
                raise ProviderContractViolationError(
                    "strict historical cognition requires bar known_at evidence",
                    details={
                        **details,
                        "knowledge_as_of": boundary.knowledge_as_of.isoformat(),
                        "reason_code": "strict_pit_evidence_missing",
                    },
                )
            if known_at > boundary.knowledge_as_of:
                raise ProviderContractViolationError(
                    "provider returned a bar learned after the knowledge_as_of boundary",
                    details={
                        **details,
                        "known_at": known_at.isoformat(),
                        "knowledge_as_of": boundary.knowledge_as_of.isoformat(),
                        "reason_code": "knowledge_as_of_exceeded",
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
