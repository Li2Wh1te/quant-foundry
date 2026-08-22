"""Strategy protocol domain objects for the page-strategy contract.

This module defines the machine identity of the only official page-strategy
protocol (``strategy_contract_version = 1``), the shared lookback limit, the
run failure phase used by the startup contract check, and the protocol error
hierarchy.  It deliberately contains no compatibility constants: the legacy
``{"intents": []}`` return protocol was removed without a migration path.

The lookback limit and the data-query error hierarchy converge into the
generic data contract (``app.backtesting.data``): this module re-exports
``MAX_LOOKBACK_SESSIONS`` and provides thin legacy subclasses of the new
errors so existing imports, ``except`` clauses, and constructor signatures
keep working while codes and semantics have exactly one truth source.
"""

from __future__ import annotations

from app.backtesting.data.errors import (
    DataContractError,
    DataCutoffExceededError,
    HistoryIncompleteError,
    IdentityMappingIncompleteError,
    LookbackSessionsLimitExceededError,
    ProviderContractViolationError,
)
from app.backtesting.data.requests import MAX_LOOKBACK_SESSIONS

STRATEGY_CONTRACT_VERSION = 1
"""Machine identifier of the current and only official strategy protocol.

This marker does not imply that historical v0/v1 protocols exist or that any
migration path is provided.  Old revisions are never backfilled with a
backtesting entry point.
"""

FAILURE_PHASE_STRATEGY_CONTRACT_CHECK = "strategy_contract_check"
"""Run failure phase recorded when the startup contract check rejects a run.

A failed contract check ends the run before the main backtest loop starts and
never rolls back the published revision.
"""


class StrategyProtocolError(ValueError):
    """Base error for every strategy-protocol violation.

    The message must stay display-safe: it may be surfaced to operators as a
    concise Chinese summary plus expandable technical details.
    """


class MissingDecisionModeError(StrategyProtocolError):
    """Raised when a decision return value has no ``mode`` field."""


class UnknownDecisionModeError(StrategyProtocolError):
    """Raised when a decision uses a mode that is not registered."""


class InvalidDecisionPayloadError(StrategyProtocolError):
    """Raised when a decision return value is not a valid object."""


class UnknownInstrumentError(StrategyProtocolError):
    """Raised when an ``instrument_id`` is outside the run's known identities."""


class DataQueryContractError(StrategyProtocolError):
    """Base error for data-query boundary violations.

    Legacy compatibility base: the concrete data-query errors below also
    subclass their counterparts in ``app.backtesting.data.errors``, so every
    instance carries the stable ``code`` of the unified hierarchy.
    """


class DataCutoffViolationError(DataCutoffExceededError, DataQueryContractError):
    """Legacy path of ``DataCutoffExceededError`` (code ``data_cutoff_exceeded``).

    Raised when a query would read past the strict ``data_cutoff``.
    """

    def __init__(self, requested_end: object, cutoff: object) -> None:
        DataContractError.__init__(
            self,
            f"query end {requested_end} is later than data_cutoff {cutoff}",
            details={"requested_end": str(requested_end), "data_cutoff": str(cutoff)},
        )
        self.requested_end = requested_end
        self.cutoff = cutoff


class LookbackLimitExceededError(
    LookbackSessionsLimitExceededError, DataQueryContractError
):
    """Legacy path of ``LookbackSessionsLimitExceededError``.

    Raised when ``lookback_sessions`` exceeds the first-version maximum.  The
    check runs before any data access so oversized windows can never trigger
    partial reads.
    """

    def __init__(self, requested: int, maximum: int = MAX_LOOKBACK_SESSIONS) -> None:
        DataContractError.__init__(
            self,
            f"lookback_sessions {requested} exceeds the maximum of {maximum}",
            details={"requested": requested, "maximum": maximum},
        )
        self.requested = requested
        self.maximum = maximum


class IdentityMappingMissingError(
    IdentityMappingIncompleteError, DataQueryContractError
):
    """Legacy path of ``IdentityMappingIncompleteError``.

    Raised when PIT identity mapping evidence does not cover the window.  The
    query is blocked instead of guessing or silently returning a shorter
    window.
    """


class IncompleteHistoryError(HistoryIncompleteError, DataQueryContractError):
    """Legacy path of ``HistoryIncompleteError``.

    Raised when required history bars are missing inside a mapped segment.
    Missing bars block the query; they are never forward-filled or replaced
    with fabricated short windows.
    """


class InvalidProviderResultError(ProviderContractViolationError, DataQueryContractError):
    """Legacy path of ``ProviderContractViolationError``.

    Raised when a data provider returns rows violating the read contract.
    Examples include bars past ``data_cutoff``, rows keyed by a different
    ``instrument_id``, out-of-order results, or mutable/float payloads.
    """


class AdjustmentNotActiveError(DataQueryContractError):
    """Raised when qfq/hfq series are requested while adjustment is unverified.

    ``raw`` never applies adjustment factors.  Adjusted bases require the
    native adjustment-factor source to pass verification and be marked active.
    """
