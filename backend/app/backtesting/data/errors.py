"""Stable error contract for the generic backtesting data layer.

Every error raised by the data contract carries a stable machine-readable
``code`` plus a display-safe message and deep-frozen JSON ``details``.
Business code must branch on ``code`` and never parse exception text.

The legacy strategy-protocol exceptions (``DataCutoffViolationError`` and
friends) converge into this hierarchy: they are re-exported through thin
compatibility subclasses in ``app.strategy_protocol.contract`` so existing
imports keep working, while this module remains the single source of truth
for codes and semantics.

This module is deliberately free of ORM, database session, FastAPI, and
Tushare imports.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Mapping

from app.backtesting.domain import DomainValidationError

__all__ = [
    "ERROR_CODES",
    "ConsistencyCoverageIncompleteError",
    "ConsistencyNotValidatedError",
    "ConsistencyTokenExpiredError",
    "ConsistencyTokenInvalidError",
    "DataContractError",
    "DataCutoffExceededError",
    "DataCutoffRequiredError",
    "DataPreflightBlockedError",
    "DataPreflightConfirmationMismatchError",
    "FormalPreflightProfileMismatchError",
    "FormalCapabilityGateBlockedError",
    "FormalDataCorrectnessGateBlockedError",
    "FormalAccountingGateBlockedError",
    "FormalInitialPositionPreflightBlockedError",
    "FormalDegradedConfirmationRequiredError",
    "FormalDegradedConfirmationMismatchError",
    "FormalRunCreationBlockedError",
    "FormalFixtureNotAllowedError",
    "FormalUnavailableCapabilityError",
    "FormalPitEvidenceMissingError",
    "FormalSourceRevisionConflictError",
    "FormalConsistencyContractUnavailableError",
    "TradingStatusCapabilityRequirementMismatchError",
    "DataSessionClosedError",
    "HistoryBarInstrumentMismatchError",
    "HistoryBarsDuplicateError",
    "HistoryBarsIncompleteError",
    "HistoryIncompleteError",
    "BarInvalidError",
    "BarFieldMissingError",
    "IdentityMappingConflictError",
    "IdentityMappingEvidenceMissingError",
    "IdentityMappingIncompleteError",
    "InvalidDataRequestError",
    "LookbackSessionsLimitExceededError",
    "ProviderContractViolationError",
    "CoverageFactInvalidError",
    "CoverageFactConflictError",
    "CoverageIncompleteError",
    "CoverageRequiredFieldMissingError",
    "CoverageProviderContractViolationError",
    "InternalPreflightProfileMismatchError",
    "InternalPreflightFixtureMissingError",
    "InternalPreflightFixtureOutOfScopeError",
    "InternalPreflightDegradedForbiddenError",
    "DataPreflightReportHashMismatchError",
    "UniverseScopeUnresolvedError",
    "UniverseCapabilityMissingError",
    "UniversePitBoundaryViolationError",
    "UniverseTargetOutsideScopeError",
    "UniverseSelectedIneligibleError",
    "UniversePreflightHashMismatchError",
    "UniverseProviderContractViolationError",
    "UniverseCalendarNotPreflightedError",
    "UnsupportedCapabilityError",
    "CalendarContractError",
    "CalendarIdSetEmptyError",
    "CalendarIdUnknownError",
    "CalendarRegistryFactMissingError",
    "CalendarRegistryReferenceInvalidError",
    "CalendarRegistryAmbiguousError",
    "CalendarBindingUnknownError",
    "CalendarBindingAmbiguousError",
    "InstrumentCalendarUnresolvedError",
    "CalendarDefinitionMissingError",
    "CalendarDefinitionAmbiguousError",
    "CalendarDefinitionInvalidError",
    "CalendarDefinitionOverlapError",
    "CalendarPitMetadataMissingError",
    "CalendarFactMissingError",
    "CalendarFactAmbiguousError",
    "CalendarFactInvalidError",
    "CalendarSessionUnresolvedError",
    "CalendarSessionInvalidError",
    "CalendarSessionIncompatibleError",
    "CalendarTimezoneInconsistentError",
    "CalendarTimezoneMismatchError",
    "CalendarTimezoneUnsupportedError",
    "CalendarSnapshotCoverageUnknownError",
    "CalendarSnapshotRevisionChangedError",
    "CalendarSnapshotRetryExhaustedError",
    "CalendarDateSpanLimitExceededError",
    "CalendarPreflightResourceLimitExceededError",
    "CalendarCapabilityDeclarationAmbiguousError",
    "CalendarSourcePriorityMissingError",
    "CalendarSourcePriorityInvalidError",
    "CalendarSourcePriorityAmbiguousError",
    "CalendarSourcePriorityChainBrokenError",
    "CalendarPitProfileUndecidedError",
    "CalendarJsonInvalidError",
    "CalendarSessionWindowLimitExceededError",
    "CalendarCrossMidnightUnsupportedError",
    "CalendarIngestionRangeIncompleteError",
    "CalendarSourceRevisionConflictError",
    "LegacyExchangeAmbiguousError",
    "freeze_json",
]


def freeze_json(value: object, field_name: str) -> Mapping[str, object] | tuple[object, ...] | None | str | bool | int | float:
    """Recursively deep-freeze one JSON value and reject non-JSON objects.

    Only exact JSON scalar types pass through (subclasses could smuggle
    mutable attributes); mappings become read-only ``MappingProxyType``
    instances and sequences become tuples.  Floats must be finite because
    NaN and infinities are not valid JSON numbers.  Sets, custom classes,
    and other non-JSON objects are rejected outright.
    """

    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field_name} float values must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{field_name} keys must be plain strings")
            frozen[key] = freeze_json(item, f"{field_name}[{key!r}]")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, field_name) for item in value)
    raise ValueError(
        f"{field_name} must only contain JSON scalars, mappings, or sequences"
    )


class DataContractError(DomainValidationError):
    """Base error for every generic data-contract violation.

    ``code`` is a stable machine identifier that never changes meaning;
    ``message`` is display-safe text and ``details`` is deep-frozen JSON
    context.  Callers branch on ``code``, never on the message.
    """

    code = "data_contract_error"

    def __init__(
        self, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(message)
        if details is None:
            self.details: Mapping[str, object] = MappingProxyType({})
        else:
            self.details = freeze_json(details, "details")


class InvalidDataRequestError(DataContractError):
    """A request or query object violates a data-contract invariant."""

    code = "invalid_data_request"


class TradingStatusCapabilityRequirementMismatchError(InvalidDataRequestError):
    """Frozen rule applicability disagrees with the request STATUS capability."""

    code = "trading_status_capability_requirement_mismatch"


class UnsupportedCapabilityError(DataContractError):
    """A requested fact type or feature is not supported by the provider."""

    code = "unsupported_capability"


class DataPreflightBlockedError(DataContractError):
    """Admission preflight ended in ``blocked``; no run may be created."""

    code = "data_preflight_blocked"


class DataPreflightConfirmationMismatchError(DataContractError):
    """A degraded preflight was accepted with a hash other than the report's."""

    code = "data_preflight_confirmation_mismatch"


