"""Read-only ETF data adapter for the generic backtesting data contract.

The adapter projects the existing ETF ingestion tables (``etf_daily_bars``,
``etf_adjustment_factors``, ``instrument_code_mappings``,
``trading_calendar_days``) onto the generic fact objects
(:class:`Bar`, :class:`AdjustedSeriesPoint`, evidenced code mappings) and
serves them through the production PIT read path.

Hard boundaries implemented here:

* the raw tables stay the single source of truth: nothing is rewritten,
  repaired, or back-filled; zero/negative prices and illegal OHLC rows are
  projected as ``invalid`` facts and keep their raw values;
* history is reachable only through stable ``instrument_id`` plus
  evidenced PIT mappings; the current ``EtfCode.etf_id`` association is
  never used to stitch cross-code history;
* daily bars carry no reliable source ``known_at``, so they are declared
  ``non_strict`` PIT and their ``updated_at`` is used only as an
  observation/revision marker, never as knowledge-time evidence;
* adjustment factors follow the approved first-version contract
  ``tushare_adj_factor_native@1``: positive factors only, selected by
  ``effective_date <= data_cutoff``, served only when the policy has been
  verified and activated, never with fabricated revisions;
* this module imports no network client of any kind: backtest reads can
  never trigger a Tushare call.

Integration boundary (task 03-08C): the adapter, its preflight summary,
and :func:`build_data_preflight_payloads` feed the existing
``backtest_data_preflight`` record and ``/data-preflight`` API.  The
persistence chain is exercised end to end at the repository level
(see ``tests/test_etf_data_adapter.py::PersistenceChainTestCase``);
automatic invocation inside a live backtest run lands together with the
formal provider registration, which this task package deliberately
excludes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import inspect
import re
from typing import Callable, Mapping, Protocol, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import CalendarSnapshot, SessionPoint, normalize_calendar_id
from app.backtesting.data.errors import (
    HistoryIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingIncompleteError,
    InstrumentCalendarUnresolvedError,
    InvalidDataRequestError,
    ProviderContractViolationError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.adjustment_policy import (
    ADJUSTMENT_ADAPTER_VERSION,
    ADJUSTMENT_SERIES_POLICY_KEY,
    ADJUSTMENT_SERIES_POLICY_VERSION,
    AdjustmentSeriesPolicy,
    INACTIVE_ADJUSTMENT_POLICY,
)
from app.backtesting.data.facts import (
    AdjustedSeriesPoint,
    Bar,
    FactEvidence,
    CorporateAction,
    DataCoverageFact,
    TradingStatus,
)
from app.backtesting.data.etf_adjustment import (
    build_research_price_series,
    cutoff_local_date,
    normalize_adjustment_factor,
)
from app.backtesting.data.pit_history import (
    PITMappingResolution,
    SegmentedAdjustedSeries,
    SegmentedBarHistory,
    resolve_pit_mappings,
    read_segmented_adjusted_series,
    read_segmented_history,
)
from app.backtesting.data.reports import (
    DataCoverageReport,
    PreflightIssue,
    build_trading_status_summary,
    canonical_hash,
)
from app.backtesting.data.requests import (
    DataCapability,
    DateRange,
    IssueSeverity,
    LookbackWindow,
    PriceBasis,
    QualityStatus,
    QueryBoundary,
    TradingStatusQuery,
    CoverageQualificationRequest,
    ContractRef,
    InternalFixture,
    INTERNAL_LINK_ACCEPTANCE_PROFILE,
    INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY,
    PreflightProfile,
    PreflightProfileRegistry,
)
from app.backtesting.data.protocols import InstrumentCoverageQualification
from app.backtesting.domain import _aware_datetime
from app.instruments.domain import (
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentSpec,
    InstrumentSpecProvider,
    MappingConflictError,
    MappingCoverageGapError,
)

__all__ = [
    "ADJUSTMENT_SERIES_POLICY",
    "ADJUSTMENT_SERIES_POLICY_KEY",
    "ADJUSTMENT_SERIES_POLICY_VERSION",
    "AdjustmentSeriesPolicy",
    "INACTIVE_ADJUSTMENT_POLICY",
    "ETF_ADAPTER_KEY",
    "ETF_ADAPTER_VERSION",
    "ETF_VALIDATION_RULE_KEY",
    "ETF_VALIDATION_RULE_VERSION",
    "ETF_PROVIDER_KEY",
    "ETF_RULE_PACKAGE",
    "EtfFactsAdapter",
    "CoverageQualificationRequest",
    "InstrumentCoverageQualification",
    "build_data_preflight_payloads",
]


ETF_PROVIDER_KEY = "etf_ingestion"
"""Stable provider key declared by the ETF ingestion data foundation."""

# These identifiers are intentionally owned by the adapter rather than the
# engine.  Persisting the identifiers in preflight evidence makes a replay
# auditable when the ETF-specific legality rules evolve.
ETF_ADAPTER_KEY = "etf_raw_bar_adapter"
ETF_ADAPTER_VERSION = "etf_raw_bar_adapter@1"
ETF_VALIDATION_RULE_KEY = "etf_raw_bar_validation"
ETF_VALIDATION_RULE_VERSION = "etf_raw_bar_validation@1"

ETF_RULE_PACKAGE = ("china_listed_etf_rules", 1)
"""Versioned rule package identifier used by the instrument domain."""

ADJUSTMENT_SERIES_POLICY = ("tushare_adj_factor_native", 1)
"""First-version adjustment-series policy (key, version)."""

_TRADING_STATUS_FIXTURE_CAPABILITY = "trading_status"
"""The internal fixture capability reserved for explicit STATUS requests."""


def _capability_value(value: object) -> str:
    """Return the stable string value of an enum or request capability."""

    return str(getattr(value, "value", value))


def _status_requested(required_capabilities: Sequence[object]) -> bool:
    """Whether the frozen request explicitly consumes STATUS evidence."""

    return any(
        _capability_value(item) == DataCapability.STATUS.value
        for item in required_capabilities
    )


def _fixtures_for_capabilities(
    fixtures: Sequence[InternalFixture],
    required_capabilities: Sequence[object],
) -> tuple[InternalFixture, ...]:
    """Keep only fixture evidence consumed by the current capability request.

    The internal profile may carry a broad fixture bundle for several task
    packages.  A trading-status fixture is meaningful only when STATUS is a
    required capability; retaining it on a bars-only ETF qualification would
    make an unconsumed substitute affect profile gates and qualification
    hashes.  Other fixture families keep their existing behavior.
    """

    if _status_requested(required_capabilities):
        return tuple(fixtures)
    return tuple(
        fixture
        for fixture in fixtures
        if _capability_value(getattr(fixture, "capability", None))
        != _TRADING_STATUS_FIXTURE_CAPABILITY
    )


def _summary_hash_content(value: object) -> object:
    """Remove presentation and run metadata from ETF summary hash input."""

    if isinstance(value, Mapping):
        return {
            key: _summary_hash_content(item)
            for key, item in value.items()
            if key not in {"generated_at", "run_id", "message", "title", "limitation"}
        }
    if isinstance(value, (list, tuple)):
        return [_summary_hash_content(item) for item in value]
    return value

_SENSITIVE_KEY_RE = re.compile(
    r"(?:access[_-]?token|api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:token|password|secret|api[_-]?key|authorization)\s*[:=]",
    re.IGNORECASE,
)


def _redact_sensitive(value: object) -> object:
    """Remove credential-shaped data before it reaches preflight/hash output.

    Adapter summaries are assembled from provider diagnostics and caller
    supplied issue details.  They are machine-readable evidence, not a place
    to persist credentials, so redact by key and by the common ``key=value``
    value form before serializing or hashing the summary.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _redact_sensitive(item)
            for key, item in value.items()
            if not (isinstance(key, str) and _SENSITIVE_KEY_RE.search(key))
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value):
        return "[redacted]"
    return value

# ---------------------------------------------------------------------------
# Read-only ports (thin wrappers over the ingestion query repositories)
# ---------------------------------------------------------------------------


