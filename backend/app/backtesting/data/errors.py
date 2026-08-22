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

__all__ = [
    "ERROR_CODES",
    "ConsistencyCoverageIncompleteError",
    "ConsistencyNotValidatedError",
    "ConsistencyTokenExpiredError",
    "ConsistencyTokenInvalidError",
    "DataContractError",
    "DataCutoffExceededError",
    "DataPreflightBlockedError",
    "DataPreflightConfirmationMismatchError",
    "DataSessionClosedError",
    "HistoryBarInstrumentMismatchError",
    "HistoryBarsDuplicateError",
    "HistoryBarsIncompleteError",
    "HistoryIncompleteError",
    "IdentityMappingConflictError",
    "IdentityMappingEvidenceMissingError",
    "IdentityMappingIncompleteError",
    "InvalidDataRequestError",
    "LookbackSessionsLimitExceededError",
    "ProviderContractViolationError",
    "UniverseCalendarNotPreflightedError",
    "UnsupportedCapabilityError",
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


class DataContractError(ValueError):
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


class UnsupportedCapabilityError(DataContractError):
    """A requested fact type or feature is not supported by the provider."""

    code = "unsupported_capability"


class DataPreflightBlockedError(DataContractError):
    """Admission preflight ended in ``blocked``; no run may be created."""

    code = "data_preflight_blocked"


class DataPreflightConfirmationMismatchError(DataContractError):
    """A degraded preflight was accepted with a hash other than the report's."""

    code = "data_preflight_confirmation_mismatch"


class DataCutoffExceededError(DataContractError):
    """A query would read past ``data_cutoff``; it fails instead of trimming."""

    code = "data_cutoff_exceeded"


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


class UniverseCalendarNotPreflightedError(DataContractError):
    """A dynamic candidate set carries a calendar never preflighted this run.

    The candidate is blocked instead of widening the run's frozen time axis.
    """

    code = "universe_calendar_not_preflighted"


class DataSessionClosedError(DataContractError):
    """A data session was closed; no new chunks or business queries may run."""

    code = "data_session_closed"


ERROR_CODES: frozenset[str] = frozenset(
    cls.code
    for cls in (
        InvalidDataRequestError,
        UnsupportedCapabilityError,
        DataPreflightBlockedError,
        DataPreflightConfirmationMismatchError,
        DataCutoffExceededError,
        LookbackSessionsLimitExceededError,
        IdentityMappingIncompleteError,
        IdentityMappingConflictError,
        IdentityMappingEvidenceMissingError,
        HistoryIncompleteError,
        HistoryBarsIncompleteError,
        HistoryBarsDuplicateError,
        HistoryBarInstrumentMismatchError,
        ConsistencyNotValidatedError,
        ConsistencyTokenInvalidError,
        ConsistencyTokenExpiredError,
        ConsistencyCoverageIncompleteError,
        ProviderContractViolationError,
        DataSessionClosedError,
        UniverseCalendarNotPreflightedError,
    )
)
"""Every stable error code defined by data-contract version 1."""