# Formal-run admission codes are kept distinct from the legacy generic
# preflight errors so API callers can classify production gate failures
# without parsing display messages.
class FormalPreflightProfileMismatchError(DataContractError):
    code = "formal_preflight_profile_mismatch"


class FormalCapabilityGateBlockedError(DataContractError):
    code = "formal_capability_gate_blocked"


class FormalDataCorrectnessGateBlockedError(DataContractError):
    code = "formal_data_correctness_gate_blocked"


class FormalAccountingGateBlockedError(DataContractError):
    code = "formal_accounting_gate_blocked"


class FormalInitialPositionPreflightBlockedError(DataContractError):
    code = "formal_initial_position_preflight_blocked"


class FormalDegradedConfirmationRequiredError(DataContractError):
    code = "formal_degraded_confirmation_required"


class FormalDegradedConfirmationMismatchError(DataContractError):
    code = "formal_degraded_confirmation_mismatch"


class FormalRunCreationBlockedError(DataContractError):
    code = "formal_run_creation_blocked"


class FormalFixtureNotAllowedError(DataContractError):
    code = "formal_fixture_not_allowed"


class FormalUnavailableCapabilityError(DataContractError):
    code = "formal_unavailable_capability"


class FormalPitEvidenceMissingError(DataContractError):
    code = "formal_pit_evidence_missing"


