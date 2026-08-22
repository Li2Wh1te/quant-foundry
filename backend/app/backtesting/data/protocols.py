"""Capability manifests, consistency objects, and provider protocols.

Everything in this module is a synchronous, runtime-checkable contract:
``DataProvider`` performs admission preflight and opens authoritative
sessions, ``DataSession`` binds its own consistency context and opens
chunks, and ``DataChunkSession`` validates consistency once before serving
business queries.  ``open_session()``/``open_chunk()`` return objects that
are themselves context managers, so resource release is deterministic.

The public consistency surface exposes only the mode, token contract, and
a non-sensitive summary; raw tokens, database snapshot handles, access
keys, and credentials stay private inside providers and never appear in
serializable DTOs, logs, or results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from app.backtesting.calendar_axis import CalendarDefinition, SessionPoint
from app.backtesting.data.errors import (
    InvalidDataRequestError,
    LookbackSessionsLimitExceededError,
)
from app.backtesting.data.facts import (
    AdjustedSeriesPoint,
    Bar,
    CorporateAction,
    DataPoint,
    InstrumentCodeMapping,
    InstrumentSpec,
    Tick,
    TradingRule,
    TradingStatus,
)
from app.backtesting.data.reports import DataCoverageReport, DataPreflightReport
from app.backtesting.data.requests import (
    AdjustedSeriesQuery,
    BarQuery,
    ConsistencyMode,
    ConsistencyValidation,
    ContractRef,
    CorporateActionQuery,
    CoverageQuery,
    DATA_CONTRACT_VERSION,
    DataCapability,
    DataChunkQuery,
    DataPreflightRequest,
    DataRequest,
    DataValueQuery,
    InstrumentMappingQuery,
    InstrumentQuery,
    MAX_LOOKBACK_SESSIONS,
    PitSupport,
    PriceBasis,
    TickQuery,
    TradingRuleQuery,
    TradingStatusQuery,
    UniverseQuery,
    _aware_datetime,
    _non_blank_text,
    _plain_date,
    _sorted_unique_enum,
    _sorted_unique_refs,
    _sorted_unique_text,
)

__all__ = [
    "ConsistencyTokenStatus",
    "CoverageEnvelope",
    "DataCapabilityManifest",
    "DataChunkSession",
    "DataConsistencyContext",
    "DataConsistencyEvidence",
    "DataProvider",
    "DataSession",
]


# ---------------------------------------------------------------------------
# Capability manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataCapabilityManifest:
    """Static, structured declaration of one provider's capabilities.

    Every collection is deduplicated and stably sorted, PIT support is
    declared per fact capability instead of a single provider-wide boolean,
    and calendar support references the named-calendar definitions of the
    previous deliverable.  The manifest never carries run data, tokens, or
    materialized snapshot identifiers.
    """

    provider_key: str
    manifest_version: int
    data_contract_version: int
    supported_calendars: tuple[CalendarDefinition, ...]
    supported_calendar_axis_policies: tuple[ContractRef, ...]
    rule_packages: tuple[ContractRef, ...]
    rule_exception_sets: tuple[ContractRef, ...]
    supported_asset_classes: tuple[str, ...]
    supported_frequencies: tuple[str, ...]
    supported_price_bases: tuple[PriceBasis, ...]
    pit_support_by_capability: Mapping[DataCapability, PitSupport]
    consistency_modes: tuple[ConsistencyMode, ...]
    consistency_token_contracts: tuple[ContractRef, ...]
    supported_chunk_policies: tuple[ContractRef, ...]
    capabilities: tuple[DataCapability, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_key", _non_blank_text(self.provider_key, "provider_key")
        )
        if isinstance(self.manifest_version, bool) or not isinstance(
            self.manifest_version, int
        ) or self.manifest_version < 1:
            raise InvalidDataRequestError(
                "manifest_version must be a positive integer"
            )
        if isinstance(self.data_contract_version, bool) or not isinstance(
            self.data_contract_version, int
        ):
            raise InvalidDataRequestError(
                "data_contract_version must be an integer"
            )
        if self.data_contract_version != DATA_CONTRACT_VERSION:
            raise InvalidDataRequestError(
                f"unsupported data_contract_version "
                f"{self.data_contract_version}; this package implements "
                f"version {DATA_CONTRACT_VERSION} only"
            )
        calendars = tuple(self.supported_calendars)
        for definition in calendars:
            if not isinstance(definition, CalendarDefinition):
                raise InvalidDataRequestError(
                    "supported_calendars entries must be CalendarDefinition"
                )
        object.__setattr__(
            self,
            "supported_calendars",
            tuple(
                sorted(
                    set(calendars),
                    key=lambda d: (d.calendar_id, d.definition_version),
                )
            ),
        )
        object.__setattr__(
            self,
            "supported_calendar_axis_policies",
            _sorted_unique_refs(
                self.supported_calendar_axis_policies,
                "supported_calendar_axis_policies",
            ),
        )
        object.__setattr__(
            self, "rule_packages", _sorted_unique_refs(self.rule_packages, "rule_packages")
        )
        object.__setattr__(
            self,
            "rule_exception_sets",
            _sorted_unique_refs(self.rule_exception_sets, "rule_exception_sets"),
        )
        object.__setattr__(
            self,
            "supported_asset_classes",
            _sorted_unique_text(self.supported_asset_classes, "supported_asset_classes"),
        )
        object.__setattr__(
            self,
            "supported_frequencies",
            _sorted_unique_text(self.supported_frequencies, "supported_frequencies"),
        )
        object.__setattr__(
            self,
            "supported_price_bases",
            _sorted_unique_enum(
                self.supported_price_bases, PriceBasis, "supported_price_bases"
            ),
        )
        pit = self.pit_support_by_capability or {}
        if not isinstance(pit, Mapping):
            raise InvalidDataRequestError(
                "pit_support_by_capability must be a mapping"
            )
        normalized_pit: dict[DataCapability, PitSupport] = {}
        for capability, support in pit.items():
            if not isinstance(capability, DataCapability):
                raise InvalidDataRequestError(
                    "pit_support_by_capability keys must be DataCapability"
                )
            if not isinstance(support, PitSupport):
                raise InvalidDataRequestError(
                    "pit_support_by_capability values must be PitSupport"
                )
            normalized_pit[capability] = support
        object.__setattr__(
            self,
            "pit_support_by_capability",
            MappingProxyType(dict(sorted(normalized_pit.items(), key=lambda item: item[0].value))),
        )
        object.__setattr__(
            self,
            "consistency_modes",
            _sorted_unique_enum(
                self.consistency_modes, ConsistencyMode, "consistency_modes"
            ),
        )
        object.__setattr__(
            self,
            "consistency_token_contracts",
            _sorted_unique_refs(
                self.consistency_token_contracts, "consistency_token_contracts"
            ),
        )
        object.__setattr__(
            self,
            "supported_chunk_policies",
            _sorted_unique_refs(
                self.supported_chunk_policies, "supported_chunk_policies"
            ),
        )
        object.__setattr__(
            self,
            "capabilities",
            _sorted_unique_enum(self.capabilities, DataCapability, "capabilities"),
        )


# ---------------------------------------------------------------------------
# Consistency objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageEnvelope:
    """Non-sensitive bounding envelope of one chunk's query surface.

    The envelope records the bounded ranges a chunk token must stay
    interpretable within: the chunk's own official session span, the
    warmup history that first-decision lookbacks may reach back into,
    the run-level historical query envelope (capped by
    ``max_lookback_sessions``), and the dependency fact types the token
    declares.  It never carries row data, snapshot handles, tokens, or
    credentials -- only the shape of what may be queried.
    """

    chunk_first_session_date: date
    chunk_last_session_date: date
    fact_types: tuple[DataCapability, ...] = ()
    warmup_first_session_date: date | None = None
    warmup_session_count: int = 0
    lookback_envelope_sessions: int = MAX_LOOKBACK_SESSIONS

    def __post_init__(self) -> None:
        first = _plain_date(
            self.chunk_first_session_date, "chunk_first_session_date"
        )
        last = _plain_date(self.chunk_last_session_date, "chunk_last_session_date")
        if first > last:
            raise InvalidDataRequestError(
                "chunk_first_session_date must not be later than "
                "chunk_last_session_date"
            )
        object.__setattr__(self, "chunk_first_session_date", first)
        object.__setattr__(self, "chunk_last_session_date", last)
        for field_name in ("warmup_session_count", "lookback_envelope_sessions"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidDataRequestError(f"{field_name} must be a non-negative integer")
            object.__setattr__(self, field_name, value)
        if self.warmup_first_session_date is not None:
            warmup_first = _plain_date(
                self.warmup_first_session_date, "warmup_first_session_date"
            )
            # Strict precedence: a warmup session on the chunk's first
            # official day would make it a formal session.
            if warmup_first >= first:
                raise InvalidDataRequestError(
                    "warmup_first_session_date must precede "
                    "chunk_first_session_date strictly; warmup sessions are "
                    "never official sessions of the run"
                )
            object.__setattr__(self, "warmup_first_session_date", warmup_first)
        # The two warmup fields describe one range and must agree: a
        # positive count without a first date (or the reverse) would let
        # one field silently claim history the other does not bound.
        if (self.warmup_first_session_date is None) != (
            self.warmup_session_count == 0
        ):
            raise InvalidDataRequestError(
                "warmup_first_session_date and warmup_session_count must "
                "be set together: a positive warmup count requires a first "
                "date, and a first date requires a positive count"
            )
        if self.lookback_envelope_sessions > MAX_LOOKBACK_SESSIONS:
            raise LookbackSessionsLimitExceededError(
                f"lookback_envelope_sessions "
                f"{self.lookback_envelope_sessions} exceeds the maximum of "
                f"{MAX_LOOKBACK_SESSIONS}",
                details={
                    "requested": self.lookback_envelope_sessions,
                    "maximum": MAX_LOOKBACK_SESSIONS,
                },
            )
        object.__setattr__(
            self,
            "fact_types",
            _sorted_unique_enum(
                self.fact_types, DataCapability, "fact_types", allow_empty=True
            ),
        )

    def to_summary(self) -> Mapping[str, object]:
        """Deep-frozen JSON summary safe for results, logs, and evidence."""

        from app.backtesting.data.errors import freeze_json

        return freeze_json(
            {
                "chunk_first_session_date": self.chunk_first_session_date.isoformat(),
                "chunk_last_session_date": self.chunk_last_session_date.isoformat(),
                "warmup_first_session_date": (
                    self.warmup_first_session_date.isoformat()
                    if self.warmup_first_session_date is not None
                    else None
                ),
                "warmup_session_count": self.warmup_session_count,
                "lookback_envelope_sessions": self.lookback_envelope_sessions,
                "fact_types": [capability.value for capability in self.fact_types],
            },
            "coverage_envelope",
        )


@dataclass(frozen=True, slots=True)
class ConsistencyTokenStatus:
    """Outcome of one chunk consistency validation.

    A ``valid`` outcome never carries a failure reason; every failing state
    (including ``not_validated``) must explain itself.
    """

    status: ConsistencyValidation
    validated_at: datetime | None = None
    covered_chunk: int | None = None
    covered_fact_types: tuple[DataCapability, ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ConsistencyValidation):
            raise InvalidDataRequestError("status must be a ConsistencyValidation")
        if self.validated_at is not None:
            object.__setattr__(
                self,
                "validated_at",
                _aware_datetime(self.validated_at, "validated_at"),
            )
        if self.covered_chunk is not None:
            index = self.covered_chunk
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise InvalidDataRequestError(
                    "covered_chunk must be a non-negative integer"
                )
        object.__setattr__(
            self,
            "covered_fact_types",
            _sorted_unique_enum(
                self.covered_fact_types,
                DataCapability,
                "covered_fact_types",
                allow_empty=True,
            ),
        )
        reason = self.failure_reason
        if reason is not None and (
            not isinstance(reason, str) or not reason.strip()
        ):
            raise InvalidDataRequestError(
                "failure_reason must be non-blank text when provided"
            )
        if self.status is ConsistencyValidation.VALID:
            if reason is not None:
                raise InvalidDataRequestError(
                    "valid consistency must not carry a failure reason"
                )
        elif reason is None:
            raise InvalidDataRequestError(
                f"failing consistency status {self.status.value} requires a "
                "failure reason"
            )


@runtime_checkable
class DataConsistencyContext(Protocol):
    """Read-only public face of a session's internal consistency binding.

    Implementations keep their runtime handles (tokens, snapshot ids)
    private; only the mode, the versioned token contract, and a
    persistable non-sensitive summary are exposed here.
    """

    @property
    def mode(self) -> ConsistencyMode: ...

    @property
    def token_contract(self) -> ContractRef | None: ...

    @property
    def context_summary(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class DataConsistencyEvidence:
    """Persistable, non-sensitive evidence about one validated chunk.

    The evidence is mode-aware: under ``chunked_logical_token`` a logical
    token exists and must appear as an irreversible short digest; under
    ``transitional_repeatable_read`` there is no logical token, so
    ``token_digest`` must stay ``None`` instead of forcing adapters to
    fabricate one.  There is deliberately no field that could carry a raw
    token, credential, password, or secret.
    """

    chunk_index: int
    first_session_id: str
    last_session_id: str
    mode: ConsistencyMode
    validation_status: ConsistencyValidation
    fact_types: tuple[DataCapability, ...]
    coverage_summary: Mapping[str, object]
    token_digest: str | None = None
    validated_at: datetime | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        index = self.chunk_index
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise InvalidDataRequestError("chunk_index must be a non-negative integer")
        object.__setattr__(
            self,
            "first_session_id",
            _non_blank_text(self.first_session_id, "first_session_id"),
        )
        object.__setattr__(
            self,
            "last_session_id",
            _non_blank_text(self.last_session_id, "last_session_id"),
        )
        # Session ids are opaque stable labels: the protocol never claims
        # that lexicographic order equals chronological order, so ids such
        # as ``session-9`` / ``session-10`` are legal in either position
        # and must not be compared as strings.
        if not isinstance(self.mode, ConsistencyMode):
            raise InvalidDataRequestError("mode must be a ConsistencyMode")
        if not isinstance(self.validation_status, ConsistencyValidation):
            raise InvalidDataRequestError(
                "validation_status must be a ConsistencyValidation"
            )
        object.__setattr__(
            self,
            "fact_types",
            _sorted_unique_enum(self.fact_types, DataCapability, "fact_types"),
        )
        digest = self.token_digest
        if digest is not None:
            digest = _non_blank_text(digest, "token_digest")
            # The digest must be short hex text: it identifies the token
            # without ever revealing it.
            if len(digest) > 64 or any(c not in "0123456789abcdef" for c in digest):
                raise InvalidDataRequestError(
                    "token_digest must be lowercase hex text of at most 64 chars"
                )
            object.__setattr__(self, "token_digest", digest)
        if self.mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN:
            if digest is None:
                raise InvalidDataRequestError(
                    "chunked_logical_token evidence requires a token_digest"
                )
        elif digest is not None:
            raise InvalidDataRequestError(
                "transitional_repeatable_read has no logical token; "
                "token_digest must be None"
            )
        summary = self.coverage_summary or {}
        from app.backtesting.data.errors import freeze_json

        frozen_summary = freeze_json(dict(summary), "coverage_summary") if isinstance(
            summary, Mapping
        ) else None
        if frozen_summary is None:
            raise InvalidDataRequestError("coverage_summary must be a mapping")
        assert isinstance(frozen_summary, MappingProxyType)
        object.__setattr__(self, "coverage_summary", frozen_summary)
        if self.validated_at is not None:
            object.__setattr__(
                self,
                "validated_at",
                _aware_datetime(self.validated_at, "validated_at"),
            )
        reason = self.failure_reason
        if reason is not None and (
            not isinstance(reason, str) or not reason.strip()
        ):
            raise InvalidDataRequestError(
                "failure_reason must be non-blank text when provided"
            )
        if self.validation_status is ConsistencyValidation.VALID:
            if reason is not None:
                raise InvalidDataRequestError(
                    "valid consistency evidence must not carry a failure reason"
                )
        elif reason is None:
            raise InvalidDataRequestError(
                f"failing consistency status {self.validation_status.value} "
                "requires a failure reason"
            )


# ---------------------------------------------------------------------------
# Runtime protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class DataProvider(Protocol):
    """Structural source of capability, preflight, and session access."""

    def capability_manifest(self) -> DataCapabilityManifest:
        """Return the static structured capability manifest."""
        ...

    def preflight(self, request: DataPreflightRequest) -> DataPreflightReport:
        """Run the page/API admission preflight for an unresolved intent."""
        ...

    def open_session(self, request: DataRequest) -> "DataSession":
        """Open an authoritative session as its own context manager."""
        ...


@runtime_checkable
class DataSession(Protocol):
    """One authoritative read session bound to its own consistency context.

    ``open_chunk`` accepts only a :class:`DataChunkQuery`; callers can never
    pass back an externally built consistency context.  Business queries on
    chunks require a prior successful ``validate_consistency()``.
    """

    def __enter__(self) -> "DataSession": ...
    def __exit__(self, exc_type, exc, traceback) -> bool | None: ...

    @property
    def resolved_sessions(self) -> tuple[SessionPoint, ...]:
        """Official sessions, fixed after a successful authoritative preflight."""
        ...

    @property
    def warmup_sessions(self) -> tuple[SessionPoint, ...]:
        """History sessions before the first formal session (task 02-02).

        They serve pre-first-decision history queries only: they never enter
        the formal timeline and never produce backtest business records.
        """
        ...

    @property
    def consistency_context(self) -> DataConsistencyContext:
        """The internally bound consistency context of this session."""
        ...

    def preflight(self, request: DataPreflightRequest) -> DataPreflightReport:
        """Authoritative in-subprocess preflight re-check.

        ``request`` is the original unresolved run intent; implementations
        must verify that it matches the frozen request the session was
        opened with on every shared business field (the admission-only
        fields preflight itself added are not compared) and fail closed on
        any mismatch.
        """
        ...

    def open_chunk(self, query: DataChunkQuery) -> "DataChunkSession":
        """Open one fixed chunk as its own context manager."""
        ...


@runtime_checkable
class DataChunkSession(Protocol):
    """One consistent read window over a fixed set of official sessions."""

    def __enter__(self) -> "DataChunkSession": ...
    def __exit__(self, exc_type, exc, traceback) -> bool | None: ...

    @property
    def consistency_evidence(self) -> DataConsistencyEvidence:
        """Persistable non-sensitive evidence about this chunk."""
        ...

    def validate_consistency(self) -> ConsistencyTokenStatus:
        """Validate chunk consistency before any business query runs."""
        ...

    def instruments(self, query: InstrumentQuery) -> tuple[InstrumentSpec, ...]: ...
    def instrument_mappings(
        self, query: InstrumentMappingQuery
    ) -> tuple[InstrumentCodeMapping, ...]: ...
    def trading_rules(self, query: TradingRuleQuery) -> tuple[TradingRule, ...]: ...
    def trading_status(
        self, query: TradingStatusQuery
    ) -> tuple[TradingStatus, ...]: ...
    def universe(self, query: UniverseQuery) -> tuple[InstrumentSpec, ...]: ...
    def bars(self, query: BarQuery) -> tuple[Bar, ...]: ...
    def ticks(self, query: TickQuery) -> tuple[Tick, ...]: ...
    def values(self, query: DataValueQuery) -> tuple[DataPoint, ...]: ...
    def adjusted_series(
        self, query: AdjustedSeriesQuery
    ) -> tuple[AdjustedSeriesPoint, ...]: ...
    def corporate_actions(
        self, query: CorporateActionQuery
    ) -> tuple[CorporateAction, ...]: ...
    def coverage(self, query: CoverageQuery) -> DataCoverageReport: ...
