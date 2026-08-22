"""Request layering, shared value objects, and point-in-time query DTOs.

This module owns the boundary between an *unresolved run intent*
(:class:`DataPreflightRequest`) and a *frozen official run request*
(:class:`DataRequest`).  Callers never choose the final ``calendar_ids`` or
``timezone`` themselves: preflight resolves them and the frozen
:class:`DataRequest` carries only resolved values.

The first data-contract version pins ``data_contract_version = 1``,
``max_lookback_sessions = 512``, ``engine_price_basis = raw``, the
``strict_compatible@1`` calendar-axis policy, and the
``fixed_trading_sessions@1`` chunk policy with 20 sessions per chunk.

Nothing here imports ORM, database session, FastAPI, or Tushare types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Iterable
from uuid import UUID

from app.backtesting.data.errors import (
    DataCutoffExceededError,
    DataPreflightBlockedError,
    DataPreflightConfirmationMismatchError,
    InvalidDataRequestError,
    LookbackSessionsLimitExceededError,
)
from app.backtesting.domain import _aware_datetime
from app.instruments.domain import VersionedReference

__all__ = [
    "CALENDAR_AXIS_POLICY",
    "CHUNK_POLICY",
    "DATA_CONTRACT_VERSION",
    "MAX_LOOKBACK_SESSIONS",
    "AdjustedSeriesQuery",
    "BarQuery",
    "ConsistencyMode",
    "ContractRef",
    "CorporateActionQuery",
    "CoverageQuery",
    "DataCapability",
    "DataChunkQuery",
    "DataPreflightRequest",
    "DataRequest",
    "DataValueQuery",
    "DateRange",
    "EffectiveDateRange",
    "InstrumentMappingQuery",
    "InstrumentQuery",
    "InstrumentScopeMode",
    "IssueSeverity",
    "LookbackWindow",
    "MarketScope",
    "PitSupport",
    "PriceBasis",
    "QualityMode",
    "QualityStatus",
    "QueryBoundary",
    "TickQuery",
    "TradingRuleQuery",
    "TradingStatusQuery",
    "UniverseQuery",
    "UniverseQueryPolicy",
]


# ---------------------------------------------------------------------------
# Version-pinned constants (single source of truth)
# ---------------------------------------------------------------------------

DATA_CONTRACT_VERSION = 1
"""Machine version of the generic data contract implemented by this package."""

MAX_LOOKBACK_SESSIONS = 512
"""First-version maximum for one lookback window, in trading sessions.

The limit is enforced before any data is read.  This constant is re-exported
by ``app.strategy_protocol.contract``; it must be defined exactly once.
"""

CALENDAR_AXIS_POLICY = VersionedReference(key="strict_compatible", version=1)
"""The only calendar-axis policy allowed in data-contract version 1."""

CHUNK_POLICY = VersionedReference(key="fixed_trading_sessions", version=1)
"""The only chunk policy allowed in data-contract version 1."""


# Versioned policy references are reused from the canonical instrument domain
# so that ``key``/``version`` has exactly one implementation everywhere.
ContractRef = VersionedReference


# ---------------------------------------------------------------------------
# Stable enums (values are machine identifiers; never rename them)
# ---------------------------------------------------------------------------


class QualityStatus(StrEnum):
    """Fact-level quality outcome for one requested slice of data."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class PreflightStatus(StrEnum):
    """Overall admission-preflight outcome."""

    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class IssueSeverity(StrEnum):
    """Severity of one structured preflight issue."""

    WARNING = "warning"
    ERROR = "error"


class InstrumentScopeMode(StrEnum):
    """How the set of tradable instruments is determined for a run."""

    FIXED = "fixed"
    DYNAMIC = "dynamic"
    HYBRID = "hybrid"


class PriceBasis(StrEnum):
    """Which price series a query or fact refers to."""

    RAW = "raw"
    QFQ = "qfq"
    HFQ = "hfq"


class ConsistencyMode(StrEnum):
    """How a provider guarantees read consistency inside one session."""

    CHUNKED_LOGICAL_TOKEN = "chunked_logical_token"
    TRANSITIONAL_REPEATABLE_READ = "transitional_repeatable_read"


class ConsistencyValidation(StrEnum):
    """Lifecycle state of a chunk consistency validation."""

    NOT_VALIDATED = "not_validated"
    VALID = "valid"
    INVALID = "invalid"
    EXPIRED = "expired"
    COVERAGE_INCOMPLETE = "coverage_incomplete"


class PitSupport(StrEnum):
    """Point-in-time support level declared per fact capability."""

    STRICT = "strict"
    NON_STRICT = "non_strict"
    UNAVAILABLE = "unavailable"