class FormalSourceRevisionConflictError(DataContractError):
    code = "formal_source_revision_conflict"


class FormalConsistencyContractUnavailableError(DataContractError):
    code = "formal_consistency_contract_unavailable"


class DataCutoffExceededError(DataContractError):
    """A query would read past ``data_cutoff``; it fails instead of trimming."""

    code = "data_cutoff_exceeded"


class DataCutoffRequiredError(DataContractError):
    """A strict calendar query did not provide an explicit data cutoff."""

    code = "data_cutoff_required"


class LookbackSessionsLimitExceededError(DataContractError):
    """A lookback window exceeds the first-version maximum of 512 sessions."""

    code = "lookback_sessions_limit_exceeded"


class IdentityMappingIncompleteError(DataContractError):
    """PIT identity-mapping evidence does not cover the requested window."""

    code = "identity_mapping_incomplete"


class IdentityMappingConflictError(DataContractError):
    """Two or more PIT mappings claim the same requested session."""

    code = "identity_mapping_conflict"


class IdentityMappingEvidenceMissingError(DataContractError):
    """A covering PIT mapping carries no usable evidence."""

    code = "identity_mapping_evidence_missing"


class HistoryBarsIncompleteError(DataContractError):
    """One identity segment did not return a bar for every requested session."""

    code = "history_bars_incomplete"


class HistoryBarsDuplicateError(DataContractError):
    """One identity segment returned more than one bar for a session."""

    code = "history_bars_duplicate"


class HistoryBarInstrumentMismatchError(DataContractError):
    """A returned bar is keyed by an instrument other than the queried one."""

    code = "history_bar_instrument_mismatch"


class HistoryIncompleteError(DataContractError):
    """Required history facts are missing; gaps are never repaired."""

    code = "history_incomplete"


class BarInvalidError(DataContractError):
    """An ETF bar failed its versioned OHLC validity rules."""

    code = "bar_invalid"


class BarFieldMissingError(DataContractError):
    """A required OHLC field is absent from an ETF bar."""

    code = "bar_field_missing"


class ConsistencyNotValidatedError(DataContractError):
    """A chunk business query ran before ``validate_consistency()`` passed."""

    code = "consistency_not_validated"


class ConsistencyTokenInvalidError(DataContractError):
    """The chunk consistency token failed validation."""

    code = "consistency_token_invalid"


class ConsistencyTokenExpiredError(DataContractError):
    """The chunk consistency token expired before validation completed."""

    code = "consistency_token_expired"


class ConsistencyCoverageIncompleteError(DataContractError):
    """The consistency token does not cover every required chunk fact type."""

    code = "consistency_coverage_incomplete"


class ProviderContractViolationError(DataContractError):
    """A provider returned wrong, out-of-range, unordered, or mutable data."""

    code = "provider_contract_violation"


class CoverageFactInvalidError(ProviderContractViolationError):
    """A coverage fact violates the immutable fact-envelope contract."""

    code = "coverage_fact_invalid"


class CoverageFactConflictError(ProviderContractViolationError):
    """Two different materializations claim one coverage fact key."""

    code = "coverage_fact_conflict"