class CodeMappingsPort(Protocol):
    """Resolves evidenced PIT mappings for one instrument/source pair."""

    def __call__(
        self,
        instrument_id: UUID,
        *,
        source: str,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> Sequence[InstrumentCodeMapping]:
        ...


class DailyBarsPort(Protocol):
    """Reads stored daily bars keyed by one source code."""

    def __call__(
        self, ts_code: str, start_date: date | None, end_date: date | None
    ) -> Sequence[object]:
        ...


class AdjustmentFactorsPort(Protocol):
    """Reads stored adjustment factors keyed by one source code."""

    def __call__(
        self, ts_code: str, start_date: date | None, end_date: date | None
    ) -> Sequence[object]:
        ...


class TradingDaysPort(Protocol):
    """Reads open trading days for one exchange inside a window."""

    def __call__(self, exchange: str, start_date: date, end_date: date) -> list[date]:
        ...


class TradingStatusFactsPort(Protocol):
    """Reads PIT-filtered normalized trading-status rows."""

    def __call__(
        self,
        instrument_ids: Sequence[UUID],
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
        knowledge_as_of: datetime | None = None,
    ) -> Sequence[object]:
        ...


class TradingStatusCoveragePort(Protocol):
    """Reads PIT-filtered status coverage proofs."""

    def __call__(
        self,
        instrument_ids: Sequence[UUID],
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
        knowledge_as_of: datetime | None = None,
    ) -> Sequence[object]:
        ...


# ---------------------------------------------------------------------------
# Source row shapes (structural; real ORM rows satisfy these)
# ---------------------------------------------------------------------------


class DailyBarRow(Protocol):
    """Structural view of one ``etf_daily_bars`` row."""

    ts_code: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    vol: Decimal | None
    amount: Decimal | None
    updated_at: datetime
    source_revision: str | None


class AdjustmentFactorRow(Protocol):
    """Structural view of one ``etf_adjustment_factors`` row."""

    ts_code: str
    trade_date: date
    adj_factor: Decimal
    updated_at: datetime


class TradingStatusRow(Protocol):
    """Structural view of one normalized status fact."""

    ts_code: str
    trade_date: date
    status: str
    dimension: str
    valid_from: date | None
    valid_to: date | None
    source: str
    source_revision: str | None
    quality_status: str
    known_at: datetime | None
    observed_at: datetime


def _decimal_or_none(value: object) -> Decimal | None:
    """Read a finite source value for validation without changing its value."""

    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        # ``project_bar`` will reject these at the generic fact boundary.  A
        # validation issue still gives preflight a useful, JSON-safe reason.
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _raw_value(value: object) -> str | None:
    """Serialize one raw value without introducing binary floating point."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _bar_validation_issues(
    row: DailyBarRow,
    *,
    instrument_id: UUID | None = None,
    source: str = ETF_PROVIDER_KEY,
    source_code: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Return one JSON-safe issue per failed ETF v1 OHLC rule.

    This function only observes source values.  It never repairs, drops, or
    replaces an invalid value; callers may therefore retain the raw row for
    an auditable preflight report while withholding it from execution.
    """

    code = source_code if source_code is not None else getattr(row, "ts_code", None)
    common: dict[str, object] = {
        "instrument_id": str(instrument_id) if instrument_id is not None else None,
        "trade_date": (
            row.trade_date.isoformat()
            if isinstance(getattr(row, "trade_date", None), date)
            else None
        ),
        "source": source,
        "source_code": code,
        "rule_key": ETF_VALIDATION_RULE_KEY,
        "rule_version": ETF_VALIDATION_RULE_VERSION,
        "adapter_key": ETF_ADAPTER_KEY,
        "adapter_version": ETF_ADAPTER_VERSION,
    }
    issues: list[dict[str, object]] = []

    def add(field: str, raw: object, reason: str, *, code_name: str) -> None:
        issues.append(
            {
                **common,
                "field": field,
                "raw_value": _raw_value(raw),
                "reason": reason,
                "code": code_name,
            }
        )

    values = {
        field: getattr(row, field, None)
        for field in ("open", "high", "low", "close")
    }
    parsed = {field: _decimal_or_none(value) for field, value in values.items()}
    for field, value in values.items():
        if value is None:
            add(field, value, "missing_ohlc_field", code_name="bar_field_missing")
            continue
        numeric = parsed[field]
        if numeric is None:
            add(field, value, "invalid_decimal", code_name="bar_invalid")
        elif numeric <= 0:
            add(field, value, "non_positive_price", code_name="bar_invalid")

    high, low = parsed["high"], parsed["low"]
    if high is not None and low is not None and high < low:
        add("high", values["high"], "high_below_low", code_name="bar_invalid")
    for field in ("open", "close"):
        value = parsed[field]
        if (
            value is not None
            and value > 0
            and low is not None
            and high is not None
            and low <= high
            and not (low <= value <= high)
        ):
            add(field, values[field], "price_outside_low_high", code_name="bar_invalid")
    return tuple(issues)


def _bar_quality(row: DailyBarRow) -> QualityStatus:
    """Classify a raw bar row without repairing anything.

    A row is consumable (``complete``) only when every OHLC price is strictly
    positive and satisfies the ETF range rules.  Volume and amount have no
    business-validity rule in this task and are therefore preserved as-is.
    Everything else stays an explicit ``invalid`` fact with its original
    values so coverage and preflight can block on it.
    """

    return (
        QualityStatus.INVALID
        if _bar_validation_issues(row)
        else QualityStatus.COMPLETE
    )


@dataclass(frozen=True, slots=True)
class EtfFactsAdapter:
    """Read-only projection of stored ETF facts onto the data contract.

    Every dependency is an injected read-only callable, so the adapter can
    never reach past the fact tables into a live external source.  The
    adapter is immutable: ``adjustment_active`` and its verification
    evidence are fixed at construction and cannot be flipped at runtime.
    """

    code_mappings: CodeMappingsPort
    daily_bars: DailyBarsPort
    adjustment_factors: AdjustmentFactorsPort
    trading_days: TradingDaysPort
    source: str = "tushare"
    adjustment_active: bool = False
    adjustment_verification_evidence: str | Mapping[str, object] | None = None
    # Policy-backed activation is the production path.  The legacy boolean
    # fields remain in the constructor only to keep old call sites readable;
    # ``__post_init__`` maps them to this fixed descriptor and rejects weak
    # evidence instead of allowing a boolean bypass.
    adjustment_policy: AdjustmentSeriesPolicy | None = None
    adjustment_verification_artifact: object | None = None
    # Task-11 canonical identity hook: the resolver must receive the
    # effective day and PIT cutoff so it can return
    # InstrumentIdentityFact.calendar_id.  There is no adapter-level
    # exchange/calendar fallback.
    calendar_id_resolver: Callable[..., str | None] | None = None
    # Trading rules are resolved by the instrument domain.  Keeping this
    # port optional preserves the adapter's read-only bar APIs while making
    # an ETF spec impossible to fabricate from the ingestion directory.
    spec_provider: InstrumentSpecProvider | None = None
    # Descriptive alias for callers that prefer the protocol's full name.
    instrument_spec_provider: InstrumentSpecProvider | None = None
    # ETF factors are dated in the market's local calendar.  Keeping this
    # explicit prevents a UTC date from changing the visible factor set.
    market_timezone: str = "Asia/Shanghai"
    # Internal substitutes are never generated by the adapter.  Callers must
    # inject an explicit, bounded ``InternalFixture`` when the internal-link
    # profile needs one.
    fixtures: tuple[InternalFixture, ...] = ()
    internal_fixtures: tuple[InternalFixture, ...] = ()
    corporate_action_repository: object | None = None
    trading_status_facts: TradingStatusFactsPort | None = None
    trading_status_coverage: TradingStatusCoveragePort | None = None

    def __post_init__(self) -> None:
        if self.calendar_id_resolver is not None and not callable(self.calendar_id_resolver):
            raise InvalidDataRequestError("calendar_id_resolver must be callable")
        providers = tuple(
            provider
            for provider in (self.spec_provider, self.instrument_spec_provider)
            if provider is not None
        )
        if len(providers) == 2 and providers[0] is not providers[1]:
            raise InvalidDataRequestError(
                "spec_provider and instrument_spec_provider must refer to one provider"
            )
        for provider in providers:
            if not callable(getattr(provider, "resolve_spec", None)):
                raise InvalidDataRequestError(
                    "spec provider must expose a callable resolve_spec method"
                )
        if self.corporate_action_repository is not None and not callable(getattr(self.corporate_action_repository, "list_facts", None)):
            raise InvalidDataRequestError("corporate_action_repository must expose list_facts")
        for name in ("trading_status_facts", "trading_status_coverage"):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise InvalidDataRequestError(f"{name} must be callable when provided")
        if not isinstance(self.market_timezone, str) or not self.market_timezone.strip():
            raise InvalidDataRequestError("market_timezone must be non-blank text")
        object.__setattr__(self, "market_timezone", self.market_timezone.strip())
        configured_fixtures = tuple(self.fixtures)
        configured_internal_fixtures = tuple(self.internal_fixtures)
        fixtures = configured_fixtures + configured_internal_fixtures
        if any(not isinstance(item, InternalFixture) for item in fixtures):
            raise InvalidDataRequestError(
                "fixtures entries must be InternalFixture instances"
            )
        unique_fixtures: dict[tuple[str, object, str], InternalFixture] = {}
        for fixture in fixtures:
            key = (fixture.fixture_key, fixture.fixture_version, fixture.capability)
            if key in unique_fixtures and unique_fixtures[key] != fixture:
                raise InvalidDataRequestError(
                    "duplicate internal fixture key/version/capability"
                )
            unique_fixtures[key] = fixture
        fixtures = tuple(unique_fixtures.values())
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
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "internal_fixtures",
            tuple(
                sorted(
                    configured_internal_fixtures,
                    key=lambda item: (
                        item.capability,
                        item.fixture_key,
                        str(item.fixture_version),
                    ),
                )
            ),
        )
        policy = self.adjustment_policy
        artifact = self.adjustment_verification_artifact
        if policy is None and artifact is not None:
            policy = AdjustmentSeriesPolicy.from_verification_artifact(artifact)
            object.__setattr__(self, "adjustment_policy", policy)
        if policy is None:
            if self.adjustment_active:
                # A free-form string is not verification evidence.  A
                # structured artifact is accepted as a compatibility path,
                # but it still goes through the immutable policy validator.
                evidence = self.adjustment_verification_evidence
                if isinstance(evidence, Mapping):
                    policy = AdjustmentSeriesPolicy.from_verification_artifact(evidence)
                    object.__setattr__(self, "adjustment_policy", policy)
                    object.__setattr__(
                        self,
                        "adjustment_verification_evidence",
                        policy.verification_summary,
                    )
                else:
                    raise InvalidDataRequestError(
                        "adjustment_active=True is not an activation mechanism; "
                        "pass a verified AdjustmentSeriesPolicy"
                    )
            else:
                policy = INACTIVE_ADJUSTMENT_POLICY
                object.__setattr__(self, "adjustment_policy", policy)
        elif not isinstance(policy, AdjustmentSeriesPolicy):
            raise InvalidDataRequestError(
                "adjustment_policy must be an AdjustmentSeriesPolicy"
            )
        if policy.key != ADJUSTMENT_SERIES_POLICY_KEY or policy.version != ADJUSTMENT_SERIES_POLICY_VERSION:
            raise InvalidDataRequestError(
                "only tushare_adj_factor_native@1 is registered for ETF adjustments"
            )
        if policy.adapter_version != ADJUSTMENT_ADAPTER_VERSION:
            raise InvalidDataRequestError(
                "adjustment_policy adapter_version does not match the ETF adapter"
            )
        if self.adjustment_active and not policy.is_active():
            raise InvalidDataRequestError(
                "adjustment_active=True cannot activate an inactive policy"
            )
        # The descriptor is the single source of truth.  A default/legacy
        # ``False`` value must not make an explicitly supplied active policy
        # unusable, while a legacy ``True`` can never promote an inactive one.
        object.__setattr__(self, "adjustment_active", policy.is_active())
        if policy.is_active():
            policy.validate_activation()
            if (
                self.adjustment_verification_evidence is not None
                and self.adjustment_verification_evidence
                != policy.verification_summary
            ):
                raise InvalidDataRequestError(
                    "legacy verification evidence does not match adjustment_policy"
                )
        elif self.adjustment_verification_evidence is not None:
            if not isinstance(self.adjustment_verification_evidence, str) or not self.adjustment_verification_evidence.strip():
                raise InvalidDataRequestError(
                    "adjustment_verification_evidence must be blank when policy is inactive"
                )

    # ------------------------------------------------------------------
    # Identity mappings
    # ------------------------------------------------------------------

    def resolve_mappings(
        self,
        instrument_id: UUID,
        *,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> tuple[InstrumentCodeMapping, ...]:
        """Resolve visible mappings, converting DB errors to stable codes.

        The instruments repository raises domain-layer
        ``MappingCoverageGapError`` / ``MappingConflictError``; crossing
        the provider boundary those become the stable data-contract codes
        ``identity_mapping_incomplete`` / ``identity_mapping_conflict``
        so callers never parse exception text.
        """

        try:
            return tuple(
                self.code_mappings(
                    instrument_id,
                    source=self.source,
                    start_date=start_date,
                    end_date=end_date,
                    data_cutoff=data_cutoff,
                )
            )
        except MappingCoverageGapError as exc:
            raise IdentityMappingIncompleteError(
                "no complete PIT code mapping covers the requested window",
                details={
                    "instrument_id": str(instrument_id),
                    "source": self.source,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "reason": str(exc),
                },
            ) from exc
        except MappingConflictError as exc:
            raise IdentityMappingConflictError(
                "PIT code mappings overlap inside the requested window",
                details={
                    "instrument_id": str(instrument_id),
                    "source": self.source,
                    "reason": str(exc),
                },
            ) from exc

    # ------------------------------------------------------------------
    # Instrument display and spec projection
    # ------------------------------------------------------------------

    def project_display(self, row: object) -> InstrumentDisplay | None:
        """Project one ``etf_codes`` row onto the generic display object.

        Display fields stay ``None`` when the source does not provide
        them; only the stable identity is mandatory.  A row without its
        entity binding (``etf_id``) has no stable identity to project and
        yields ``None`` instead of a fabricated one.
        """

        entity_id = getattr(row, "etf_id", None)
        if not isinstance(entity_id, UUID):
            return None
        ts_code = getattr(row, "ts_code", None)
        trading_code = (
            ts_code.split(".", 1)[0]
            if isinstance(ts_code, str) and "." in ts_code
            else ts_code
        )
        return InstrumentDisplay(
            instrument_id=entity_id,
            trading_code=trading_code if isinstance(trading_code, str) else None,
            name=getattr(row, "cname", None),
            display_name=getattr(row, "csname", None),
        )

    def _identity_calendar_id(
        self,
        row: object,
        instrument_id: UUID,
        *,
        effective_date: date,
        data_cutoff: datetime | None,
    ) -> str | None:
        """Read one PIT calendar from an identity fact or strict resolver.

        A plain ``row.calendar_id`` attribute is intentionally not accepted:
        an ETF code row is not an identity fact and does not carry the
        effective-day/PIT evidence required by task 11.  Callers may provide
        an already-resolved ``identity_fact`` sidecar, or production wiring
        may inject a resolver with the explicit identity/PIT arguments.
        """

        # Identity resolution always uses the caller's frozen PIT cutoff;
        # wall-clock fallbacks would make a replay non-deterministic.
        cutoff = data_cutoff
        if cutoff is None:
            return None
        try:
            cutoff = _aware_datetime(cutoff, "data_cutoff")
        except Exception as exc:
            raise InstrumentCalendarUnresolvedError(
                "ETF calendar resolution requires an aware data_cutoff",
                details={"instrument_id": str(instrument_id)},
            ) from exc

        identity_fact = getattr(row, "identity_fact", None)
        if identity_fact is not None:
            if getattr(identity_fact, "instrument_id", instrument_id) != instrument_id:
                raise InstrumentCalendarUnresolvedError(
                    "ETF identity fact does not belong to the requested instrument",
                    details={"instrument_id": str(instrument_id)},
                )
            valid_from = getattr(identity_fact, "valid_from", None)
            valid_to = getattr(identity_fact, "valid_to", None)
            known_at = getattr(identity_fact, "known_at", None)
            if (
                not isinstance(valid_from, date)
                or isinstance(valid_from, datetime)
                or (valid_to is not None and (not isinstance(valid_to, date) or isinstance(valid_to, datetime)))
                or not isinstance(known_at, datetime)
                or known_at.tzinfo is None
                or known_at.utcoffset() is None
                or known_at > cutoff
                or effective_date < valid_from
                or (valid_to is not None and effective_date >= valid_to)
            ):
                raise InstrumentCalendarUnresolvedError(
                    "ETF identity fact is not visible for the requested effective day and PIT cutoff",
                    details={
                        "instrument_id": str(instrument_id),
                        "effective_date": effective_date.isoformat(),
                        "data_cutoff": cutoff.isoformat(),
                    },
                )
            calendar_id = getattr(identity_fact, "calendar_id", None)
        elif self.calendar_id_resolver is not None:
            try:
                calendar_id = self.calendar_id_resolver(
                    instrument_id,
                    effective_date=effective_date,
                    data_cutoff=cutoff,
                )
            except TypeError as exc:
                raise InstrumentCalendarUnresolvedError(
                    "ETF calendar resolver must accept effective_date and data_cutoff",
                    details={"instrument_id": str(instrument_id)},
                ) from exc
        else:
            return None
        if not isinstance(calendar_id, str) or not calendar_id.strip():
            return None
        try:
            return normalize_calendar_id(calendar_id)
        except Exception as exc:
            raise InstrumentCalendarUnresolvedError(
                "ETF identity fact returned an invalid calendar_id",
                details={
                    "instrument_id": str(instrument_id),
                    "calendar_id": calendar_id,
                },
            ) from exc

    def project_instrument_spec(
        self,
        row: object,
        *,
        effective_date: date | None = None,
        data_cutoff: datetime | None = None,
    ) -> InstrumentSpec | None:
        """Project one ``etf_codes`` row onto a complete engine spec.

        The ingestion directory only supplies a stable identity/display
        candidate.  A complete spec must come from the injected instrument
        domain provider, which resolves PIT identity and versioned rule facts;
        no adapter-level trading, currency, exchange, calendar, session, or
        capability defaults are permitted.  The validity window starts
        strictly at ``list_date``: falling back to ``setup_date`` would admit
        funds that exist but are not listed yet.
        """

        display = self.project_display(row)
        if display is None:
            return None
        list_date = getattr(row, "list_date", None)
        if effective_date is None:
            effective_date = list_date
        if not isinstance(effective_date, date) or isinstance(effective_date, datetime):
            return None
        provider = self.spec_provider or self.instrument_spec_provider
        if provider is None:
            # The directory row is not a rules fact.  Without the domain
            # provider there is no safe projection, so fail closed with the
            # stable calendar/spec-resolution contract error.  Rows that do
            # not even expose the legacy exchange marker remain unresolvable
            # ``None`` for compatibility with display-only callers.
            if not isinstance(getattr(row, "exchange", None), str) or not getattr(
                row, "exchange", ""
            ).strip():
                return None
            raise InstrumentCalendarUnresolvedError(
                "ETF instrument spec requires an InstrumentSpecProvider",
                details={
                    "instrument_id": str(display.instrument_id),
                    "ts_code": getattr(row, "ts_code", None),
                },
            )
        if data_cutoff is None:
            raise InvalidDataRequestError(
                "data_cutoff is required for point-in-time ETF spec resolution"
            )
        try:
            cutoff = _aware_datetime(data_cutoff, "data_cutoff")
        except Exception as exc:
            raise InvalidDataRequestError(
                "data_cutoff must be a timezone-aware datetime"
            ) from exc
        effective_at = datetime.combine(effective_date, datetime.min.time(), tzinfo=timezone.utc)
        spec = provider.resolve_spec(
            display.instrument_id,
            effective_at=effective_at,
            data_cutoff=cutoff,
        )
        if spec is None:
            return None
        if not isinstance(spec, InstrumentSpec):
            raise ProviderContractViolationError(
                "instrument spec provider returned a non-InstrumentSpec value",
                details={"instrument_id": str(display.instrument_id)},
            )
        if spec.instrument_id != display.instrument_id:
            raise ProviderContractViolationError(
                "instrument spec provider returned another instrument identity",
                details={
                    "requested_instrument_id": str(display.instrument_id),
                    "returned_instrument_id": str(spec.instrument_id),
                },
            )
        return spec

    # ------------------------------------------------------------------
    # Bar projection and segmented history
    # ------------------------------------------------------------------

    def project_bar(self, row: DailyBarRow, instrument_id: UUID) -> Bar:
        """Project one raw ``etf_daily_bars`` row onto a generic ``Bar``.

        ``price_basis`` is always ``raw``, frequency ``1d``.  ``known_at``
        stays ``None`` because the table has no reliable knowledge-time
        column; ``updated_at`` is carried as ``observed_at`` only.
        """

        quality = _bar_quality(row)
        source_code = getattr(row, "ts_code", None)
        missing_reasons = {
            ("volume" if field == "vol" else field): "source_field_missing"
            for field in ("open", "high", "low", "close", "vol", "amount")
            if getattr(row, field, None) is None
        }
        return Bar(
            instrument_id=instrument_id,
            trade_date=row.trade_date,
            frequency="1d",
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.vol,
            amount=row.amount,
            price_basis=PriceBasis.RAW,
            evidence=FactEvidence(
                source=self.source,
                observed_at=row.updated_at,
                quality_status=quality,
                known_at=None,
                source_revision=(
                    str(row.source_revision)
                    if getattr(row, "source_revision", None) is not None
                    else None
                ),
            ),
            validation_rule_version=ETF_VALIDATION_RULE_VERSION,
            attributes={
                "source_code": source_code,
                "field_units": {
                    "open": "CNY",
                    "high": "CNY",
                    "low": "CNY",
                    "close": "CNY",
                    "volume": "lot",
                    "amount": "thousand_CNY",
                },
                "missing_reasons": missing_reasons,
                "adapter_key": ETF_ADAPTER_KEY,
                "adapter_version": ETF_ADAPTER_VERSION,
                "validation_rule_version": ETF_VALIDATION_RULE_VERSION,
            },
        )

    def validate_bar(
        self,
        row: DailyBarRow | Bar,
        instrument_id: UUID | None = None,
        *,
        source_code: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Return JSON-safe ETF v1 legality issues for one raw/projection row."""

        if isinstance(row, Bar):
            instrument_id = instrument_id or row.instrument_id
            source = row.evidence.source
            source_code = source_code or getattr(row, "attributes", {}).get("source_code")
        else:
            source = self.source
        return _bar_validation_issues(
            row,
            instrument_id=instrument_id,
            source=source,
            source_code=source_code,
        )

    @staticmethod
    def require_row_code(row: object, requested_source_code: str) -> str:
        """Verify a stored row belongs to the requested source code.

        The generic ``Bar``/``AdjustedSeriesPoint`` envelopes no longer
        carry the source code, so a repository bug that returns another
        code's rows would be invisible after projection and could poison
        one identity's history with another's facts.  The check runs
        before projection and blocks with a stable provider-contract code.
        """

        row_code = getattr(row, "ts_code", None)
        if not isinstance(row_code, str) or row_code != requested_source_code:
            raise ProviderContractViolationError(
                "the repository returned a row keyed by another source "
                "code than the requested PIT segment",
                details={
                    "requested_source_code": requested_source_code,
                    "returned_source_code": (
                        row_code if isinstance(row_code, str) else None
                    ),
                },
            )
        return row_code

    def _segment_bar_reader(self, instrument_id: UUID):
        """Build the per-segment reader over the stored daily-bar table."""

        adapter = self

        class _Reader:
            def read_bars(self, source_code: str, start_date: date, end_date: date):
                projected = []
                for row in adapter.daily_bars(source_code, start_date, end_date):
                    adapter.require_row_code(row, source_code)
                    if start_date <= row.trade_date <= end_date:
                        projected.append(adapter.project_bar(row, instrument_id))
                return projected

        return _Reader()

    def resolve(
        self,
        instrument_id: UUID,
        *,
        sessions: Sequence[date],
        data_cutoff: datetime,
    ) -> PITMappingResolution:
        """Bind every requested session to exactly one evidenced code."""

        if not sessions:
            raise HistoryIncompleteError(
                "the requested window contains no trading sessions"
            )
        mappings = self.resolve_mappings(
            instrument_id,
            start_date=min(sessions),
            end_date=max(sessions),
            data_cutoff=data_cutoff,
        )
        return resolve_pit_mappings(
            instrument_id,
            source=self.source,
            sessions=sessions,
            mappings=mappings,
            data_cutoff=data_cutoff,
            session_cutoff_date=cutoff_local_date(data_cutoff, self.market_timezone),
        )

    def bars(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
    ) -> SegmentedBarHistory:
        """Read and stitch one stable-identity bar series by mapping.

        ETF daily bars keep no reliable source ``known_at``, so the read
        runs in non-strict fact mode: bars without knowledge-time evidence
        are served as latest authoritative revisions, while any bar whose
        ``known_at`` lands after ``data_cutoff`` still blocks.
        """

        return read_segmented_history(
            resolution,
            self._segment_bar_reader(instrument_id),
            allow_non_strict_facts=True,
        )

    def preflight_bars(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
    ) -> dict[str, object]:
        """Read raw bars for admission checks, retaining invalid facts.

        ``bars()`` deliberately applies the generic complete-quality gate
        before formal consumption.  Admission preflight needs the opposite
        view: an invalid source row must be visible as evidence, not silently
        look like a missing row.  This method therefore performs only the
        structural checks needed to build a report and never returns a row to
        strategy code.
        """

        if resolution.instrument_id != instrument_id:
            raise ProviderContractViolationError(
                "bar preflight resolution belongs to another instrument",
                details={
                    "instrument_id": str(instrument_id),
                    "resolution_instrument_id": str(resolution.instrument_id),
                },
            )
        returned_dates: list[date] = []
        out_of_window: list[date] = []
        duplicate_dates: list[date] = []
        seen: set[date] = set()
        invalid_bars: list[dict[str, object]] = []
        for segment in resolution.segments:
            for raw in self.daily_bars(
                segment.source_code,
                segment.first_requested_session,
                segment.last_requested_session,
            ):
                self.require_row_code(raw, segment.source_code)
                bar = self.project_bar(raw, instrument_id)
                returned_dates.append(bar.trade_date)
                if bar.trade_date in seen:
                    duplicate_dates.append(bar.trade_date)
                seen.add(bar.trade_date)
                if bar.trade_date not in segment.requested_sessions:
                    out_of_window.append(bar.trade_date)
                invalid_bars.extend(self.validate_bar(bar, instrument_id))
        expected = list(resolution.requested_sessions)
        missing = sorted(set(expected) - set(returned_dates))
        structurally_complete = not (missing or duplicate_dates or out_of_window)
        status = "ready" if structurally_complete and not invalid_bars else "blocked"
        return {
            "status": status,
            "instrument_id": str(instrument_id),
            "source": resolution.source,
            "frequency": "1d",
            "price_basis": PriceBasis.RAW.value,
            "requested_range": {
                "start_date": expected[0].isoformat() if expected else None,
                "end_date": expected[-1].isoformat() if expected else None,
            },
            "expected_sessions": len(expected),
            "returned_sessions": len(set(returned_dates)),
            "missing_sessions": [day.isoformat() for day in missing],
            "duplicate_sessions": sorted({day.isoformat() for day in duplicate_dates}),
            "out_of_window_sessions": sorted({day.isoformat() for day in out_of_window}),
            "mapping_segments": [
                {
                    "source_code": segment.source_code,
                    "first_session": segment.first_requested_session.isoformat(),
                    "last_session": segment.last_requested_session.isoformat(),
                    "requested_sessions": [day.isoformat() for day in segment.requested_sessions],
                    "fact_id": str(segment.fact_id) if segment.fact_id else None,
                    "fact_version": segment.fact_version,
                    "mapping_evidence": segment.mapping.evidence,
                }
                for segment in resolution.segments
            ],
            "invalid_bars": invalid_bars,
            "adapter_key": ETF_ADAPTER_KEY,
            "adapter_version": ETF_ADAPTER_VERSION,
            "rule_key": ETF_VALIDATION_RULE_KEY,
            "rule_version": ETF_VALIDATION_RULE_VERSION,
        }

    # ------------------------------------------------------------------
    # Coverage qualification port
    # ------------------------------------------------------------------

    def qualify(
        self, request: CoverageQualificationRequest
    ) -> InstrumentCoverageQualification:
        """Evaluate one ETF identity through the shared qualification port."""

        if not isinstance(request, CoverageQualificationRequest):
            raise InvalidDataRequestError(
                "request must be a CoverageQualificationRequest"
            )
        return self.qualify_instrument(request)

    def trading_status(self, query: TradingStatusQuery) -> tuple[TradingStatus, ...]:
        """Project PIT-filtered status rows into generic status facts.

        A missing row is not converted to ``tradable`` here.  The separate
        coverage proof is the only authority for a complete no-event window;
        this method returns only source-provided status observations.
        """

        if self.trading_status_facts is None:
            raise UnsupportedCapabilityError("trading status facts are unavailable")
        status_reader = self.trading_status_facts
        try:
            parameters = inspect.signature(status_reader).parameters
        except (TypeError, ValueError):
            parameters = {}
        status_kwargs = (
            {"knowledge_as_of": query.boundary.knowledge_as_of}
            if "knowledge_as_of" in parameters
            else {}
        )
        rows = status_reader(
            query.instrument_ids,
            query.window.start_date,
            query.window.end_date,
            query.boundary.data_cutoff,
            **status_kwargs,
        )
        result: list[TradingStatus] = []
        for row in rows:
            instrument_id = getattr(row, "instrument_id", None)
            if not isinstance(instrument_id, UUID) or instrument_id not in query.instrument_ids:
                raise ProviderContractViolationError(
                    "trading status row has an invalid instrument identity"
                )
            valid_from = getattr(row, "valid_from", None) or getattr(row, "trade_date", None)
            if not isinstance(valid_from, date) or isinstance(valid_from, datetime):
                raise ProviderContractViolationError(
                    "trading status row has no valid effective date"
                )
            valid_to = getattr(row, "valid_to", None) or (valid_from + timedelta(days=1))
            if valid_to <= query.window.start_date or valid_from > query.window.end_date:
                raise ProviderContractViolationError(
                    "trading status row is outside the requested window"
                )
            try:
                quality = QualityStatus(str(getattr(row, "quality_status", "unavailable")))
            except ValueError as exc:
                raise ProviderContractViolationError(
                    "trading status row has an unsupported quality status"
                ) from exc
            observed = getattr(row, "observed_at", None)
            if not isinstance(observed, datetime):
                raise ProviderContractViolationError(
                    "trading status row has no observed_at timestamp"
                )
            if observed.tzinfo is None or observed.utcoffset() is None:
                observed = observed.replace(tzinfo=timezone.utc)
            known_at = getattr(row, "known_at", None)
            if known_at is not None and (
                known_at.tzinfo is None or known_at.utcoffset() is None
            ):
                known_at = known_at.replace(tzinfo=timezone.utc)
            if observed > query.boundary.data_cutoff or (
                known_at is not None and known_at > query.boundary.data_cutoff
            ) or (
                query.boundary.knowledge_as_of is not None
                and (
                    known_at is None
                    or known_at > query.boundary.knowledge_as_of
                )
            ):
                raise ProviderContractViolationError(
                    "trading status row is not visible at the requested PIT cutoff"
                )
            result.append(
                TradingStatus(
                    instrument_id=instrument_id,
                    status=str(getattr(row, "status", "")).strip(),
                    valid_from=valid_from,
                    valid_to=valid_to,
                    evidence=FactEvidence(
                        source=str(getattr(row, "source", self.source)),
                        observed_at=observed,
                        known_at=known_at,
                        quality_status=quality,
                        source_revision=getattr(row, "source_revision", None),
                    ),
                    attributes={
                        "source_code": getattr(row, "ts_code", None),
                        "dimension": getattr(row, "dimension", "suspension"),
                        "fact_version": getattr(row, "fact_version", None),
                    },
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.valid_from,
                    str(item.instrument_id),
                    item.attributes.get("dimension", ""),
                ),
            )
        )

    def trading_status_coverage_facts(
        self,
        instrument_ids: Sequence[UUID],
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
        knowledge_as_of: datetime | None = None,
    ) -> tuple[DataCoverageFact, ...]:
        """Project persisted status coverage proofs into generic coverage facts."""

        if self.trading_status_coverage is None:
            raise UnsupportedCapabilityError("trading status coverage is unavailable")
        coverage_reader = self.trading_status_coverage
        try:
            parameters = inspect.signature(coverage_reader).parameters
        except (TypeError, ValueError):
            parameters = {}
        coverage_kwargs = (
            {"knowledge_as_of": knowledge_as_of}
            if "knowledge_as_of" in parameters
            else {}
        )
        rows = coverage_reader(
            instrument_ids, start_date, end_date, data_cutoff, **coverage_kwargs
        )
        facts: list[DataCoverageFact] = []
        for row in rows:
            if getattr(row, "instrument_id", None) not in instrument_ids:
                raise ProviderContractViolationError(
                    "trading status coverage row has an invalid instrument identity"
                )
            try:
                quality = QualityStatus(str(getattr(row, "status", "unavailable")))
            except ValueError as exc:
                raise ProviderContractViolationError(
                    "trading status coverage has an unsupported quality status"
                ) from exc
            observed = getattr(row, "observed_at", None) or getattr(row, "computed_at", None)
            if not isinstance(observed, datetime):
                raise ProviderContractViolationError(
                    "trading status coverage has no observed_at timestamp"
                )
            if observed.tzinfo is None or observed.utcoffset() is None:
                observed = observed.replace(tzinfo=timezone.utc)
            known_at = getattr(row, "known_at", None)
            if known_at is not None and (
                known_at.tzinfo is None or known_at.utcoffset() is None
            ):
                known_at = known_at.replace(tzinfo=timezone.utc)
            if observed > data_cutoff or (
                known_at is not None and known_at > data_cutoff
            ) or (
                knowledge_as_of is not None
                and (known_at is None or known_at > knowledge_as_of)
            ) or (
                not isinstance(start_date, date)
                or not isinstance(end_date, date)
                or row.start_date > start_date
                or row.end_date < end_date
            ):
                raise ProviderContractViolationError(
                    "trading status coverage is not visible or does not cover the requested window"
                )
            facts.append(
                DataCoverageFact(
                    instrument_id=row.instrument_id,
                    session_date=row.start_date,
                    capability=DataCapability.STATUS,
                    field=str(getattr(row, "dimension", "suspension")),
                    validation_rule=getattr(row, "validation_rule", None),
                    quality_status=quality,
                    evidence=FactEvidence(
                        source=str(getattr(row, "source", self.source)),
                        observed_at=observed,
                        known_at=known_at,
                        quality_status=quality,
                        source_revision=getattr(row, "source_revision", None),
                    ),
                    details={
                        "start_date": row.start_date.isoformat(),
                        "end_date": row.end_date.isoformat(),
                        "event_count": getattr(row, "event_count", None),
                        "summary": getattr(row, "summary", {}) or {},
                        "evidence": getattr(row, "evidence", {}) or {},
                    },
                )
            )
        return tuple(facts)

    def corporate_actions(self, query):
        """Read normalized actions through the injected repository only."""
        if self.corporate_action_repository is None:
            raise UnsupportedCapabilityError("corporate_actions repository is unavailable")
        rows = self.corporate_action_repository.list_facts(
            query.instrument_ids,
            query.window.start_date,
            query.window.end_date,
            cutoff=query.boundary.data_cutoff,
            knowledge_as_of=query.boundary.knowledge_as_of,
            action_types=query.action_types,
        )
        result = []
        supported_types = {
            "cash_dividend",
            "split",
            "consolidation",
            "share_change",
        }
        for row in rows:
            action_type = getattr(row, "action_type", None)
            if action_type not in supported_types:
                raise ProviderContractViolationError(
                    "provider returned an unsupported corporate action type",
                    details={
                        "event_id": str(getattr(row, "event_id", "")),
                        "action_type": action_type,
                        "reason_code": "corporate_action_type_unsupported",
                    },
                )
            if action_type in {"split", "consolidation", "share_change"}:
                # v1 keeps quantity actions in the common fact model but does
                # not account for them.  Failing here prevents a later
                # snapshot conversion from silently dropping the event.
                raise ProviderContractViolationError(
                    "quantity-class corporate actions are unsupported in formal v1",
                    details={
                        "event_id": str(row.event_id),
                        "action_type": action_type,
                        "reason_code": "quantity_corporate_action_unsupported",
                    },
                )

            evidence_payload = dict(getattr(row, "evidence", None) or {})
            ex_date = row.ex_date
            if ex_date is None:
                raise ProviderContractViolationError(
                    "corporate action has no usable effective date",
                    details={
                        "event_id": str(row.event_id),
                        "action_type": action_type,
                        "reason_code": "corporate_action_date_unresolved",
                    },
                )
            # Cash events must carry explicit calendar/date-rule evidence.
            # Never guess an exchange calendar or host timezone at runtime.
            required = ("calendar_id", "timezone", "cash_date_rule", "timing_rule")
            if not all(
                getattr(row, name, None) or evidence_payload.get(name)
                for name in required
            ):
                raise ProviderContractViolationError(
                    "corporate action calendar/date-rule evidence is incomplete",
                    details={
                        "event_id": str(row.event_id),
                        "reason_code": "corporate_action_calendar_unresolved",
                    },
                )
            if row.source_payment_date is None and row.source_arrival_date is None:
                raise ProviderContractViolationError(
                    "cash corporate action has no source payment or arrival date",
                    details={
                        "event_id": str(row.event_id),
                        "reason_code": "corporate_action_cash_date_unresolved",
                    },
                )
            raw_quality = getattr(row, "quality", None)
            if raw_quality is None:
                raw_quality = getattr(
                    getattr(row, "evidence", None),
                    "quality_status",
                    "unavailable",
                )
            quality = raw_quality
            try:
                quality_status = QualityStatus(quality)
            except ValueError as exc:
                raise ProviderContractViolationError(
                    "corporate action quality status is unsupported",
                    details={"event_id": str(row.event_id), "quality": quality},
                ) from exc
            if quality_status is not QualityStatus.COMPLETE:
                raise ProviderContractViolationError(
                    "corporate action is not complete enough for runtime use",
                    details={
                        "event_id": str(row.event_id),
                        "quality_status": quality_status.value,
                        "reason_code": "corporate_action_quality_incomplete",
                    },
                )
            observed = (
                getattr(row, "observed_at", None)
                or getattr(row, "created_at", None)
                or datetime(1970, 1, 1, tzinfo=timezone.utc)
            )
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            known_at = getattr(row, "known_at", None)
            if known_at is not None and known_at.tzinfo is None:
                known_at = known_at.replace(tzinfo=timezone.utc)
            source_revision = (
                getattr(row, "source_revision", None)
                or evidence_payload.get("source_revision")
                or str(row.fact_version)
            )
            if observed > query.boundary.data_cutoff or (
                known_at is not None and known_at > query.boundary.data_cutoff
            ) or (
                query.boundary.knowledge_as_of is not None
                and (
                    known_at is None
                    or known_at > query.boundary.knowledge_as_of
                )
            ):
                raise ProviderContractViolationError(
                    "corporate action is not visible at the requested PIT cutoff",
                    details={"event_id": str(row.event_id)},
                )
            result.append(
                CorporateAction(
                    instrument_id=row.instrument_id,
                    action_type=action_type,
                    ex_date=ex_date,
                    valid_from=getattr(row, "valid_from", None),
                    valid_to=getattr(row, "valid_to", None),
                    effective_time=getattr(row, "effective_time", None),
                    record_date=getattr(row, "record_date", None),
                    source_payment_date=getattr(row, "source_payment_date", None),
                    source_arrival_date=getattr(row, "source_arrival_date", None),
                    cash_effective_date=getattr(row, "cash_effective_date", None),
                    cash_effective_phase=getattr(row, "cash_effective_phase", None),
                    cash_amount_per_unit=getattr(row, "cash_amount_per_unit", None),
                    currency=getattr(row, "currency", None),
                    evidence=FactEvidence(
                        source=row.source,
                        observed_at=observed,
                        known_at=known_at,
                        quality_status=quality_status,
                        source_revision=str(source_revision),
                    ),
                    attributes={
                        "event_id": str(row.event_id),
                        "record_date": row.record_date.isoformat() if row.record_date else None,
                        "source_payment_date": row.source_payment_date.isoformat() if row.source_payment_date else None,
                        "source_arrival_date": row.source_arrival_date.isoformat() if row.source_arrival_date else None,
                        "cash_effective_date": row.cash_effective_date.isoformat() if row.cash_effective_date else None,
                        "cash_amount_per_unit": str(row.cash_amount_per_unit) if row.cash_amount_per_unit is not None else None,
                        "currency": row.currency,
                        "cash_effective_phase": row.cash_effective_phase,
                        "entitlement_rule": row.entitlement_rule,
                        "cash_date_rule": row.cash_date_rule,
                        "timing_rule": row.timing_rule,
                        "valid_from": (
                            getattr(row, "valid_from", None).isoformat()
                            if isinstance(getattr(row, "valid_from", None), date)
                            else None
                        ),
                        "valid_to": (
                            getattr(row, "valid_to", None).isoformat()
                            if isinstance(getattr(row, "valid_to", None), date)
                            else None
                        ),
                        "effective_time": (
                            getattr(row, "effective_time", None).isoformat()
                            if isinstance(getattr(row, "effective_time", None), datetime)
                            else None
                        ),
                        "known_at": known_at.isoformat() if known_at else None,
                        "observed_at": observed.isoformat(),
                        "source_revision": source_revision,
                        **evidence_payload,
                    },
                )
            )
        result.sort(key=lambda x: (x.ex_date, str(x.instrument_id), x.action_type))
        return tuple(result)

    def corporate_action_coverage(
        self,
        instrument_ids,
        start_date: date,
        end_date: date,
        *,
        cutoff: datetime | None = None,
        knowledge_as_of: datetime | None = None,
        action_types=(),
    ):
        """Return persisted domain coverage facts for 16A projection."""
        if self.corporate_action_repository is None or not callable(getattr(self.corporate_action_repository, "coverage", None)):
            raise UnsupportedCapabilityError("corporate action coverage is unavailable")
        coverage = self.corporate_action_repository.coverage
        return coverage(
            instrument_ids,
            start_date,
            end_date,
            cutoff=cutoff,
            knowledge_as_of=knowledge_as_of,
            action_types=action_types,
        )

    def corporate_action_coverage_facts(
        self,
        instrument_ids,
        start_date: date,
        end_date: date,
        *,
        cutoff: datetime | None = None,
        knowledge_as_of: datetime | None = None,
        action_types=(),
    ):
        """Project persisted coverage rows into immutable 16A facts."""
        rows = self.corporate_action_coverage(
            instrument_ids,
            start_date,
            end_date,
            cutoff=cutoff,
            knowledge_as_of=knowledge_as_of,
            action_types=action_types,
        )
        facts = []
        for row in rows:
            try:
                status = QualityStatus(str(row.status))
            except ValueError as exc:
                raise ProviderContractViolationError(
                    "corporate action coverage status is unsupported",
                    details={"status": row.status},
                ) from exc
            evidence_data = row.evidence or {}
            observed = (
                getattr(row, "observed_at", None)
                or getattr(row, "computed_at", None)
                or datetime(1970, 1, 1, tzinfo=timezone.utc)
            )
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            known_at = getattr(row, "known_at", None)
            if known_at is not None and known_at.tzinfo is None:
                known_at = known_at.replace(tzinfo=timezone.utc)
            if cutoff is not None and (
                observed > cutoff
                or (known_at is not None and known_at > cutoff)
            ):
                raise ProviderContractViolationError(
                    "corporate action coverage is not visible at the requested PIT cutoff"
                )
            if knowledge_as_of is not None and (
                known_at is None or known_at > knowledge_as_of
            ):
                raise ProviderContractViolationError(
                    "corporate action coverage lacks strict PIT evidence"
                )
            evidence = FactEvidence(
                source=getattr(row, "source", None) or "corporate_action_repository",
                observed_at=observed,
                known_at=known_at,
                quality_status=status,
                source_revision=getattr(row, "source_revision", None),
            )
            count = row.event_count
            details = {"event_count": count, "summary": row.summary or {}, **evidence_data}
            facts.append(DataCoverageFact(
                instrument_id=row.instrument_id, session_date=row.start_date,
                capability=DataCapability.ACTIONS,
                field=row.action_type or "corporate_actions",
                validation_rule=row.validation_rule,
                quality_status=status, evidence=evidence, details=details,
            ))
        return tuple(facts)

    def qualify_instrument(
        self,
        request: CoverageQualificationRequest | UUID | None = None,
        **kwargs: object,
    ) -> InstrumentCoverageQualification:
        """Project ETF PIT Bar coverage into one auditable result.

        This adapter only reads the injected mapping, bar, and trading-day
        ports.  It never scans an ETF catalogue, calls a strategy, or creates
        an internal fixture implicitly.  A malformed provider row remains a
        provider contract failure; missing/invalid bars become candidate
        ineligibility in the returned result.
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
                    raise InvalidDataRequestError("instrument_id must be a UUID")
                values["instrument_id"] = request
            try:
                instrument_id = values["instrument_id"]
                requested_window = values["requested_window"]
                effective_date = values["effective_date"]
                required_capabilities = values["required_capabilities"]
                query_boundary = values["query_boundary"]
                resolved_calendar_ids = values["resolved_calendar_ids"]
            except KeyError as exc:
                raise InvalidDataRequestError(
                    f"missing qualification field: {exc.args[0]}"
                ) from exc
            if not isinstance(requested_window, DateRange):
                raise InvalidDataRequestError("requested_window must be a DateRange")
            qualification_request = CoverageQualificationRequest(
                instrument_id=instrument_id,
                effective_date=effective_date,
                requested_window=requested_window,
                formal_envelope=values.get("formal_envelope", requested_window),
                warmup_envelope=values.get("warmup_envelope"),
                history_envelope=values.get("history_envelope") or requested_window,
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

        profile = PreflightProfileRegistry().resolve(
            qualification_request.preflight_profile
        )
        status_requested = _status_requested(
            qualification_request.required_capabilities
        )
        if (
            profile.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY
            and not status_requested
        ):
            # A shared internal fixture bundle may include trading-status
            # evidence for another capability path.  It is not part of this
            # bars-only qualification contract, so remove it before the
            # request is validated and hashed.  This keeps the N/A path from
            # depending on, or recording, an unconsumed status substitute.
            request_fixtures = tuple(
                fixture
                for fixture in qualification_request.fixtures
                if _capability_value(getattr(fixture, "capability", None))
                != _TRADING_STATUS_FIXTURE_CAPABILITY
            )
            request_fixture_capabilities = tuple(
                capability
                for capability in qualification_request.required_fixture_capabilities
                if _capability_value(capability)
                != _TRADING_STATUS_FIXTURE_CAPABILITY
            )
            if (
                request_fixtures != qualification_request.fixtures
                or request_fixture_capabilities
                != qualification_request.required_fixture_capabilities
            ):
                qualification_request = replace(
                    qualification_request,
                    fixtures=request_fixtures,
                    required_fixture_capabilities=request_fixture_capabilities,
                )
        configured_fixtures = tuple(self.fixtures) + tuple(qualification_request.fixtures)
        fixtures = (
            _fixtures_for_capabilities(
                configured_fixtures,
                qualification_request.required_capabilities,
            )
            if profile.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY
            else configured_fixtures
        )
        profile.validate_request(qualification_request)
        if profile.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY:
            if profile.allow_degraded:
                raise InvalidDataRequestError(
                    "internal_link_acceptance@1 forbids degraded results"
                )
            for fixture in fixtures:
                if not profile.accepts_fixture(fixture):
                    raise InvalidDataRequestError(
                        "fixture is not allowed by internal_link_acceptance@1",
                        details={
                            "reason_code": "internal_preflight_fixture_missing",
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                        },
                    )
                if not fixture.covers(qualification_request):
                    raise InvalidDataRequestError(
                        "fixture does not cover the qualification request",
                        details={
                            "reason_code": "internal_preflight_fixture_out_of_scope",
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                        },
                    )
        elif fixtures:
            # Formal profile never consumes fixture-only facts, even when an
            # adapter happens to have such facts attached for another run.
            raise InvalidDataRequestError(
                "formal@1 rejects fixture_only facts",
                details={"reason_code": "formal_fixture_not_allowed"},
            )

        instrument_id = qualification_request.instrument_id
        try:
            start = qualification_request.history_envelope or qualification_request.formal_envelope
            expected_sessions = self._qualification_sessions(
                qualification_request,
                start_date=start.start_date,
                end_date=start.end_date,
            )
            # Resolve the same PIT mappings used by the execution path before
            # reading any source rows; no current-code fallback is permitted.
            resolution = self.resolve(
                instrument_id,
                sessions=expected_sessions,
                data_cutoff=qualification_request.query_boundary.data_cutoff,
            )
            summary = self.preflight_bars(
                instrument_id,
                resolution=resolution,
            )
        except HistoryIncompleteError as exc:
            # An empty resolved session set is a candidate-level unavailable
            # result only when the request's calendar returned no sessions;
            # mapping/provider contract failures still propagate as stable
            # request errors from ``resolve``.
            if "no trading sessions" not in str(exc):
                raise
            expected_sessions = ()
            summary = {
                "status": "blocked",
                "instrument_id": str(instrument_id),
                "expected_sessions": 0,
                "returned_sessions": 0,
                "missing_sessions": [],
                "duplicate_sessions": [],
                "out_of_window_sessions": [],
                "invalid_bars": [],
            }

        report = self._qualification_report(
            instrument_id,
            expected_sessions,
            summary,
            qualification_request,
        )
        reasons: list[str] = []
        fixture_capabilities = {fixture.capability for fixture in fixtures}
        missing_named_fixtures = set(
            qualification_request.required_fixture_capabilities
        ) - fixture_capabilities
        if missing_named_fixtures:
            reasons.append("internal_preflight_fixture_missing")
        if DataCapability.ACTIONS in qualification_request.required_capabilities and (
            "quantity_action_coverage" not in fixture_capabilities
        ):
            reasons.append("internal_preflight_fixture_missing")
        if DataCapability.STATUS in qualification_request.required_capabilities and (
            "trading_status" not in fixture_capabilities
        ):
            reasons.append("internal_preflight_fixture_missing")
        unsupported_dimensions = set(qualification_request.required_capabilities) - {
            DataCapability.BARS,
            DataCapability.ACTIONS,
            DataCapability.STATUS,
            DataCapability.COVERAGE,
        }
        if unsupported_dimensions:
            raise UnsupportedCapabilityError(
                "ETF qualification does not implement the requested data dimension",
                details={
                    "capabilities": sorted(item.value for item in unsupported_dimensions)
                },
            )
        if report.quality_status is QualityStatus.PARTIAL:
            reasons.append("coverage_incomplete")
        elif report.quality_status is QualityStatus.INVALID:
            reasons.append("coverage_invalid")
        elif report.quality_status is QualityStatus.UNAVAILABLE:
            reasons.append("coverage_unavailable")
        if summary.get("duplicate_sessions") or summary.get("out_of_window_sessions"):
            reasons.append("coverage_provider_contract_violation")
        for item in summary.get("invalid_bars", ()):
            code = item.get("code") if isinstance(item, Mapping) else None
            if isinstance(code, str) and code:
                reasons.append(code)

        evidence_summary = {
            "adapter_key": ETF_ADAPTER_KEY,
            "adapter_version": ETF_ADAPTER_VERSION,
            "validation_rule": {
                "key": ETF_VALIDATION_RULE_KEY,
                "version": ETF_VALIDATION_RULE_VERSION,
            },
            "profile": {
                "key": qualification_request.preflight_profile.key,
                "version": qualification_request.preflight_profile.version,
            },
            "request": qualification_request.machine_content(),
            "preflight": _redact_sensitive(summary),
            "fixtures": [item.as_dict() for item in fixtures],
        }
        return InstrumentCoverageQualification(
            instrument_id=instrument_id,
            eligible=not reasons
            and report.quality_status is QualityStatus.COMPLETE,
            coverage_reports=(report,),
            reason_codes=tuple(sorted(set(reasons))),
            evidence_summary=evidence_summary,
            request=qualification_request,
        )

    def _qualification_sessions(
        self,
        request: CoverageQualificationRequest,
        *,
        start_date: date,
        end_date: date,
    ) -> tuple[date, ...]:
        """Read sessions from the injected calendar/trading-day port only."""

        # The existing ETF port is exchange-oriented.  Qualification requests
        # already carry resolved calendar ids, so pass each explicitly named
        # id and retain only the common date set.  No default SSE is used.
        calendars = tuple(request.resolved_calendar_ids)
        day_sets: list[set[date]] = []
        for calendar_id in calendars:
            values = self.trading_days(calendar_id, start_date, end_date)
            day_sets.append(
                {
                    day
                    for day in values
                    if isinstance(day, date) and not isinstance(day, datetime)
                }
            )
        if not day_sets:
            return ()
        return tuple(sorted(set.intersection(*day_sets)))

    @staticmethod
    def _qualification_report(
        instrument_id: UUID,
        expected_sessions: Sequence[date],
        summary: Mapping[str, object],
        request: CoverageQualificationRequest,
    ) -> DataCoverageReport:
        """Map adapter preflight evidence onto the existing report DTO."""

        expected = len(expected_sessions)
        missing = {
            date.fromisoformat(value)
            for value in summary.get("missing_sessions", ())
            if isinstance(value, str)
        }
        invalid_dates = {
            date.fromisoformat(value)
            for item in summary.get("invalid_bars", ())
            if isinstance(item, Mapping)
            for value in (item.get("trade_date"),)
            if isinstance(value, str)
        }
        duplicate = bool(summary.get("duplicate_sessions"))
        out_of_window = bool(summary.get("out_of_window_sessions"))
        expected_set = set(expected_sessions)
        missing &= expected_set
        invalid_dates &= expected_set
        unavailable = len(missing - invalid_dates)
        invalid = len(invalid_dates)
        partial = 0
        complete = max(expected - unavailable - invalid, 0)
        if duplicate or out_of_window:
            # Preserve anomaly semantics without inventing a second quality
            # enum: an anomalous row is not complete evidence.
            if complete:
                partial = 1
                complete -= partial
        if invalid:
            quality = QualityStatus.INVALID
        elif partial or unavailable:
            quality = QualityStatus.PARTIAL if complete or partial else QualityStatus.UNAVAILABLE
        elif expected:
            quality = QualityStatus.COMPLETE
        else:
            quality = QualityStatus.UNAVAILABLE
        missing_ranges = tuple(
            DateRange(start_date=day, end_date=day)
            for day in sorted(missing | invalid_dates)
        )
        return DataCoverageReport(
            requested_window=(request.history_envelope or request.formal_envelope),
            capability=DataCapability.BARS,
            instrument_ids=(instrument_id,),
            expected_count=expected,
            complete_count=complete,
            partial_count=partial,
            invalid_count=invalid,
            unavailable_count=unavailable,
            quality_status=quality,
            missing_ranges=missing_ranges,
        )

    def project_coverage_report(
        self,
        instrument_id: UUID,
        expected_sessions: Sequence[date],
        summary: Mapping[str, object],
        request: CoverageQualificationRequest,
    ) -> DataCoverageReport:
        """Public read-only name for the Bar-coverage projection."""

        return self._qualification_report(
            instrument_id, expected_sessions, summary, request
        )

    coverage_report = project_coverage_report

    def bar_validity_summary(
        self,
        rows: Sequence[DailyBarRow | Bar],
        *,
        instrument_id: UUID | None = None,
    ) -> dict[str, object]:
        """Summarize ETF v1 validity without dropping raw source rows."""

        invalid_bars: list[dict[str, object]] = []
        for row in rows:
            invalid_bars.extend(self.validate_bar(row, instrument_id))
        return {
            "status": "blocked" if invalid_bars else "ready",
            "adapter_key": ETF_ADAPTER_KEY,
            "adapter_version": ETF_ADAPTER_VERSION,
            "rule_key": ETF_VALIDATION_RULE_KEY,
            "rule_version": ETF_VALIDATION_RULE_VERSION,
            "invalid_count": len({
                (item.get("instrument_id"), item.get("trade_date"))
                for item in invalid_bars
            }),
            "invalid_field_count": len(invalid_bars),
            "invalid_bars": invalid_bars,
        }


    # ------------------------------------------------------------------
    # Adjustment factors
    # ------------------------------------------------------------------

    def project_factor(
        self,
        row: AdjustmentFactorRow,
        instrument_id: UUID,
        *,
        price_basis: PriceBasis = PriceBasis.QFQ,
        cutoff: date | None = None,
        source_code: str | None = None,
    ) -> AdjustedSeriesPoint:
        """Project one current factor after strict source-row validation.

        ``trade_date`` is the source field and ``point_date`` is its
        normalized ``effective_date``.  The optional source coordinates on
        the generic point preserve that relation for audit consumers.
        """

        expected_code = source_code or getattr(row, "ts_code", None)
        if not isinstance(expected_code, str) or not expected_code.strip():
            raise ProviderContractViolationError(
                "adjustment factor requires a non-blank source code"
            )
        normalized = normalize_adjustment_factor(
            row,
            instrument_id=instrument_id,
            source=self.source,
            expected_source_code=expected_code.strip(),
            cutoff=cutoff,
        )
        return normalized.as_point(price_basis=price_basis)

    def adjusted_series(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
        price_basis: PriceBasis,
    ) -> SegmentedAdjustedSeries:
        """Read factors per mapped segment under the activation gate."""

        if price_basis is PriceBasis.RAW:
            raise InvalidDataRequestError(
                "raw prices need no adjustment series"
            )
        if self.adjustment_policy is None or not self.adjustment_policy.is_active():
            raise UnsupportedCapabilityError(
                "the tushare_adj_factor_native@1 policy is not verified "
                "and active; adjusted series are blocked",
                details={
                    "policy_key": ADJUSTMENT_SERIES_POLICY_KEY,
                    "policy_version": ADJUSTMENT_SERIES_POLICY_VERSION,
                },
            )
        policy = self.adjustment_policy
        effective_date_mapping = policy.effective_date.strip().casefold()
        if (
            policy.source.strip().casefold() != self.source.strip().casefold()
            or policy.factor_field != "adj_factor"
            or not (
                effective_date_mapping == "trade_date"
                or (
                    effective_date_mapping.startswith("trade_date")
                    and "normalized" in effective_date_mapping
                )
            )
        ):
            raise InvalidDataRequestError(
                "active adjustment policy does not match ETF factor storage",
                details={
                    "policy_source": policy.source,
                    "adapter_source": self.source,
                    "factor_field": policy.factor_field,
                    "effective_date": policy.effective_date,
                },
            )
        adapter = self
        cutoff_date = cutoff_local_date(
            resolution.data_cutoff, self.market_timezone
        )

        class _FactorReader:
            def read_factors(self, source_code: str, start_date: date, end_date: date):
                points = []
                for row in adapter.adjustment_factors(source_code, start_date, end_date):
                    adapter.require_row_code(row, source_code)
                    point = adapter.project_factor(
                        row,
                        instrument_id,
                        price_basis=price_basis,
                        cutoff=cutoff_date,
                        source_code=source_code,
                    )
                    row_date = point.point_date
                    # A repository returning an out-of-segment row is a
                    # contract violation, not a row to silently discard.
                    if not (start_date <= row_date <= end_date):
                        raise HistoryIncompleteError(
                            "adjustment factor falls outside its PIT segment",
                            details={
                                "instrument_id": str(instrument_id),
                                "source_code": source_code,
                                "effective_date": row_date.isoformat(),
                                "expected_start_date": start_date.isoformat(),
                                "expected_end_date": end_date.isoformat(),
                            },
                        )
                    if point.price_basis is not price_basis:
                        continue
                    points.append(point)
                return points

        return read_segmented_adjusted_series(
            resolution,
            _FactorReader(),
            cutoff_date=cutoff_date,
        )

    def research_price_series(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
        price_basis: PriceBasis,
    ) -> SegmentedBarHistory:
        """Generate an explicitly requested qfq/hfq Bar series for research.

        Raw bars and cutoff-visible factors are read independently, then the
        source-native semantics frozen by the active policy are applied by the
        ETF adapter.  The returned bars are new objects; the raw table and raw
        ``Bar`` facts are never mutated or replaced.
        """

        if price_basis is PriceBasis.RAW:
            raise InvalidDataRequestError(
                "research price series require qfq or hfq basis"
            )
        policy = getattr(self, "adjustment_policy", None)
        if policy is not None:
            active = bool(getattr(policy, "is_active", lambda: False)())
            if not active:
                raise UnsupportedCapabilityError(
                    "the adjustment policy is not verified and active",
                    details={
                        "policy_key": getattr(policy, "policy_key", None),
                        "price_basis": price_basis.value,
                    },
                )
            formula = getattr(
                policy,
                "qfq_formula" if price_basis is PriceBasis.QFQ else "hfq_formula",
                None,
            )
            anchor = getattr(
                policy,
                "qfq_anchor" if price_basis is PriceBasis.QFQ else "hfq_anchor",
                None,
            )
            precision = getattr(policy, "precision", None)
            rounding = getattr(policy, "rounding", None)
            policy_key = getattr(policy, "key", None)
            policy_version = getattr(policy, "version", None)
        else:
            # Legacy boolean construction remains a read gate only.  It does
            # not imply a formula, anchor, precision, or rounding contract.
            if not self.adjustment_active:
                raise UnsupportedCapabilityError(
                    "the adjustment policy is not verified and active",
                    details={"price_basis": price_basis.value},
                )
            formula = getattr(
                self,
                "qfq_formula" if price_basis is PriceBasis.QFQ else "hfq_formula",
                None,
            )
            anchor = getattr(
                self,
                "qfq_anchor" if price_basis is PriceBasis.QFQ else "hfq_anchor",
                None,
            )
            precision = getattr(self, "adjustment_precision", None)
            rounding = getattr(self, "adjustment_rounding", None)
            policy_key = ADJUSTMENT_SERIES_POLICY[0]
            policy_version = ADJUSTMENT_SERIES_POLICY[1]
        raw_history = self.bars(instrument_id, resolution=resolution)
        factor_history = self.adjusted_series(
            instrument_id,
            resolution=resolution,
            price_basis=price_basis,
        )
        adjusted = build_research_price_series(
            raw_history.bars,
            factor_history.points,
            price_basis=price_basis,
            formula=formula,
            anchor=anchor,
            precision=precision,
            rounding=rounding,
            policy_key=policy_key,
            policy_version=policy_version,
        )
        return SegmentedBarHistory(
            bars=adjusted,
            resolution=raw_history.resolution,
            segment_evidence=raw_history.segment_evidence,
        )

    # Descriptive aliases keep the adapter-to-view conversion discoverable
    # without introducing another public data contract or source of truth.
    def research_bars(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
        price_basis: PriceBasis,
    ) -> SegmentedBarHistory:
        return self.research_price_series(
            instrument_id, resolution=resolution, price_basis=price_basis
        )

    def adjusted_bars(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
        price_basis: PriceBasis,
    ) -> SegmentedBarHistory:
        return self.research_price_series(
            instrument_id, resolution=resolution, price_basis=price_basis
        )

    # ------------------------------------------------------------------
    # Calendar projection
    # ------------------------------------------------------------------

    def session_points(
        self,
        start_date: date,
        end_date: date,
        *,
        calendar_id: str | None = None,
        snapshot: CalendarSnapshot | None = None,
        instrument_id: UUID | None = None,
        data_cutoff: datetime | None = None,
    ) -> tuple[SessionPoint, ...]:
        """Project sessions from an immutable PIT calendar snapshot.

        Callers must supply the CalendarSnapshot opened for the
        identity-derived calendar; it is the only source that can prove PIT
        session definitions.
        """

        if snapshot is not None:
            if start_date < snapshot.request.formal_start or end_date > snapshot.request.formal_end:
                raise ProviderContractViolationError(
                    "calendar snapshot does not cover requested session range"
                )
            points = tuple(
                point for point in snapshot.resolution.resolved_sessions
                if start_date <= point.session_date <= end_date
            )
            for point in points:
                context = point.context
                if context is None or not context.calendar_ids:
                    raise ProviderContractViolationError(
                        "ETF session points require instrument/calendar snapshot context"
                    )
                if instrument_id is not None and instrument_id not in context.instrument_ids:
                    raise ProviderContractViolationError(
                        "session point context does not include the requested instrument"
                    )
                if calendar_id is not None and normalize_calendar_id(calendar_id) not in context.calendar_ids:
                    raise ProviderContractViolationError(
                        "session point context does not include the requested calendar"
                    )
            return points
        # Calendar windows and timezone are facts of the immutable snapshot;
        # the adapter cannot synthesize them from an exchange or code.
        raise ProviderContractViolationError(
            "ETF session projection requires an immutable CalendarSnapshot"
        )

    # ------------------------------------------------------------------
    # Coverage, PIT status, and revision summaries
    # ------------------------------------------------------------------

    @staticmethod
    def coverage_summary(
        expected_sessions: Sequence[date],
        returned_dates: Sequence[date],
    ) -> dict[str, object]:
        """Structured coverage summary in the preflight-report shape.

        Duplicate and out-of-window returns are recorded explicitly and
        downgrade the status to ``partial``: deduplicating silently would
        let a buggy reader report complete coverage while serving the same
        session twice.
        """

        expected_set = set(expected_sessions)
        seen: set[date] = set()
        duplicates: list[date] = []
        out_of_window: list[date] = []
        for day in returned_dates:
            if day not in expected_set:
                out_of_window.append(day)
            elif day in seen:
                duplicates.append(day)
            else:
                seen.add(day)
        missing = sorted(
            day.isoformat() for day in expected_set if day not in seen
        )
        anomalies = bool(duplicates or out_of_window)
        if missing or anomalies:
            status = "partial"
        else:
            status = "complete" if len(seen) == len(expected_set) else "partial"
        return {
            "expected_sessions": len(set(expected_sessions)),
            "returned_sessions": len(seen),
            "missing_sessions": missing,
            "duplicate_sessions": sorted(day.isoformat() for day in duplicates),
            "out_of_window_sessions": sorted(
                day.isoformat() for day in out_of_window
            ),
            "status": status,
        }

    def pit_status(self) -> dict[str, object]:
        """Per-fact-family PIT declarations for the run metadata.

        ``adjustment_factors`` is the approved first-version policy marker,
        not a PIT support level: per the data protocol, factors follow the
        ``effective_date <= data_cutoff`` contract and never trigger the
        run-level ``non_strict_pit`` flag.
        """

        return {
            "instrument_code_mappings": "strict",
            "daily_bars": "non_strict",
            "adjustment_factors": f"{ADJUSTMENT_SERIES_POLICY[0]}"
            f"@{ADJUSTMENT_SERIES_POLICY[1]}:effective_date_cutoff",
            # Ingestion acceptance time is persisted as known_at for these
            # normalized facts; unlike raw daily bars, they can therefore be
            # selected strictly at a PIT cutoff when the production ports are
            # actually wired.
            "trading_status": (
                "strict"
                if self.trading_status_facts is not None
                and self.trading_status_coverage is not None
                else "unavailable"
            ),
            "corporate_actions": (
                "strict"
                if self.corporate_action_repository is not None
                else "unavailable"
            ),
            "corporate_action_coverage": (
                "strict"
                if self.corporate_action_repository is not None
                else "unavailable"
            ),
            "trading_calendar": "non_strict",
        }

    def revision_stamp(
        self,
        rows: Sequence[DailyBarRow] | Sequence[AdjustmentFactorRow],
    ) -> str | None:
        """Latest observed timestamp across one family of source rows."""

        stamps = [row.updated_at for row in rows if row.updated_at is not None]
        return max(stamps).isoformat() if stamps else None

    @staticmethod
    def _fact_revision_summary(rows: Sequence[object]) -> dict[str, object]:
        """Summarize source revisions, knowledge time, and valid time."""

        ordered = tuple(
            sorted(
                rows,
                key=lambda row: (
                    getattr(row, "valid_from", getattr(row, "trade_date", date.min))
                    or date.min,
                    str(getattr(row, "source_revision", "")),
                ),
            )
        )
        revisions = sorted(
            {
                str(value)
                for row in ordered
                if (value := getattr(row, "source_revision", None))
            }
        )
        known = [
            value
            for row in ordered
            if isinstance(value := getattr(row, "known_at", None), datetime)
        ]
        observed = [
            value
            for row in ordered
            if isinstance(value := getattr(row, "observed_at", None), datetime)
        ]
        valid_dates = [
            value
            for row in ordered
            for value in (
                getattr(row, "valid_from", None),
                getattr(row, "valid_to", None),
            )
            if isinstance(value, date) and not isinstance(value, datetime)
        ]
        vector = [
            {
                "instrument_id": str(getattr(row, "instrument_id", "")),
                "source_code": str(getattr(row, "ts_code", "")),
                "dimension": getattr(row, "dimension", None),
                "action_type": getattr(row, "action_type", None),
                "valid_from": getattr(row, "valid_from", getattr(row, "trade_date", None)),
                "valid_to": getattr(row, "valid_to", None),
                "known_at": getattr(row, "known_at", None),
                "observed_at": getattr(row, "observed_at", None),
                "source_revision": getattr(row, "source_revision", None),
            }
            for row in ordered
        ]
        return {
            "fact_count": len(ordered),
            "source_revisions": revisions,
            "revision_vector_hash": canonical_hash(vector),
            "valid_time_range": {
                "start": min(valid_dates).isoformat() if valid_dates else None,
                "end": max(valid_dates).isoformat() if valid_dates else None,
            },
            "known_at_range": {
                "min": min(known).isoformat() if known else None,
                "max": max(known).isoformat() if known else None,
            },
            "observed_at_range": {
                "min": min(observed).isoformat() if observed else None,
                "max": max(observed).isoformat() if observed else None,
            },
            "pit_status": "strict" if ordered and len(known) == len(ordered) else "non_strict",
        }

    @staticmethod
    def _data_revision_summary(rows: Sequence[DailyBarRow]) -> dict[str, object]:
        """Build the bounded ``data_revision_summary@1`` contract for bars.

        The vector is derived solely from consumed rows (revision, session and
        accepted time), making it deterministic and sensitive to corrections
        without retaining raw payloads or presentation metadata.
        """
        ordered = sorted(
            rows,
            key=lambda row: (
                getattr(row, "trade_date", date.min),
                str(getattr(row, "ts_code", "")),
            ),
        )
        revisions = [getattr(row, "source_revision", None) for row in ordered]
        missing = sum(1 for revision in revisions if not revision)
        dates = [row.trade_date for row in ordered if isinstance(getattr(row, "trade_date", None), date)]
        accepted = [row.updated_at for row in ordered if isinstance(getattr(row, "updated_at", None), datetime)]
        vector = [
            {
                "trade_date": row.trade_date.isoformat(),
                "source_code": str(getattr(row, "ts_code", "")),
                "source_revision": getattr(row, "source_revision", None),
                "accepted_at": row.updated_at.isoformat() if getattr(row, "updated_at", None) else None,
            }
            for row in ordered
        ]
        vector_hash = canonical_hash(vector)
        valid_range = {"start": min(dates).isoformat(), "end": max(dates).isoformat()} if dates else {"start": None, "end": None}
        accepted_range = {"min": min(accepted).isoformat(), "max": max(accepted).isoformat()} if accepted else {"min": None, "max": None}
        # A source revision identifies the content version, not whether the
        # current row was a historical correction.  Treating every non-null
        # revision as a correction over-reports the affected range for normal
        # first ingestion and metadata backfills.  Only explicit audit fields
        # emitted by the ingestion audit boundary are accepted here.
        def _is_correction(row: object) -> bool:
            for field in ("change_kind", "audit_change_kind", "revision_change_kind"):
                value = row.get(field) if isinstance(row, Mapping) else getattr(row, field, None)
                if value is not None:
                    return str(getattr(value, "value", value)).lower() == "correction"
            audit = row.get("audit") if isinstance(row, Mapping) else getattr(row, "audit", None)
            if isinstance(audit, Mapping):
                value = audit.get("change_kind")
                if value is not None:
                    return str(getattr(value, "value", value)).lower() == "correction"
            return False

        revised_dates = [row.trade_date for row in ordered if _is_correction(row)]
        affected = {
            "start": min(revised_dates).isoformat() if revised_dates else None,
            "end": max(revised_dates).isoformat() if revised_dates else None,
            "correction_count": len(revised_dates),
        }
        status = "complete" if ordered and missing == 0 else ("partial" if ordered else "unavailable")
        capability = {
            "source": ETF_PROVIDER_KEY,
            "fact_count": len(ordered),
            "missing_revision_count": missing,
            "correction_count": len(revised_dates),
            "valid_time_range": valid_range,
            "accepted_at_range": accepted_range,
            "affected_range": affected,
        }
        return {
            "daily_bars": {
                "source": ETF_PROVIDER_KEY,
                "revision_kind": "derived_content_hash",
                "revision_vector_hash": vector_hash,
                "fact_count": len(ordered),
                "missing_revision_count": missing,
                "valid_time_range": valid_range,
                "accepted_at_range": accepted_range,
                "affected_range": affected,
                "audit": {
                    "evidence_class": "production_audit",
                    "evidence_ref": "etf_daily_bar_revision_audits@1",
                    "status": status,
                },
            },
            "__data_revision_summary__": {
                "contract": "data_revision_summary@1",
                "status": status,
                "revision_vector_hash": vector_hash,
                "qualification": {
                    "profile": "formal@1",
                    "eligible": status == "complete",
                    "evidence_class": "production_audit",
                    "evidence_ref": "etf_daily_bar_revision_audits@1",
                    "reason_codes": [] if status == "complete" else ["missing_source_revision"],
                },
                "capabilities": {"bars": capability},
            },
        }

    def preflight_summary(
        self,
        *,
        instrument_ids: Sequence[UUID],
        expected_sessions: Sequence[date],
        bars_by_instrument: Mapping[UUID, Sequence[date]],
        factors_by_instrument: Mapping[UUID, Sequence[date]] | None = None,
        # ``strategy_price_bases`` is optional for compatibility with the
        # existing bars-only summary.  When qfq/hfq is requested, this method
        # becomes the hard admission gate for policy and coverage evidence.
        strategy_price_bases: Sequence[PriceBasis] = (),
        research_prices_by_instrument: Mapping[UUID, Sequence[date]] | None = None,
        data_cutoff: datetime | None = None,
        mappings_by_instrument: Mapping[UUID, Sequence[InstrumentCodeMapping]]
        | None = None,
        daily_rows: Sequence[DailyBarRow] = (),
        factor_rows: Sequence[AdjustmentFactorRow] = (),
        trading_status_rows: Sequence[object] = (),
        trading_status_coverage: Mapping[str, object] | None = None,
        corporate_action_rows: Sequence[object] = (),
        corporate_action_coverage: Mapping[str, object] | None = None,
        blocking_issues: Sequence[Mapping[str, object]] = (),
        requested_range: Mapping[str, object] | None = None,
        lookback_sessions: int | None = None,
        max_lookback_sessions: int = 512,
        preflight_profile: PreflightProfile | str | None = None,
        run_kind: str | None = None,
        fixtures: Sequence[InternalFixture] = (),
        required_capabilities: Sequence[DataCapability] = (),
        trading_status_applicability: Mapping[str, str] | None = None,
        trading_status_limitation: str | None = None,
    ) -> dict[str, object]:
        """Assemble the machine summary consumed by result records.

        The content deliberately excludes generation time, database keys,
        and credentials so identical data facts hash identically; the hash
        changes when source revisions, coverage, mappings, or the
        adjustment-policy activation state change.
        """

        # Keep one issue record per failed field.  This gives the report a
        # precise raw value while ``bar_validity_summary`` below provides the
        # bar-level counts operators need at a glance.
        invalid_bars: list[dict[str, object]] = []
        for row in daily_rows:
            row_instrument_id = getattr(row, "instrument_id", None)
            if row_instrument_id is None and len(instrument_ids) == 1:
                row_instrument_id = instrument_ids[0]
            invalid_bars.extend(
                self.validate_bar(row, row_instrument_id)
            )

        factor_coverage: dict[str, object] = {}
        if factors_by_instrument is not None:
            factor_coverage = {
                str(instrument_id): self.coverage_summary(
                    expected_sessions, factors_by_instrument.get(instrument_id, ())
                )
                for instrument_id in instrument_ids
            }
        research_price_coverage: dict[str, object] = {}
        if research_prices_by_instrument is not None:
            research_price_coverage = {
                str(instrument_id): self.coverage_summary(
                    expected_sessions,
                    research_prices_by_instrument.get(instrument_id, ()),
                )
                for instrument_id in instrument_ids
            }
        requested_bases: tuple[PriceBasis, ...] = tuple(
            sorted(
                {
                    basis if isinstance(basis, PriceBasis) else PriceBasis(basis)
                    for basis in strategy_price_bases
                },
                key=lambda basis: basis.value,
            )
        )
        mapping_summary: dict[str, object] = {}
        if mappings_by_instrument is not None:
            mapping_summary = {
                str(instrument_id): [
                    {
                        "source_code": mapping.source_code,
                        "trading_code": mapping.trading_code,
                        "valid_from": mapping.valid_from.isoformat(),
                        "valid_to": (
                            mapping.valid_to.isoformat()
                            if mapping.valid_to is not None
                            else None
                        ),
                        "mapping_source": mapping.mapping_source,
                        "evidence": mapping.evidence,
                        "known_at": mapping.known_at.isoformat(),
                        "source_revision": mapping.source_revision,
                    }
                    for mapping in mappings_by_instrument.get(instrument_id, ())
                ]
                for instrument_id in instrument_ids
            }
        expected_range = dict(requested_range or {})
        if not expected_range and expected_sessions:
            expected_range = {
                "start_date": min(expected_sessions).isoformat(),
                "end_date": max(expected_sessions).isoformat(),
            }
        profile = None
        profile_issues: list[dict[str, object]] = []
        configured_fixtures = tuple(self.fixtures) + tuple(fixtures)
        normalized_fixtures = configured_fixtures
        if preflight_profile is not None:
            registry = PreflightProfileRegistry()
            profile = (
                preflight_profile
                if isinstance(preflight_profile, PreflightProfile)
                else registry.resolve(preflight_profile)
            )
            if profile.key == INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY:
                normalized_fixtures = _fixtures_for_capabilities(
                    configured_fixtures,
                    required_capabilities,
                )
            if profile.run_kind != (run_kind or profile.run_kind):
                profile_issues.append(
                    {
                        "code": "internal_preflight_profile_mismatch",
                        "field": "run_kind",
                        "expected": profile.run_kind,
                        "actual": run_kind,
                    }
                )
            if profile.key == "formal" and normalized_fixtures:
                profile_issues.append(
                    {
                        "code": "formal_fixture_not_allowed",
                        "field": "fixtures",
                    }
                )
            for fixture in normalized_fixtures:
                if not isinstance(fixture, InternalFixture) or not profile.accepts_fixture(fixture):
                    profile_issues.append(
                        {
                            "code": "internal_preflight_fixture_missing",
                            "field": "fixture_key",
                            "fixture_key": getattr(fixture, "fixture_key", None),
                            "fixture_version": getattr(fixture, "fixture_version", None),
                        }
                    )
                    continue
                covered_ids = set(fixture.instrument_ids)
                if instrument_ids and covered_ids and not set(instrument_ids).issubset(covered_ids):
                    profile_issues.append(
                        {
                            "code": "internal_preflight_fixture_out_of_scope",
                            "field": "instrument_ids",
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                        }
                    )
                if expected_sessions and (
                    fixture.start_date > min(expected_sessions)
                    or fixture.end_date < max(expected_sessions)
                ):
                    profile_issues.append(
                        {
                            "code": "internal_preflight_fixture_out_of_scope",
                            "field": "date_range",
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                        }
                    )
        elif not _status_requested(required_capabilities):
            normalized_fixtures = _fixtures_for_capabilities(
                configured_fixtures,
                required_capabilities,
            )
        issues_payload = [dict(issue) for issue in blocking_issues]
        issues_payload.extend(profile_issues)
        adjusted_requested = any(
            basis in (PriceBasis.QFQ, PriceBasis.HFQ) for basis in requested_bases
        )
        policy = self.adjustment_policy
        cutoff_date: date | None = None
        if data_cutoff is not None:
            # The cutoff is an instant, while ETF factor effective dates are
            # market-local calendar dates.  Resolve it once and reuse the
            # same boundary for all adjusted-basis checks below.
            cutoff_date = cutoff_local_date(data_cutoff, self.market_timezone)
        if adjusted_requested:
            if (
                policy is None
                or policy.key != ADJUSTMENT_SERIES_POLICY_KEY
                or policy.version != ADJUSTMENT_SERIES_POLICY_VERSION
            ):
                issues_payload.append(
                    {
                        "code": "ADJUSTMENT_POLICY_MISMATCH",
                        "field": "adjustment_series_policy",
                        "reason": "qfq/hfq research requires tushare_adj_factor_native@1",
                    }
                )
            if policy is None or not policy.is_active():
                issues_payload.append(
                    {
                        "code": "ADJUSTMENT_POLICY_INACTIVE",
                        "field": "adjustment_series_policy",
                        "reason": "qfq/hfq research requires an active verified policy",
                    }
                )
            elif policy.adapter_version != ETF_ADAPTER_VERSION:
                issues_payload.append(
                    {
                        "code": "ADJUSTMENT_ADAPTER_VERSION_MISMATCH",
                        "field": "adjustment_adapter_version",
                        "reason": "the active adjustment policy targets another adapter version",
                    }
                )
            elif not policy.verification_summary:
                issues_payload.append(
                    {
                        "code": "ADJUSTMENT_VERIFICATION_MISSING",
                        "field": "verification_summary",
                        "reason": "qfq/hfq research requires a verification evidence summary",
                    }
                )
            if factors_by_instrument is None:
                issues_payload.append(
                    {
                        "code": "ADJUSTMENT_FACTOR_COVERAGE_MISSING",
                        "field": "factor_coverage",
                        "reason": "qfq/hfq research requires cutoff-visible factors",
                    }
                )
            elif any(
                coverage.get("status") != "complete"
                for coverage in factor_coverage.values()
                if isinstance(coverage, Mapping)
            ):
                issues_payload.append(
                    {
                        "code": "ADJUSTMENT_FACTOR_COVERAGE_INCOMPLETE",
                        "field": "factor_coverage",
                        "reason": "factor coverage is incomplete for the requested window",
                    }
                )
            if research_prices_by_instrument is None:
                issues_payload.append(
                    {
                        "code": "ADJUSTED_PRICE_COVERAGE_MISSING",
                        "field": "research_price_coverage",
                        "reason": "qfq/hfq research requires generated price coverage",
                    }
                )
            elif any(
                coverage.get("status") != "complete"
                for coverage in research_price_coverage.values()
                if isinstance(coverage, Mapping)
            ):
                issues_payload.append(
                    {
                        "code": "ADJUSTED_PRICE_COVERAGE_INCOMPLETE",
                        "field": "research_price_coverage",
                        "reason": "research price coverage is incomplete for the requested window",
                    }
                )
            if data_cutoff is None:
                issues_payload.append(
                    {
                        "code": "ADJUSTMENT_CUTOFF_MISSING",
                        "field": "cutoff_boundary",
                        "reason": "qfq/hfq research requires a timezone-aware data cutoff",
                    }
                )
            else:
                future_sessions = sorted(
                    {
                        day
                        for day in expected_sessions
                        if isinstance(day, date) and day > cutoff_date
                    }
                )
                if future_sessions:
                    issues_payload.append(
                        {
                            "code": "ADJUSTMENT_CUTOFF_EXCEEDED",
                            "field": "cutoff_boundary",
                            "reason": "requested adjusted sessions exceed the local data cutoff",
                            "sessions": [day.isoformat() for day in future_sessions],
                        }
                    )
                future_factors = sorted(
                    {
                        day
                        for dates in (factors_by_instrument or {}).values()
                        for day in dates
                        if isinstance(day, date) and day > cutoff_date
                    }
                )
                if future_factors:
                    issues_payload.append(
                        {
                            "code": "ADJUSTMENT_FACTOR_AFTER_CUTOFF",
                            "field": "factor_coverage",
                            "reason": "factor effective_date is later than the local data cutoff",
                            "sessions": [day.isoformat() for day in future_factors],
                        }
                    )
        cutoff_boundary = None
        if data_cutoff is not None:
            cutoff_boundary = {
                "data_cutoff": data_cutoff.isoformat(),
                "cutoff_local_date": cutoff_date.isoformat() if cutoff_date else None,
                "factor_cutoff_rule": (
                    policy.cutoff_rule
                    if policy is not None
                    else "effective_date <= data_cutoff"
                ),
            }
        # Invalid bars are blocking issues, but remain separately indexed in
        # ``invalid_bars`` so callers can render the original field/value.
        issues_payload.extend(
            {
                "code": item.get("code", "bar_invalid"),
                "instrument_id": item.get("instrument_id"),
                "trade_date": item.get("trade_date"),
                "field": item.get("field"),
                "reason": item.get("reason"),
            }
            for item in invalid_bars
        )
        # Persist only the stable policy contract.  The verification artifact
        # may carry source metadata or credential-shaped fields needed while
        # validating it; those fields never belong in a run record or its
        # hash.  The three reproducible evidence digests and summary remain.
        policy_description = self.adjustment_policy.as_dict()
        policy_payload = {
            key: policy_description.get(key)
            for key in (
                "key",
                "version",
                "policy_key",
                "status",
                "adapter_version",
                "source",
                "factor_field",
                "effective_date",
                "cutoff_rule",
                "factor_cutoff_rule",
                "qfq_formula",
                "hfq_formula",
                "formula_version",
                "qfq_anchor",
                "hfq_anchor",
                "precision",
                "rounding",
                "verification_summary",
                "verification_status",
                "verification_published",
                "verification_input_hash",
                "verification_output_hash",
                "verification_evidence_hash",
                "verification",
            )
        }
        # Merge the derived revision contract into the legacy daily-bars
        # revision record instead of replacing it wholesale.  Existing
        # consumers rely on ``latest_observed_at`` (the observation marker),
        # while the T20 summary contributes bounded revision-vector fields.
        revision_summary = self._data_revision_summary(daily_rows)
        derived_daily_revision = revision_summary.get("daily_bars", {})
        if not isinstance(derived_daily_revision, Mapping):  # pragma: no cover - defensive
            derived_daily_revision = {}
        daily_revision = {
            **derived_daily_revision,
            "source": self.source,
            "latest_observed_at": self.revision_stamp(daily_rows),
        }
        source_revisions = {
            "daily_bars": daily_revision,
            "adjustment_factors": {
                "source": self.source,
                "latest_observed_at": self.revision_stamp(factor_rows),
            },
            "trading_status": self._fact_revision_summary(trading_status_rows),
            "corporate_actions": self._fact_revision_summary(corporate_action_rows),
            "__data_revision_summary__": revision_summary.get(
                "__data_revision_summary__", {}
            ),
        }
        quantity_action_integrity: dict[str, object] = {
            "status": "not_required",
            "reason": "the request does not require quantity-class company actions",
        }
        if DataCapability.ACTIONS in required_capabilities:
            quantity_fixture = next(
                (
                    fixture
                    for fixture in normalized_fixtures
                    if fixture.capability == "quantity_action_coverage"
                ),
                None,
            )
            quantity_action_integrity = (
                {
                    "status": "complete",
                    "source": quantity_fixture.source,
                    "fixture_key": quantity_fixture.fixture_key,
                    "fixture_version": quantity_fixture.fixture_version,
                    "content_hash": quantity_fixture.content_hash,
                    "scope": quantity_fixture.scope,
                    "proof_summary": quantity_fixture.proof_summary,
                }
                if quantity_fixture is not None
                else {
                    "status": "unavailable",
                    "reason": "no approved quantity-action source or coverage proof is configured",
                }
            )
        pit_status = self.pit_status()
        if trading_status_rows:
            pit_status["trading_status"] = source_revisions["trading_status"]["pit_status"]
        elif trading_status_coverage:
            coverage_values = [
                item
                for values in trading_status_coverage.values()
                if isinstance(values, Sequence)
                for item in values
                if isinstance(item, Mapping)
            ]
            pit_status["trading_status"] = (
                "strict"
                if coverage_values and all(item.get("known_at") is not None for item in coverage_values)
                else "non_strict"
            )
        if corporate_action_rows:
            pit_status["corporate_actions"] = source_revisions["corporate_actions"]["pit_status"]
        elif corporate_action_coverage:
            coverage_values = [
                item
                for values in corporate_action_coverage.values()
                if isinstance(values, Sequence)
                for item in values
                if isinstance(item, Mapping)
            ]
            pit_status["corporate_actions"] = (
                "strict"
                if coverage_values and all(item.get("known_at") is not None for item in coverage_values)
                else "non_strict"
            )
        summary: dict[str, object] = {
            "provider_key": ETF_PROVIDER_KEY,
            "adapter_key": ETF_ADAPTER_KEY,
            "adapter_version": ETF_ADAPTER_VERSION,
            "validation_rule_version": ETF_VALIDATION_RULE_VERSION,
            "data_contract_version": 1,
            "capability": "bars",
            "frequency": "1d",
            "price_basis": PriceBasis.RAW.value,
            "strategy_price_bases": [basis.value for basis in requested_bases],
            "requested_range": expected_range,
            "run_kind": run_kind or (profile.run_kind if profile is not None else None),
            "preflight_profile": (
                {"key": profile.key, "version": profile.version}
                if profile is not None
                else None
            ),
            "quantity_action_integrity": quantity_action_integrity,
            "trading_status": build_trading_status_summary(
                ContractRef(key=ETF_RULE_PACKAGE[0], version=ETF_RULE_PACKAGE[1]),
                capability_declarations=trading_status_applicability,
                coverage=trading_status_coverage,
                source_revisions=source_revisions.get("trading_status"),
                **(
                    {"limitation": trading_status_limitation}
                    if trading_status_limitation is not None
                    else {}
                ),
            ),
            "fixtures": [fixture.as_dict() for fixture in normalized_fixtures],
            "lookback_sessions": lookback_sessions,
            "max_lookback_sessions": max_lookback_sessions,
            "expected_sessions": len(expected_sessions),
            "returned_sessions": sum(
                len(set(bars_by_instrument.get(instrument_id, ())))
                for instrument_id in instrument_ids
            ),
            "adjustment_series_policy": policy_payload,
            "policy_status": policy_payload.get("status"),
            "adjustment_policy_status": policy_payload.get("status"),
            "adjustment_adapter_version": policy_payload.get("adapter_version"),
            "formula_version": policy_payload.get("formula_version"),
            "adjustment_formula_version": policy_payload.get("formula_version"),
            "qfq_anchor": policy_payload.get("qfq_anchor"),
            "adjustment_qfq_anchor": policy_payload.get("qfq_anchor"),
            "hfq_anchor": policy_payload.get("hfq_anchor"),
            "adjustment_hfq_anchor": policy_payload.get("hfq_anchor"),
            "factor_cutoff_rule": policy_payload.get("cutoff_rule"),
            "adjustment_factor_cutoff_rule": policy_payload.get("cutoff_rule"),
            "verification_input_hash": policy_payload.get("verification_input_hash"),
            "adjustment_verification_input_hash": policy_payload.get("verification_input_hash"),
            "verification_output_hash": policy_payload.get("verification_output_hash"),
            "adjustment_verification_output_hash": policy_payload.get("verification_output_hash"),
            "verification_evidence_hash": policy_payload.get("verification_evidence_hash"),
            "adjustment_verification_evidence_hash": policy_payload.get("verification_evidence_hash"),
            "factor_coverage": factor_coverage,
            "adjustment_factor_coverage": factor_coverage,
            "research_price_coverage": research_price_coverage,
            "cutoff_boundary": cutoff_boundary,
            # Audit evidence for why the adjustment policy may be active:
            # absent evidence with an active policy is a construction error.
            "adjustment_series_validation": {
                "active": self.adjustment_policy.is_active(),
                "policy_key": self.adjustment_policy.key,
                "policy_version": self.adjustment_policy.version,
                "adapter_version": self.adjustment_policy.adapter_version,
                "verification_evidence": self.adjustment_policy.verification_summary,
                "verification_status": self.adjustment_policy.verification_status,
                "verification_published": self.adjustment_policy.verification_published,
                "verification_input_hash": self.adjustment_policy.verification_input_hash,
                "verification_output_hash": self.adjustment_policy.verification_output_hash,
                "verification_evidence_hash": self.adjustment_policy.verification_evidence_hash,
                "factor_cutoff_rule": self.adjustment_policy.cutoff_rule,
            },
            "pit_status": pit_status,
            "instrument_mapping_summary": mapping_summary,
            "coverage": {
                "daily_bars": {
                    str(instrument_id): self.coverage_summary(
                        expected_sessions, bars_by_instrument.get(instrument_id, ())
                    )
                    for instrument_id in instrument_ids
                },
                **(
                    {"adjusted_series": {"coverage": factor_coverage}}
                    if factor_coverage
                    else {}
                ),
                **(
                    {"research_prices": {"coverage": research_price_coverage}}
                    if research_price_coverage
                    else {}
                ),
                **(
                    {"trading_status": trading_status_coverage}
                    if trading_status_coverage is not None
                    else {}
                ),
                **(
                    {"corporate_actions": corporate_action_coverage}
                    if corporate_action_coverage is not None
                    else {}
                ),
            },
            "mapping_segments": mapping_summary,
            "source_revisions": source_revisions,
            "invalid_bars": invalid_bars,
            "bar_validity_summary": {
                "adapter_key": ETF_ADAPTER_KEY,
                "adapter_version": ETF_ADAPTER_VERSION,
                "rule_key": ETF_VALIDATION_RULE_KEY,
                "rule_version": ETF_VALIDATION_RULE_VERSION,
                "invalid_count": len({
                    (item.get("instrument_id"), item.get("trade_date"))
                    for item in invalid_bars
                }),
                "invalid_field_count": len(invalid_bars),
                "blocked": bool(invalid_bars),
                "invalid_bars": invalid_bars,
            },
            "issues": issues_payload,
        }
        coverage_values = [
            self.coverage_summary(expected_sessions, bars_by_instrument.get(instrument_id, ()))
            for instrument_id in instrument_ids
        ]
        summary["status"] = (
            "blocked"
            if invalid_bars
            or issues_payload
            or any(item["status"] != "complete" for item in coverage_values)
            else "ready"
        )
        sanitized = _redact_sensitive(summary)
        if not isinstance(sanitized, dict):  # pragma: no cover - defensive
            raise ProviderContractViolationError(
                "preflight summary must remain a JSON object"
            )
        summary = sanitized
        summary["report_hash"] = canonical_hash(_summary_hash_content(summary))
        return summary


def build_data_preflight_payloads(
    summary: Mapping[str, object],
) -> dict[str, object]:
    """Map an adapter preflight summary onto the persisted record fields.

    The existing ``backtest_data_preflight`` record already carries JSON
    payload columns; no new ETF-specific table or column is created.  The
    mapping summaries ride inside the ``coverage`` payload.
    """

    pit_status = summary.get("pit_status")
    pit_value = ""
    if isinstance(pit_status, Mapping):
        # Only families explicitly declared "non_strict" trip the run-level
        # flag.  Policy markers such as
        # "tushare_adj_factor_native@1:effective_date_cutoff" are contract
        # declarations, not missing knowledge-time evidence: the approved
        # first-version factor contract never triggers non_strict_pit.
        non_strict_families = sorted(
            family
            for family, value in pit_status.items()
            if value == "non_strict"
        )
        pit_value = (
            "strict"
            if not non_strict_families
            else f"non_strict:{','.join(non_strict_families)}"
        )
    coverage_payload: dict[str, object] = dict(
        summary.get("coverage") or {}
    )
    # Audit summaries ride the coverage payload of the existing record so
    # /data-preflight can explain which code mappings were used and on
    # what evidence the adjustment policy was activated.
    if summary.get("instrument_mapping_summary"):
        coverage_payload["instrument_mapping_summary"] = summary[
            "instrument_mapping_summary"
        ]
    if summary.get("adjustment_series_validation"):
        coverage_payload["adjustment_series_validation"] = summary[
            "adjustment_series_validation"
        ]
    if summary.get("trading_status"):
        coverage_payload["trading_status"] = summary["trading_status"]
    if summary.get("quantity_action_integrity"):
        coverage_payload["quantity_action_integrity"] = summary[
            "quantity_action_integrity"
        ]
    for field in ("bar_validity_summary", "invalid_bars"):
        if summary.get(field) is not None:
            coverage_payload[field] = summary[field]
    issues = summary.get("issues", [])
    failure_reason = None
    if issues:
        first = issues[0]
        failure_reason = str(first.get("code", "history_incomplete"))
    elif summary.get("invalid_bars"):
        first_invalid = summary["invalid_bars"][0]
        failure_reason = str(first_invalid.get("code", "bar_invalid"))
    # Keep the full adjustment contract in the existing capabilities JSON so
    # admission records and later run reads expose the same machine evidence
    # as the preflight summary.  Do not copy arbitrary summary keys: this
    # explicit allow-list prevents credentials or raw source payloads from
    # leaking into persistence.
    adjustment_fields = {
        "adjustment_series_policy": summary.get("adjustment_series_policy"),
        "policy_status": summary.get("policy_status"),
        "adjustment_policy_status": summary.get("adjustment_policy_status"),
        # ``adapter_version`` is already an existing generic capability key;
        # preserve that value below and expose the adjustment-specific value
        # under an explicit alias as well.
        "adjustment_adapter_version": summary.get("adjustment_adapter_version"),
        "formula_version": summary.get("formula_version"),
        "adjustment_formula_version": summary.get("adjustment_formula_version"),
        "qfq_anchor": summary.get("qfq_anchor"),
        "adjustment_qfq_anchor": summary.get("adjustment_qfq_anchor"),
        "hfq_anchor": summary.get("hfq_anchor"),
        "adjustment_hfq_anchor": summary.get("adjustment_hfq_anchor"),
        "factor_cutoff_rule": summary.get("factor_cutoff_rule"),
        "adjustment_factor_cutoff_rule": summary.get("adjustment_factor_cutoff_rule"),
        "verification_input_hash": summary.get("verification_input_hash"),
        "adjustment_verification_input_hash": summary.get("adjustment_verification_input_hash"),
        "verification_output_hash": summary.get("verification_output_hash"),
        "adjustment_verification_output_hash": summary.get("adjustment_verification_output_hash"),
        "verification_evidence_hash": summary.get("verification_evidence_hash"),
        "adjustment_verification_evidence_hash": summary.get("adjustment_verification_evidence_hash"),
        "factor_coverage": summary.get("factor_coverage"),
        "adjustment_factor_coverage": summary.get("adjustment_factor_coverage"),
        "research_price_coverage": summary.get("research_price_coverage"),
        "cutoff_boundary": summary.get("cutoff_boundary"),
    }
    return {
        "capabilities": {
            "provider_key": summary.get("provider_key"),
            "data_contract_version": summary.get("data_contract_version"),
            "adapter_key": summary.get("adapter_key"),
            "adapter_version": summary.get("adapter_version"),
            "validation_rule_version": summary.get("validation_rule_version"),
            **adjustment_fields,
        },
        "coverage": coverage_payload,
        "pit_status": pit_value,
        "source_revisions": summary.get("source_revisions"),
        "session_summary": {
            "failure_reason": failure_reason,
        },
    }
