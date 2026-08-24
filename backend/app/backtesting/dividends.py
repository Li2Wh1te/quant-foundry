"""Cash-dividend events with record-date entitlement and arrival sessions.

The first dividend slice credited cash by simply multiplying the
position held on the arrival day.  This module replaces those
semantics with the frozen corporate-action model:

* Entitlement is fixed by the **record date**, not by whatever is held
  on the arrival day: selling after the record date keeps the dividend,
  buying after it gains nothing.  The entitlement quantity is computed
  once under a declared derivation rule (which must state explicitly
  whether unsettled T+1 lots count) and frozen onto the event.
* Cash lands in the ``cash_effective_session``, derived from the
  source payment/arrival dates through the trading calendar — never
  guessed from natural days.  The credit happens strictly *after* that
  session's opening match, so dividend cash can never fund the same
  morning's buy checks.
* ``event_id`` is the unique idempotency key.  Revisions, cancellations,
  and reversals enter as new events; history is never overwritten.

This module owns pure facts and derivations only; account mutation
lives in :mod:`app.backtesting.accounting`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol
from uuid import UUID

from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    PortfolioState,
    _decimal,
    _positive,
)

__all__ = [
    "CashDividendEvent",
    "CashEffectivePhase",
    "DividendDerivationError",
    "DividendEntryKind",
    "DividendEntitlementRuleError",
    "DividendError",
    "ENTITLEMENT_RULE_KEY",
    "ENTITLEMENT_RULE_VERSION",
    "compute_dividend_entitlement",
    "derive_cash_effective_session",
    "entitlement_from_portfolio",
]


class DividendError(DomainValidationError):
    """Base class of stable cash-dividend failures.

    ``code`` is a stable machine identifier and ``details`` carries
    structured context without parsing exception text.
    """

    code = "cash_dividend_error"

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = MappingProxyType(dict(details or {}))


class DividendEntitlementRuleError(DividendError):
    """The declared entitlement rule is unsupported or cannot be applied."""

    code = "dividend_entitlement_rule_unsupported"


class DividendDerivationError(DividendError):
    """The cash-effective session or entitlement could not be derived."""

    code = "dividend_derivation_failed"


class CashEffectivePhase(StrEnum):
    """Pipeline point where dividend cash may enter the account."""

    AFTER_OPEN_MATCH = "after_open_match"


class DividendEntryKind(StrEnum):
    """Direction of one dividend ledger entry.

    Revisions, cancellations, and reversals are *new events* referencing
    the original; they never mutate or delete a historical entry.
    """

    DIVIDEND = "dividend"
    REVERSAL = "reversal"


#: Key of the first supported entitlement derivation rule.
ENTITLEMENT_RULE_KEY = "record_date_entitlement"
ENTITLEMENT_RULE_VERSION = 1


def _calendar_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise DividendError(f"{field_name} must be a calendar date")
    return value


@dataclass(frozen=True, slots=True)
class CashDividendEvent:
    """One auditable cash-dividend corporate action.

    An event is created from source evidence with its entitlement still
    open (``entitlement_quantity=None``); the runner freezes the
    entitlement during the record-date session under the declared
    derivation rule.  A source that already knows the entitlement (a
    late-subscribed run starting after the record date) may supply it
    directly; either way the frozen quantity never changes afterwards.
    """

    event_id: UUID
    instrument_id: UUID
    ex_date: date
    record_date: date
    source_payment_date: date
    source_arrival_date: date
    cash_effective_session_id: date
    amount_per_share: Decimal | int | str
    source_evidence: Mapping[str, str]
    as_of: date
    currency: str = "CNY"
    cash_effective_phase: CashEffectivePhase | str = (
        CashEffectivePhase.AFTER_OPEN_MATCH
    )
    entitlement_quantity: Decimal | int | str | None = None
    withholding_tax: Decimal | int | str = ZERO
    entry_kind: DividendEntryKind | str = DividendEntryKind.DIVIDEND
    revision_of_event_id: UUID | None = None
    derivation_rule_key: str = ENTITLEMENT_RULE_KEY
    derivation_rule_version: int = ENTITLEMENT_RULE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID):
            raise DividendError("event_id must be a UUID")
        if not isinstance(self.instrument_id, UUID):
            raise DividendError("instrument_id must be a UUID")
        for name in (
            "ex_date",
            "record_date",
            "source_payment_date",
            "source_arrival_date",
            "cash_effective_session_id",
        ):
            _calendar_date(getattr(self, name), name)
        # Corporate-action chronology: ex-date cuts first, holders are
        # registered second, money is paid and arrives afterwards, and
        # the cash-effective session can never precede registration.
        if not (
            self.ex_date
            <= self.record_date
            <= self.source_payment_date
            <= self.source_arrival_date
            <= self.cash_effective_session_id
        ):
            raise DividendError(
                f"dividend event {self.event_id} dates violate the "
                "ex_date <= record_date <= payment_date <= arrival_date "
                "<= cash_effective_session ordering",
                details={
                    "event_id": str(self.event_id),
                    "ex_date": self.ex_date.isoformat(),
                    "record_date": self.record_date.isoformat(),
                    "source_payment_date": self.source_payment_date.isoformat(),
                    "source_arrival_date": self.source_arrival_date.isoformat(),
                    "cash_effective_session_id": (
                        self.cash_effective_session_id.isoformat()
                    ),
                },
            )
        _calendar_date(self.as_of, "as_of")
        if not isinstance(self.source_evidence, Mapping) or not self.source_evidence:
            raise DividendError(
                f"dividend event {self.event_id} carries no source "
                "evidence; unverifiable corporate actions cannot enter "
                "the accounting pipeline"
            )
        try:
            object.__setattr__(
                self, "cash_effective_phase", CashEffectivePhase(self.cash_effective_phase)
            )
        except ValueError as exc:
            raise DividendError(
                "only after_open_match cash dividends are supported"
            ) from exc
        try:
            object.__setattr__(
                self, "entry_kind", DividendEntryKind(self.entry_kind)
            )
        except ValueError as exc:
            raise DividendError("entry_kind must be dividend or reversal") from exc
        object.__setattr__(
            self, "amount_per_share", _positive(self.amount_per_share, "amount_per_share")
        )
        object.__setattr__(
            self, "withholding_tax", _decimal(self.withholding_tax, "withholding_tax")
        )
        if self.withholding_tax < ZERO:
            raise DividendError("withholding_tax must be non-negative")
        if self.entitlement_quantity is not None:
            normalized = _decimal(self.entitlement_quantity, "entitlement_quantity")
            if normalized < ZERO:
                raise DividendError("entitlement_quantity must be non-negative")
            object.__setattr__(self, "entitlement_quantity", normalized)
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise DividendError("currency must be non-blank text")
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if not isinstance(self.derivation_rule_key, str) or not self.derivation_rule_key.strip():
            raise DividendError(
                "derivation_rule_key must identify the frozen derivation rule"
            )
        object.__setattr__(
            self, "derivation_rule_key", self.derivation_rule_key.strip()
        )
        if (
            isinstance(self.derivation_rule_version, bool)
            or not isinstance(self.derivation_rule_version, int)
            or self.derivation_rule_version <= 0
        ):
            raise DividendError("derivation_rule_version must be a positive integer")
        if self.revision_of_event_id is not None and not isinstance(
            self.revision_of_event_id, UUID
        ):
            raise DividendError("revision_of_event_id must be a UUID when provided")
        evidence: dict[str, str] = {}
        for key, value in self.source_evidence.items():
            if not isinstance(key, str) or not key.strip():
                raise DividendError("source_evidence keys must be non-blank text")
            if not isinstance(value, str):
                raise DividendError("source_evidence values must be text")
            evidence[key.strip()] = value
        object.__setattr__(self, "source_evidence", MappingProxyType(evidence))

    @property
    def is_entitlement_frozen(self) -> bool:
        return self.entitlement_quantity is not None

    def with_entitlement(self, quantity: Decimal | int | str) -> "CashDividendEvent":
        """Return a copy with the entitlement frozen exactly once."""

        if self.is_entitlement_frozen:
            raise DividendError(
                f"dividend event {self.event_id} already carries a frozen "
                "entitlement; revisions must be new events"
            )
        return CashDividendEvent(
            event_id=self.event_id,
            instrument_id=self.instrument_id,
            ex_date=self.ex_date,
            record_date=self.record_date,
            source_payment_date=self.source_payment_date,
            source_arrival_date=self.source_arrival_date,
            cash_effective_session_id=self.cash_effective_session_id,
            amount_per_share=self.amount_per_share,
            currency=self.currency,
            cash_effective_phase=self.cash_effective_phase,
            entitlement_quantity=quantity,
            withholding_tax=self.withholding_tax,
            entry_kind=self.entry_kind,
            revision_of_event_id=self.revision_of_event_id,
            source_evidence=self.source_evidence,
            as_of=self.as_of,
            derivation_rule_key=self.derivation_rule_key,
            derivation_rule_version=self.derivation_rule_version,
        )

    @property
    def gross_amount(self) -> Decimal:
        """``amount_per_share × entitlement``; requires a frozen entitlement."""

        return self.amount_per_share * self._required_entitlement()

    @property
    def net_cash_delta(self) -> Decimal:
        """Signed cash change of this event (negative for reversals)."""

        net = self.gross_amount - self.withholding_tax
        if net < ZERO:
            raise DividendError(
                f"dividend event {self.event_id} withholds more than its "
                "gross amount",
                details={
                    "event_id": str(self.event_id),
                    "gross_amount": str(self.gross_amount),
                    "withholding_tax": str(self.withholding_tax),
                },
            )
        if self.entry_kind is DividendEntryKind.REVERSAL:
            return -net
        return net

    def _required_entitlement(self) -> Decimal:
        if self.entitlement_quantity is None:
            raise DividendDerivationError(
                f"dividend event {self.event_id} has no frozen entitlement "
                "quantity; it cannot be valued yet",
                details={"event_id": str(self.event_id)},
            )
        assert self.entitlement_quantity is not None
        return self.entitlement_quantity


def compute_dividend_entitlement(
    *,
    held_quantity: Decimal | int | str,
    pending_settlement_quantity: Decimal | int | str = ZERO,
    include_pending_settlement: bool,
) -> Decimal:
    """Apply the declared record-date entitlement rule to held units.

    The rule must state explicitly whether unsettled T+1 lots count
    towards the record-date entitlement; leaving it implicit would let
    the same facts produce different entitlements across runs.
    """

    held = _decimal(held_quantity, "held_quantity")
    pending = _decimal(pending_settlement_quantity, "pending_settlement_quantity")
    if held < ZERO or pending < ZERO:
        raise DividendEntitlementRuleError(
            "held and pending quantities must be non-negative"
        )
    base = held if include_pending_settlement else held - pending
    if base < ZERO:
        raise DividendEntitlementRuleError(
            "pending settlement quantity exceeds held quantity; the "
            "position facts are inconsistent"
        )
    return base


class _PendingSettlementView(Protocol):
    """Structural view of the accounting policy used for entitlements."""

    def pending_batches(self): ...


def entitlement_from_portfolio(
    portfolio: PortfolioState,
    accounting: _PendingSettlementView,
    *,
    instrument_id: UUID,
    include_pending_settlement: bool,
) -> Decimal:
    """Compute the record-date entitlement from live portfolio facts.

    Called during the record-date session only: positions already reflect
    that session's opening match, so same-day trades participate under
    the end-of-record-date-session convention while T+1 lots bought that
    day are still explicitly visible as pending batches.
    """

    position = portfolio.positions.get(instrument_id)
    held = position.quantity if position is not None else ZERO
    pending = ZERO
    for batch in accounting.pending_batches():
        if batch.instrument_id == instrument_id:
            pending += batch.quantity
    return compute_dividend_entitlement(
        held_quantity=held,
        pending_settlement_quantity=pending,
        include_pending_settlement=include_pending_settlement,
    )


class SettlementCalendarGatewayLike(Protocol):
    """Structural gateway contract reused from the settlement module."""

    def next_open_session(self, calendar_id: str, after_session: date) -> date | None:
        ...


def derive_cash_effective_session(
    gateway: SettlementCalendarGatewayLike,
    *,
    calendar_id: str,
    source_arrival_date: date,
) -> date:
    """Derive the cash-effective session from the source arrival date.

    The effective session is the calendar's first open session on or
    after the arrival date: funds physically arriving on a closed day
    land with the next open session.  Derivation goes through the same
    hardened calendar gateway as T+1 settlement — natural-day guesses
    and default calendars are impossible here.
    """

    _calendar_date(source_arrival_date, "source_arrival_date")
    search_after = source_arrival_date - timedelta(days=1)
    effective = gateway.next_open_session(calendar_id, after_session=search_after)
    if effective is None or effective < source_arrival_date:
        raise DividendDerivationError(
            f"calendar {calendar_id!r} resolved no open session on or "
            f"after the arrival date {source_arrival_date.isoformat()}; "
            "the cash-effective session cannot be derived",
            details={
                "calendar_id": calendar_id,
                "source_arrival_date": source_arrival_date.isoformat(),
            },
        )
    return effective