class CoverageIncompleteError(DataContractError):
    """Required coverage is partial or cannot be proven complete."""

    code = "coverage_incomplete"


class CoverageRequiredFieldMissingError(DataContractError):
    """A required field has no covering coverage fact."""

    code = "coverage_required_field_missing"


class CoverageProviderContractViolationError(ProviderContractViolationError):
    """Coverage input contains a fact outside the requested contract."""

    code = "coverage_provider_contract_violation"


class InternalPreflightProfileMismatchError(DataContractError):
    """The requested internal preflight profile is not an exact match."""

    code = "internal_preflight_profile_mismatch"


class InternalPreflightFixtureMissingError(DataContractError):
    """A required named internal fixture was not supplied."""

    code = "internal_preflight_fixture_missing"


class InternalPreflightFixtureOutOfScopeError(DataContractError):
    """An internal fixture does not cover the requested instrument/date scope."""

    code = "internal_preflight_fixture_out_of_scope"


class InternalPreflightDegradedForbiddenError(DataContractError):
    """The internal-link profile cannot return a degraded result."""

    code = "internal_preflight_degraded_forbidden"


class DataPreflightReportHashMismatchError(DataContractError):
    """A preflight report hash does not match its canonical machine content."""

    code = "data_preflight_report_hash_mismatch"


class UniverseCalendarNotPreflightedError(DataContractError):
    """A dynamic candidate set carries a calendar never preflighted this run.

    The candidate is blocked instead of widening the run's frozen time axis.
    """

    code = "universe_calendar_not_preflighted"


class UniverseScopeUnresolvedError(DataContractError):
    """The dynamic scope could not be reduced to a finite named calendar set."""

    code = "universe_scope_unresolved"


class UniverseCapabilityMissingError(DataContractError):
    """The provider cannot prove the capabilities required by a dynamic scope."""

    code = "universe_capability_missing"


class UniversePitBoundaryViolationError(DataContractError):
    """A candidate or query would read outside its frozen PIT boundaries."""

    code = "universe_pit_boundary_violation"


class UniverseTargetOutsideScopeError(DataContractError):
    """A strategy target is not in the fixed or current dynamic permission set."""

    code = "universe_target_outside_scope"


class UniverseSelectedIneligibleError(DataContractError):
    """A selected candidate failed final qualification before order creation."""

    code = "universe_selected_ineligible"


class UniversePreflightHashMismatchError(DataContractError):
    """A session or run observed a different frozen universe-scope hash."""

    code = "universe_preflight_hash_mismatch"


class UniverseProviderContractViolationError(DataContractError):
    """A universe provider returned malformed or non-PIT candidate data."""

    code = "universe_provider_contract_violation"


class DataSessionClosedError(DataContractError):
    """A data session was closed; no new chunks or business queries may run."""

    code = "data_session_closed"


class CalendarContractError(DataContractError, DomainValidationError):
    """Base class for stable named-calendar contract failures."""

    code = "calendar_contract_error"


# Calendar errors are deliberately one-class-per-code.  Callers branch on
# ``code`` and never parse the Chinese display message; details remain JSON
# only so the errors are safe for API and report serialization.
class CalendarIdSetEmptyError(CalendarContractError):
    code = "calendar_id_set_empty"


class CalendarIdUnknownError(CalendarContractError):
    code = "calendar_id_unknown"


class CalendarRegistryFactMissingError(CalendarContractError):
    code = "calendar_registry_fact_missing"


class CalendarRegistryReferenceInvalidError(CalendarContractError):
    code = "calendar_registry_reference_invalid"


class CalendarRegistryAmbiguousError(CalendarContractError):
    code = "calendar_registry_ambiguous"


class CalendarBindingUnknownError(CalendarContractError):
    code = "calendar_binding_unknown"


class CalendarBindingAmbiguousError(CalendarContractError):
    code = "calendar_binding_ambiguous"