class QualityMode(StrEnum):
    """Overall run-level data-quality policy."""

    STRICT = "strict"


class DataCapability(StrEnum):
    """Fact capabilities a provider may declare or a query may request."""

    BARS = "bars"
    RULES = "rules"
    STATUS = "status"
    ACTIONS = "actions"
    UNIVERSE = "universe"
    COVERAGE = "coverage"
    MAPPINGS = "mappings"
    CALENDARS = "calendars"
    ADJUSTED_SERIES = "adjusted_series"
    TICKS = "ticks"
    VALUES = "values"


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _plain_date(value: object, field_name: str) -> date:
    """Require a calendar date and reject full datetimes."""

    if not isinstance(value, date) or isinstance(value, datetime):
        raise InvalidDataRequestError(f"{field_name} must be a datetime.date")
    return value


def _strict_int(value: object, field_name: str) -> int:
    """Require a plain integer; booleans are not integers in this contract."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataRequestError(f"{field_name} must be an integer")
    return value


def _non_blank_text(value: object, field_name: str) -> str:
    """Require non-blank plain text."""

    if not isinstance(value, str) or not value.strip():
        raise InvalidDataRequestError(f"{field_name} must be non-blank text")
    return value


def _sorted_unique_ids(
    value: UUID | Iterable[UUID], field_name: str
) -> tuple[UUID, ...]:
    """Accept one id or an iterable; deduplicate by stable string form."""

    if isinstance(value, UUID):
        return (value,)
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(f"{field_name} must be a UUID or iterable")
    ids = tuple(value)
    if not ids:
        raise InvalidDataRequestError(f"{field_name} must not be empty")
    for instrument_id in ids:
        if not isinstance(instrument_id, UUID):
            raise InvalidDataRequestError(f"{field_name} entries must be UUIDs")
    return tuple(sorted(set(ids), key=str))


def _sorted_unique_refs(
    value: Iterable[ContractRef], field_name: str
) -> tuple[ContractRef, ...]:
    """Deduplicate versioned references and sort them by (key, version)."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(
            f"{field_name} must be an iterable of ContractRef"
        )
    refs = tuple(value)
    for ref in refs:
        if not isinstance(ref, ContractRef):
            raise InvalidDataRequestError(f"{field_name} entries must be ContractRef")
    return tuple(sorted(set(refs), key=lambda ref: (ref.key, ref.version)))