class InstrumentCalendarUnresolvedError(CalendarContractError):
    code = "instrument_calendar_unresolved"


class CalendarDefinitionMissingError(CalendarContractError):
    code = "calendar_definition_missing"


class CalendarDefinitionAmbiguousError(CalendarContractError):
    code = "calendar_definition_ambiguous"


class CalendarDefinitionInvalidError(CalendarContractError):
    code = "calendar_definition_invalid"


class CalendarDefinitionOverlapError(CalendarContractError):
    code = "calendar_definition_overlap"


class CalendarPitMetadataMissingError(CalendarContractError):
    code = "calendar_pit_metadata_missing"


class CalendarFactMissingError(CalendarContractError):
    code = "calendar_fact_missing"


class CalendarFactAmbiguousError(CalendarContractError):
    code = "calendar_fact_ambiguous"


class CalendarFactInvalidError(CalendarContractError):
    code = "calendar_fact_invalid"


class CalendarSessionUnresolvedError(CalendarContractError):
    code = "calendar_session_unresolved"


class CalendarSessionInvalidError(CalendarContractError):
    code = "calendar_session_invalid"


class CalendarSessionIncompatibleError(CalendarContractError):
    code = "calendar_session_incompatible"


class CalendarTimezoneInconsistentError(CalendarContractError):
    code = "calendar_timezone_inconsistent"


class CalendarTimezoneMismatchError(CalendarContractError):
    code = "calendar_timezone_mismatch"


class CalendarTimezoneUnsupportedError(CalendarContractError):
    code = "calendar_timezone_unsupported"


class CalendarSnapshotCoverageUnknownError(CalendarContractError):
    code = "calendar_snapshot_coverage_unknown"


class CalendarSnapshotRevisionChangedError(CalendarContractError):
    code = "calendar_snapshot_revision_changed"


class CalendarSnapshotRetryExhaustedError(CalendarContractError):
    code = "calendar_snapshot_retry_exhausted"


class CalendarDateSpanLimitExceededError(CalendarContractError):
    code = "calendar_date_span_limit_exceeded"


class CalendarPreflightResourceLimitExceededError(CalendarContractError):
    code = "calendar_preflight_resource_limit_exceeded"


class CalendarCapabilityDeclarationAmbiguousError(CalendarContractError):
    code = "capability_declaration_ambiguous"


class CalendarSourcePriorityMissingError(CalendarContractError):
    code = "calendar_source_priority_missing"


class CalendarSourcePriorityInvalidError(CalendarContractError):
    code = "calendar_source_priority_invalid"


class CalendarSourcePriorityAmbiguousError(CalendarContractError):
    code = "calendar_source_priority_ambiguous"


class CalendarSourcePriorityChainBrokenError(CalendarContractError):
    code = "calendar_source_priority_chain_broken"


class CalendarPitProfileUndecidedError(CalendarContractError):
    code = "calendar_pit_profile_undecided"


class CalendarJsonInvalidError(CalendarContractError):
    code = "calendar_json_invalid"


class CalendarSessionWindowLimitExceededError(CalendarContractError):
    code = "calendar_session_window_limit_exceeded"


class CalendarCrossMidnightUnsupportedError(CalendarContractError):
    code = "calendar_cross_midnight_unsupported"


class CalendarIngestionRangeIncompleteError(CalendarContractError):
    code = "calendar_ingestion_range_incomplete"


class CalendarSourceRevisionConflictError(CalendarContractError):
    code = "calendar_source_revision_conflict"


class LegacyExchangeAmbiguousError(CalendarContractError):
    code = "legacy_exchange_ambiguous"