def _sorted_unique_text(
    value: Iterable[str], field_name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    """Deduplicate non-blank machine labels and sort them stably."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(f"{field_name} must be an iterable of strings")
    labels = tuple(_non_blank_text(item, f"{field_name} entry") for item in value)
    if not labels:
        if allow_empty:
            return ()
        raise InvalidDataRequestError(f"{field_name} must not be empty")
    return tuple(sorted(set(labels)))


def _sorted_unique_enum(
    value: Iterable, enum_type, field_name: str, *, allow_empty: bool = False
) -> tuple:
    """Deduplicate enum members and sort them by their stable value."""

    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InvalidDataRequestError(
            f"{field_name} must be an iterable of {enum_type.__name__}"
        )
    members = tuple(value)
    for member in members:
        if not isinstance(member, enum_type):
            raise InvalidDataRequestError(
                f"{field_name} entries must be {enum_type.__name__} members"
            )
    if not members:
        if allow_empty:
            return ()
        raise InvalidDataRequestError(f"{field_name} must not be empty")
    return tuple(sorted(set(members), key=lambda member: member.value))


# ---------------------------------------------------------------------------
# Time windows and query boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DateRange:
    """An inclusive calendar-date window: both endpoints are contained."""

    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        start = _plain_date(self.start_date, "start_date")
        end = _plain_date(self.end_date, "end_date")
        if start > end:
            raise InvalidDataRequestError("start_date must not be later than end_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)


@dataclass(frozen=True, slots=True)
class EffectiveDateRange:
    """A half-open validity interval ``[valid_from, valid_to)``.

    ``valid_to=None`` means the interval is still open-ended.  Identity
    mappings, rules, and trading-status facts all use this semantics.
    """

    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        start = _plain_date(self.valid_from, "valid_from")
        if self.valid_to is not None:
            end = _plain_date(self.valid_to, "valid_to")
            if end <= start:
                raise InvalidDataRequestError(
                    "valid_to must be later than valid_from (half-open interval)"
                )
            object.__setattr__(self, "valid_to", end)
        object.__setattr__(self, "valid_from", start)

    def covers(self, day: date) -> bool:
        """Whether ``day`` falls inside the half-open interval."""

        day = _plain_date(day, "day")
        if day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to


@dataclass(frozen=True, slots=True)
class LookbackWindow:
    """A session-count window ending at one explicit cutoff instant.

    ``sessions`` must be between 1 and 512 inclusive; exceeding the limit
    fails before any data is read.  ``end_at`` must be timezone-aware.
    """

    sessions: int
    end_at: datetime

    def __post_init__(self) -> None:
        sessions = _strict_int(self.sessions, "sessions")
        if sessions <= 0:
            raise InvalidDataRequestError("sessions must be a positive integer")
        if sessions > MAX_LOOKBACK_SESSIONS:
            raise LookbackSessionsLimitExceededError(
                f"sessions {sessions} exceeds the maximum of "
                f"{MAX_LOOKBACK_SESSIONS}",
                details={"requested": sessions, "maximum": MAX_LOOKBACK_SESSIONS},
            )
        object.__setattr__(self, "sessions", sessions)
        object.__setattr__(self, "end_at", _aware_datetime(self.end_at, "end_at"))


@dataclass(frozen=True, slots=True)
class QueryBoundary:
    """The point-in-time visibility boundary carried by market-fact queries.

    ``data_cutoff`` is mandatory: no query may read facts recorded after it,
    and overflow fails instead of silently trimming.  Strict historical
    cognition additionally supplies ``knowledge_as_of``, which must not be
    later than ``data_cutoff``.  Both timestamps must be timezone-aware.

    Date-window queries need an explicit rule for the cutoff day itself,
    because a bare ``data_cutoff`` instant cannot tell whether the day's
    facts are already complete.  ``include_cutoff_day`` encodes that
    decision: the default ``False`` means the cutoff day counts as
    incomplete and any date window touching it fails; ``True`` means the
    caller has established that the whole cutoff day is readable.
    """

    data_cutoff: datetime
    knowledge_as_of: datetime | None = None
    include_cutoff_day: bool = False

    def __post_init__(self) -> None:
        cutoff = _aware_datetime(self.data_cutoff, "data_cutoff")
        object.__setattr__(self, "data_cutoff", cutoff)
        if not isinstance(self.include_cutoff_day, bool):
            raise InvalidDataRequestError(
                "include_cutoff_day must be a boolean"
            )
        if self.knowledge_as_of is not None:
            known = _aware_datetime(self.knowledge_as_of, "knowledge_as_of")
            if known > cutoff:
                raise InvalidDataRequestError(
                    "knowledge_as_of must not be later than data_cutoff"
                )
            object.__setattr__(self, "knowledge_as_of", known)

    @property
    def cutoff_date(self) -> date:
        """The latest calendar date a query may touch."""

        return self.data_cutoff.date()

    def require_not_past_cutoff(self, day: date, field_name: str) -> None:
        """Reject a date beyond the boundary instead of trimming it.

        A date strictly after ``data_cutoff``'s day always fails.  The
        cutoff day itself fails unless ``include_cutoff_day`` declares the
        whole day complete.
        """

        day = _plain_date(day, field_name)
        if day > self.cutoff_date:
            raise DataCutoffExceededError(
                f"{field_name} {day.isoformat()} is later than data_cutoff "
                f"{self.data_cutoff.isoformat()}",
                details={
                    "requested": day.isoformat(),
                    "data_cutoff": self.data_cutoff.isoformat(),
                },
            )
        if (
            day == self.cutoff_date
            and not self.include_cutoff_day
        ):
            raise DataCutoffExceededError(
                f"{field_name} {day.isoformat()} touches the incomplete "
                f"cutoff day (data_cutoff {self.data_cutoff.isoformat()}); "
                "the caller must confirm whole-day completion via "
                "include_cutoff_day",
                details={
                    "requested": day.isoformat(),
                    "data_cutoff": self.data_cutoff.isoformat(),
                    "include_cutoff_day": False,
                },
            )

    def require_instant_not_past_cutoff(
        self, instant: datetime, field_name: str
    ) -> None:
        """Reject an aware instant later than ``data_cutoff``."""

        instant = _aware_datetime(instant, field_name)
        if instant > self.data_cutoff:
            raise DataCutoffExceededError(
                f"{field_name} {instant.isoformat()} is later than data_cutoff "
                f"{self.data_cutoff.isoformat()}",
                details={
                    "requested": instant.isoformat(),
                    "data_cutoff": self.data_cutoff.isoformat(),
                },
            )


# ---------------------------------------------------------------------------
# Market scope and universe policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketScope:
    """Immutable allow-list of markets, exchanges, asset classes, currencies.

    An empty tuple means "no restriction along that axis"; a non-empty tuple
    is deduplicated and stably sorted.  Free-form SQL, callbacks, and
    unvalidated dictionaries are deliberately not expressible here.
    """

    markets: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    asset_classes: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("markets", "exchanges", "asset_classes", "currencies"):
            raw = getattr(self, field_name)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
                raise InvalidDataRequestError(
                    f"{field_name} must be an iterable of strings"
                )
            labels = tuple(_non_blank_text(item, f"{field_name} entry") for item in raw)
            object.__setattr__(self, field_name, tuple(sorted(set(labels))))


@dataclass(frozen=True, slots=True)
class UniverseQueryPolicy:
    """Versioned candidate-set rules that bound dynamic universe queries.

    ``candidate_set_rules`` is empty exactly when the run has no dynamic
    candidate pool (fixed scope).  The rules are versioned references owned
    by the universe domain; this object never embeds rule bodies.
    """

    candidate_set_rules: tuple[ContractRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_set_rules",
            _sorted_unique_refs(self.candidate_set_rules, "candidate_set_rules"),
        )

    @property
    def has_candidate_rules(self) -> bool:
        """Whether any dynamic candidate-set rule is configured."""

        return bool(self.candidate_set_rules)


# ---------------------------------------------------------------------------
# DataPreflightRequest: the unresolved run intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataPreflightRequest:
    """A run's data intent before calendars and time zone are resolved.

    This object is what ``DataProvider.preflight()`` consumes.  It never
    contains resolved calendar ids, a resolved time zone, or session lists:
    those are preflight outputs frozen into :class:`DataRequest`.
    """

    provider_key: str
    requested_window: DateRange
    frequency: str
    rule_package: ContractRef
    market_scope: MarketScope
    universe_query_policy: UniverseQueryPolicy
    instrument_scope_mode: InstrumentScopeMode
    required_capabilities: tuple[DataCapability, ...]
    strategy_price_bases: tuple[PriceBasis, ...]
    consistency_mode: ConsistencyMode
    static_instrument_ids: tuple[UUID, ...] = ()
    mandatory_instrument_ids: tuple[UUID, ...] = ()
    warmup_sessions: int = 0
    rule_exception_set: ContractRef | None = None
    allowed_settlement_rule_class: str | None = None
    adjustment_series_policy: ContractRef | None = None
    consistency_token_contract: ContractRef | None = None
    knowledge_as_of: datetime | None = None
    data_contract_version: int = DATA_CONTRACT_VERSION
    max_lookback_sessions: int = MAX_LOOKBACK_SESSIONS
    calendar_axis_policy: ContractRef = CALENDAR_AXIS_POLICY
    engine_price_basis: PriceBasis = PriceBasis.RAW
    quality_mode: QualityMode = QualityMode.STRICT
    data_chunk_policy: ContractRef = CHUNK_POLICY
    data_chunk_size_sessions: int = 20

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_key", _non_blank_text(self.provider_key, "provider_key")
        )
        if not isinstance(self.requested_window, DateRange):
            raise InvalidDataRequestError("requested_window must be a DateRange")
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        if not isinstance(self.rule_package, ContractRef):
            raise InvalidDataRequestError("rule_package must be a ContractRef")
        if not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        if not isinstance(self.universe_query_policy, UniverseQueryPolicy):
            raise InvalidDataRequestError(
                "universe_query_policy must be a UniverseQueryPolicy"
            )
        if not isinstance(self.instrument_scope_mode, InstrumentScopeMode):
            raise InvalidDataRequestError(
                "instrument_scope_mode must be an InstrumentScopeMode"
            )
        object.__setattr__(
            self,
            "required_capabilities",
            _sorted_unique_enum(
                self.required_capabilities, DataCapability, "required_capabilities"
            ),
        )
        object.__setattr__(
            self,
            "strategy_price_bases",
            _sorted_unique_enum(
                self.strategy_price_bases, PriceBasis, "strategy_price_bases"
            ),
        )
        if not isinstance(self.consistency_mode, ConsistencyMode):
            raise InvalidDataRequestError("consistency_mode must be a ConsistencyMode")
        if self.static_instrument_ids:
            object.__setattr__(
                self,
                "static_instrument_ids",
                _sorted_unique_ids(self.static_instrument_ids, "static_instrument_ids"),
            )
        if self.mandatory_instrument_ids:
            object.__setattr__(
                self,
                "mandatory_instrument_ids",
                _sorted_unique_ids(
                    self.mandatory_instrument_ids, "mandatory_instrument_ids"
                ),
            )
        warmup = _strict_int(self.warmup_sessions, "warmup_sessions")
        if warmup < 0:
            raise InvalidDataRequestError("warmup_sessions must not be negative")
        object.__setattr__(self, "warmup_sessions", warmup)
        if self.rule_exception_set is not None and not isinstance(
            self.rule_exception_set, ContractRef
        ):
            raise InvalidDataRequestError(
                "rule_exception_set must be a ContractRef when provided"
            )
        if self.allowed_settlement_rule_class is not None:
            object.__setattr__(
                self,
                "allowed_settlement_rule_class",
                _non_blank_text(
                    self.allowed_settlement_rule_class,
                    "allowed_settlement_rule_class",
                ),
            )
        if self.adjustment_series_policy is not None and not isinstance(
            self.adjustment_series_policy, ContractRef
        ):
            raise InvalidDataRequestError(
                "adjustment_series_policy must be a ContractRef when provided"
            )
        if self.consistency_token_contract is not None and not isinstance(
            self.consistency_token_contract, ContractRef
        ):
            raise InvalidDataRequestError(
                "consistency_token_contract must be a ContractRef when provided"
            )
        if self.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN and (
            self.consistency_token_contract is None
        ):
            raise InvalidDataRequestError(
                "chunked_logical_token consistency requires a "
                "consistency_token_contract"
            )
        if self.knowledge_as_of is not None:
            object.__setattr__(
                self,
                "knowledge_as_of",
                _aware_datetime(self.knowledge_as_of, "knowledge_as_of"),
            )
        self._validate_version_pinned_fields()
        self._validate_scope_consistency()

    def _validate_version_pinned_fields(self) -> None:
        """Enforce every value frozen by data-contract version 1."""

        version = _strict_int(self.data_contract_version, "data_contract_version")
        if version != DATA_CONTRACT_VERSION:
            raise InvalidDataRequestError(
                f"unsupported data_contract_version {version}; this package "
                f"implements version {DATA_CONTRACT_VERSION} only"
            )
        object.__setattr__(self, "data_contract_version", version)

        maximum = _strict_int(self.max_lookback_sessions, "max_lookback_sessions")
        if maximum > MAX_LOOKBACK_SESSIONS:
            raise LookbackSessionsLimitExceededError(
                f"max_lookback_sessions {maximum} exceeds the maximum of "
                f"{MAX_LOOKBACK_SESSIONS}",
                details={"requested": maximum, "maximum": MAX_LOOKBACK_SESSIONS},
            )
        # Version 1 freezes the run-level cap at exactly 512: callers can
        # neither raise nor lower it, mirroring strategy-side enforcement.
        if maximum != MAX_LOOKBACK_SESSIONS:
            raise InvalidDataRequestError(
                "data-contract version 1 fixes max_lookback_sessions to "
                f"{MAX_LOOKBACK_SESSIONS}"
            )
        object.__setattr__(self, "max_lookback_sessions", maximum)

        if self.calendar_axis_policy != CALENDAR_AXIS_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the strict_compatible@1 "
                "calendar axis policy"
            )
        if not isinstance(self.engine_price_basis, PriceBasis):
            raise InvalidDataRequestError("engine_price_basis must be a PriceBasis")
        if self.engine_price_basis is not PriceBasis.RAW:
            raise InvalidDataRequestError(
                "data-contract version 1 requires engine_price_basis=raw"
            )
        if not isinstance(self.quality_mode, QualityMode):
            raise InvalidDataRequestError("quality_mode must be a QualityMode")
        if self.quality_mode is not QualityMode.STRICT:
            raise InvalidDataRequestError(
                "data-contract version 1 requires quality_mode=strict"
            )
        if self.data_chunk_policy != CHUNK_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the fixed_trading_sessions@1 "
                "chunk policy"
            )
        chunk_size = _strict_int(
            self.data_chunk_size_sessions, "data_chunk_size_sessions"
        )
        if chunk_size != 20:
            raise InvalidDataRequestError(
                "data-contract version 1 fixes data_chunk_size_sessions to 20"
            )
        object.__setattr__(self, "data_chunk_size_sessions", chunk_size)

    def _validate_scope_consistency(self) -> None:
        """Enforce the fixed/dynamic/hybrid scope invariants."""

        has_fixed = bool(self.static_instrument_ids or self.mandatory_instrument_ids)
        has_dynamic = self.universe_query_policy.has_candidate_rules
        mode = self.instrument_scope_mode
        if mode is InstrumentScopeMode.FIXED:
            if not has_fixed:
                raise InvalidDataRequestError(
                    "fixed scope requires static or mandatory instrument ids"
                )
            if has_dynamic:
                raise InvalidDataRequestError(
                    "fixed scope must not configure dynamic candidate rules"
                )
        elif mode is InstrumentScopeMode.DYNAMIC:
            if not has_dynamic:
                raise InvalidDataRequestError(
                    "dynamic scope requires at least one candidate-set rule"
                )
        else:  # hybrid
            if not has_fixed or not has_dynamic:
                raise InvalidDataRequestError(
                    "hybrid scope requires fixed instruments and "
                    "candidate-set rules together"
                )


# ---------------------------------------------------------------------------
# DataRequest: the frozen official run request
# ---------------------------------------------------------------------------

_HASH_ALPHABET = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class DataRequest(DataPreflightRequest):
    """A preflight-frozen official run request.

    Everything from :class:`DataPreflightRequest` is inherited unchanged;
    the resolved calendar axis, time zone, and admission evidence are added
    by preflight and can never be overridden by the caller afterwards.
    """

    resolved_calendar_ids: tuple[str, ...] = ()
    resolved_timezone: str = ""
    admission_calendar_session_signature: str = ""
    admission_preflight_status: PreflightStatus = PreflightStatus.READY
    admission_preflight_hash: str = ""
    accepted_degraded_preflight_hash: str | None = None

    def __post_init__(self) -> None:
        # Explicit parent call: zero-arg super() breaks inside __post_init__
        # because dataclass(slots=True) recreates the class object.
        DataPreflightRequest.__post_init__(self)
        object.__setattr__(
            self,
            "resolved_calendar_ids",
            _sorted_unique_text(self.resolved_calendar_ids, "resolved_calendar_ids"),
        )
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        timezone = _non_blank_text(self.resolved_timezone, "resolved_timezone")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidDataRequestError(
                "resolved_timezone must be a resolvable IANA time-zone name"
            ) from exc
        object.__setattr__(self, "resolved_timezone", timezone)
        signature = _non_blank_text(
            self.admission_calendar_session_signature,
            "admission_calendar_session_signature",
        )
        object.__setattr__(self, "admission_calendar_session_signature", signature)
        if not isinstance(self.admission_preflight_status, PreflightStatus):
            raise InvalidDataRequestError(
                "admission_preflight_status must be a PreflightStatus"
            )
        admission_hash = _non_blank_text(
            self.admission_preflight_hash, "admission_preflight_hash"
        )
        if len(admission_hash) != 64 or any(
            character not in _HASH_ALPHABET for character in admission_hash
        ):
            raise InvalidDataRequestError(
                "admission_preflight_hash must be a lowercase SHA-256 hex digest"
            )
        object.__setattr__(self, "admission_preflight_hash", admission_hash)
        if self.admission_preflight_status is PreflightStatus.BLOCKED:
            raise DataPreflightBlockedError(
                "a blocked preflight report cannot be admitted into a DataRequest",
                details={"admission_preflight_hash": admission_hash},
            )
        if self.admission_preflight_status is PreflightStatus.DEGRADED:
            accepted = self.accepted_degraded_preflight_hash
            if not isinstance(accepted, str) or accepted != admission_hash:
                raise DataPreflightConfirmationMismatchError(
                    "a degraded admission requires the user-confirmed hash to "
                    "equal the preflight report hash",
                    details={
                        "admission_preflight_hash": admission_hash,
                        "accepted_degraded_preflight_hash": (
                            accepted if isinstance(accepted, str) else None
                        ),
                    },
                )
        elif self.accepted_degraded_preflight_hash is not None:
            raise InvalidDataRequestError(
                "accepted_degraded_preflight_hash must be None unless the "
                "admission preflight is degraded"
            )

    @classmethod
    def from_admission(
        cls,
        request: "DataPreflightRequest",
        report: "DataPreflightReport",
        *,
        accepted_degraded: bool = False,
    ) -> "DataRequest":
        """Freeze an official run request from a request/report pair.

        This is the only sanctioned admission path: it verifies that the
        report actually describes *this* request (provider, window,
        frequency, market scope, universe rules, rule package, capabilities,
        consistency and chunking semantics), rejects blocked reports,
        enforces the degraded-confirmation rule, and copies the resolved
        calendar axis out of the report.  The report hash covers the full
        request semantics plus the report status, so the stored
        ``admission_preflight_hash`` binds everything a later audit needs.
        """

        from dataclasses import fields

        from app.backtesting.data.errors import InvalidDataRequestError as _Invalid
        from app.backtesting.data.reports import DataPreflightReport  # noqa: F401

        if not isinstance(request, DataPreflightRequest):
            raise _Invalid("request must be a DataPreflightRequest")
        if not isinstance(report, DataPreflightReport):
            raise _Invalid("report must be a DataPreflightReport")
        if report.status is PreflightStatus.BLOCKED:
            raise DataPreflightBlockedError(
                "a blocked preflight report cannot be admitted",
                details={"admission_preflight_hash": report.report_hash},
            )
        # Bind the report to the request: every shared semantic must match
        # exactly, so a report generated for another intent can never be
        # replayed against this one.
        mismatches = []
        for field in fields(DataPreflightRequest):
            name = field.name
            report_name = {
                "instrument_scope_mode": "scope_mode",
                "warmup_sessions": "warmup_sessions_count",
            }.get(name, name)
            if getattr(report, report_name) != getattr(request, name):
                mismatches.append(name)
        if mismatches:
            raise _Invalid(
                "preflight report does not belong to this request",
                details={"mismatched_fields": sorted(mismatches)},
            )
        if report.status is PreflightStatus.DEGRADED:
            if not accepted_degraded:
                raise DataPreflightConfirmationMismatchError(
                    "admitting a degraded preflight requires explicit "
                    "user confirmation of its hash",
                    details={"admission_preflight_hash": report.report_hash},
                )
            confirmed_hash: str | None = report.report_hash
        else:
            if accepted_degraded:
                raise _Invalid(
                    "accepted_degraded is only valid for degraded reports"
                )
            confirmed_hash = None
        return cls(
            **{field.name: getattr(request, field.name) for field in fields(DataPreflightRequest)},
            resolved_calendar_ids=report.resolved_calendar_ids,
            resolved_timezone=report.resolved_timezone or "",
            admission_calendar_session_signature=report.calendar_session_signature,
            admission_preflight_status=report.status,
            admission_preflight_hash=report.report_hash,
            accepted_degraded_preflight_hash=confirmed_hash,
        )


# ---------------------------------------------------------------------------
# Point-in-time query DTOs (strongly typed; no dicts, no **kwargs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentQuery:
    """Resolve full instrument specs for one or more stable identities."""

    instrument_ids: UUID | tuple[UUID, ...]
    effective_at: datetime
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(
            self, "effective_at", _aware_datetime(self.effective_at, "effective_at")
        )
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_instant_not_past_cutoff(
            self.effective_at, "effective_at"
        )


@dataclass(frozen=True, slots=True)
class InstrumentMappingQuery:
    """Query evidenced PIT source-code mappings for a window."""

    instrument_ids: UUID | tuple[UUID, ...]
    source: str
    window: DateRange
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(self, "source", _non_blank_text(self.source, "source"))
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class TradingRuleQuery:
    """Query trading-rule facts valid inside a window."""

    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary
    rule_class: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        if self.rule_class is not None:
            object.__setattr__(
                self, "rule_class", _non_blank_text(self.rule_class, "rule_class")
            )
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class TradingStatusQuery:
    """Query trading-status facts (suspensions and the like) for a window."""

    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class UniverseQuery:
    """Resolve the candidate universe under frozen rules and market scope.

    The rule reference and market scope come from the frozen request, so
    callers cannot widen the candidate pool at query time.
    """

    rule: ContractRef
    market_scope: MarketScope
    effective_date: date
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.rule, ContractRef):
            raise InvalidDataRequestError("rule must be a ContractRef")
        if not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        effective = _plain_date(self.effective_date, "effective_date")
        object.__setattr__(self, "effective_date", effective)
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(effective, "effective_date")


def _require_single_window(window: object, field_name: str) -> None:
    """Reject anything that is neither a DateRange nor a LookbackWindow."""

    if not isinstance(window, (DateRange, LookbackWindow)):
        raise InvalidDataRequestError(
            f"{field_name} must be a DateRange or a LookbackWindow, never both"
        )


@dataclass(frozen=True, slots=True)
class BarQuery:
    """Query bars for one window expressed either explicitly or as lookback.

    Exactly one of :class:`DateRange` or :class:`LookbackWindow` may be
    supplied; passing both shapes is a caller error.
    """

    instrument_ids: UUID | tuple[UUID, ...]
    frequency: str
    boundary: QueryBoundary
    window: DateRange | LookbackWindow

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        _require_single_window(self.window, "window")
        if isinstance(self.window, DateRange):
            self.boundary.require_not_past_cutoff(
                self.window.end_date, "window.end_date"
            )
        else:
            self.boundary.require_instant_not_past_cutoff(
                self.window.end_at, "window.end_at"
            )


@dataclass(frozen=True, slots=True)
class TickQuery:
    """Generic tick query boundary (first-version providers need not serve it).

    Only the common time bounds are defined here; the concrete minute/tick
    business capability is out of scope for this contract version.
    """

    instrument_ids: UUID | tuple[UUID, ...]
    start_at: datetime
    end_at: datetime
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        start = _aware_datetime(self.start_at, "start_at")
        end = _aware_datetime(self.end_at, "end_at")
        if start > end:
            raise InvalidDataRequestError("start_at must not be later than end_at")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_instant_not_past_cutoff(end, "end_at")


@dataclass(frozen=True, slots=True)
class DataValueQuery:
    """Generic named-series value query (first version defines bounds only).

    ``series`` is the machine name of the requested generic value series;
    ``frequency`` is optional because some series are not bar-shaped.
    """

    instrument_ids: UUID | tuple[UUID, ...]
    series: str
    boundary: QueryBoundary
    window: DateRange | LookbackWindow
    frequency: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(self, "series", _non_blank_text(self.series, "series"))
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        if self.frequency is not None:
            object.__setattr__(
                self, "frequency", _non_blank_text(self.frequency, "frequency")
            )
        _require_single_window(self.window, "window")
        if isinstance(self.window, DateRange):
            self.boundary.require_not_past_cutoff(
                self.window.end_date, "window.end_date"
            )
        else:
            self.boundary.require_instant_not_past_cutoff(
                self.window.end_at, "window.end_at"
            )


@dataclass(frozen=True, slots=True)
class AdjustedSeriesQuery:
    """Query an adjustment series with an explicitly chosen price basis."""

    instrument_ids: UUID | tuple[UUID, ...]
    frequency: str
    price_basis: PriceBasis
    boundary: QueryBoundary
    window: DateRange | LookbackWindow

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        if not isinstance(self.price_basis, PriceBasis):
            raise InvalidDataRequestError("price_basis must be a PriceBasis")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        _require_single_window(self.window, "window")
        if isinstance(self.window, DateRange):
            self.boundary.require_not_past_cutoff(
                self.window.end_date, "window.end_date"
            )
        else:
            self.boundary.require_instant_not_past_cutoff(
                self.window.end_at, "window.end_at"
            )


@dataclass(frozen=True, slots=True)
class CorporateActionQuery:
    """Query corporate actions for a window, optionally by action type."""

    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary
    action_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        if self.action_types:
            object.__setattr__(
                self,
                "action_types",
                _sorted_unique_text(self.action_types, "action_types"),
            )
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class CoverageQuery:
    """Ask a provider for a coverage report over one capability and window."""

    capability: DataCapability
    instrument_ids: UUID | tuple[UUID, ...]
    window: DateRange
    boundary: QueryBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.capability, DataCapability):
            raise InvalidDataRequestError("capability must be a DataCapability")
        object.__setattr__(
            self,
            "instrument_ids",
            _sorted_unique_ids(self.instrument_ids, "instrument_ids"),
        )
        if not isinstance(self.window, DateRange):
            raise InvalidDataRequestError("window must be a DateRange")
        if not isinstance(self.boundary, QueryBoundary):
            raise InvalidDataRequestError("boundary must be a QueryBoundary")
        self.boundary.require_not_past_cutoff(self.window.end_date, "window.end_date")


@dataclass(frozen=True, slots=True)
class DataChunkQuery:
    """Identify one fixed chunk of official sessions and its fact types.

    ``chunk_index`` starts at 0.  The first and last session identifiers
    refer to ``SessionPoint.session_id`` values of the resolved official
    sessions.  Computing chunk boundaries from the 20-session policy is
    deliberately not implemented in this contract version.
    """

    chunk_index: int
    first_session_id: str
    last_session_id: str
    fact_types: tuple[DataCapability, ...]

    def __post_init__(self) -> None:
        index = _strict_int(self.chunk_index, "chunk_index")
        if index < 0:
            raise InvalidDataRequestError("chunk_index must not be negative")
        object.__setattr__(self, "chunk_index", index)
        first = _non_blank_text(self.first_session_id, "first_session_id")
        last = _non_blank_text(self.last_session_id, "last_session_id")
        if first > last:
            raise InvalidDataRequestError(
                "first_session_id must not be later than last_session_id"
            )
        object.__setattr__(self, "first_session_id", first)
        object.__setattr__(self, "last_session_id", last)
        object.__setattr__(
            self,
            "fact_types",
            _sorted_unique_enum(self.fact_types, DataCapability, "fact_types"),
        )