ERROR_CODES: frozenset[str] = frozenset(
    cls.code
    for cls in (
        InvalidDataRequestError,
        TradingStatusCapabilityRequirementMismatchError,
        UnsupportedCapabilityError,
        DataPreflightBlockedError,
        DataPreflightConfirmationMismatchError,
        FormalPreflightProfileMismatchError,
        FormalCapabilityGateBlockedError,
        FormalDataCorrectnessGateBlockedError,
        FormalAccountingGateBlockedError,
        FormalInitialPositionPreflightBlockedError,
        FormalDegradedConfirmationRequiredError,
        FormalDegradedConfirmationMismatchError,
        FormalRunCreationBlockedError,
        FormalFixtureNotAllowedError,
        FormalUnavailableCapabilityError,
        FormalPitEvidenceMissingError,
        FormalSourceRevisionConflictError,
        FormalConsistencyContractUnavailableError,
        DataCutoffExceededError,
        DataCutoffRequiredError,
        LookbackSessionsLimitExceededError,
        IdentityMappingIncompleteError,
        IdentityMappingConflictError,
        IdentityMappingEvidenceMissingError,
        HistoryIncompleteError,
        BarInvalidError,
        BarFieldMissingError,
        HistoryBarsIncompleteError,
        HistoryBarsDuplicateError,
        HistoryBarInstrumentMismatchError,
        ConsistencyNotValidatedError,
        ConsistencyTokenInvalidError,
        ConsistencyTokenExpiredError,
        ConsistencyCoverageIncompleteError,
        ProviderContractViolationError,
        CoverageFactInvalidError,
        CoverageFactConflictError,
        CoverageIncompleteError,
        CoverageRequiredFieldMissingError,
        CoverageProviderContractViolationError,
        InternalPreflightProfileMismatchError,
        InternalPreflightFixtureMissingError,
        InternalPreflightFixtureOutOfScopeError,
        InternalPreflightDegradedForbiddenError,
        DataPreflightReportHashMismatchError,
        UniverseScopeUnresolvedError,
        UniverseCapabilityMissingError,
        UniversePitBoundaryViolationError,
        UniverseTargetOutsideScopeError,
        UniverseSelectedIneligibleError,
        UniversePreflightHashMismatchError,
        UniverseProviderContractViolationError,
        DataSessionClosedError,
        UniverseCalendarNotPreflightedError,
        CalendarIdSetEmptyError,
        CalendarIdUnknownError,
        CalendarRegistryFactMissingError,
        CalendarRegistryReferenceInvalidError,
        CalendarRegistryAmbiguousError,
        CalendarBindingUnknownError,
        CalendarBindingAmbiguousError,
        InstrumentCalendarUnresolvedError,
        CalendarDefinitionMissingError,
        CalendarDefinitionAmbiguousError,
        CalendarDefinitionInvalidError,
        CalendarDefinitionOverlapError,
        CalendarPitMetadataMissingError,
        CalendarFactMissingError,
        CalendarFactAmbiguousError,
        CalendarFactInvalidError,
        CalendarSessionUnresolvedError,
        CalendarSessionInvalidError,
        CalendarSessionIncompatibleError,
        CalendarTimezoneInconsistentError,
        CalendarTimezoneMismatchError,
        CalendarTimezoneUnsupportedError,
        CalendarSnapshotCoverageUnknownError,
        CalendarSnapshotRevisionChangedError,
        CalendarSnapshotRetryExhaustedError,
        CalendarDateSpanLimitExceededError,
        CalendarPreflightResourceLimitExceededError,
        CalendarCapabilityDeclarationAmbiguousError,
        CalendarSourcePriorityMissingError,
        CalendarSourcePriorityInvalidError,
        CalendarSourcePriorityAmbiguousError,
        CalendarSourcePriorityChainBrokenError,
        CalendarPitProfileUndecidedError,
        CalendarJsonInvalidError,
        CalendarSessionWindowLimitExceededError,
        CalendarCrossMidnightUnsupportedError,
        CalendarIngestionRangeIncompleteError,
        CalendarSourceRevisionConflictError,
        LegacyExchangeAmbiguousError,
    )
)
"""Every stable error code defined by data-contract version 1."""
