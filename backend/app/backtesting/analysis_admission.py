"""Run-admission gates for analyzer runs (task package 06 section 8/10.1).

The repository currently has no unified run-creation service; this module
provides the frozen admission gates as pure, independently testable
functions so any future run-creation flow composes them in the documented
order:

1. :func:`build_initial_equity_snapshot` -- construct the E0 evidence from
   raw close marks that are *strictly* point-in-time available before the
   first formal open (missing marks block with ``MISSING_INITIAL_MARK``,
   non-positive E0 blocks with ``NON_POSITIVE_INITIAL_EQUITY``);
2. :func:`verify_initial_portfolio_consistency` -- the frozen E0 must
   equal the actual initial portfolio state;
3. :func:`freeze_rate_snapshot` -- one-shot prefetch of the whole formal
   window's PIT daily risk-free rates through the injected analysis
   gateway (Sharpe B only);
4. :func:`ensure_modeled_cash_movements` -- every cash movement must be
   classifiable as initial capital, an applied fill/fee, or an already
   modeled corporate-action event; anything else blocks the run with
   ``UNMODELED_EXTERNAL_CASH_FLOW``.

Every rejection carries the frozen v1 ``reason_code`` so callers surface
the documented blocked reasons verbatim.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.backtesting.analysis_inputs import (
    InitialEquitySnapshot,
    PitRateSnapshot,
)
from app.backtesting.data.facts import ClosePriceFact, PitRateSnapshotQuery
from app.backtesting.domain import DomainValidationError

__all__ = [
    "ALLOWED_CASH_FLOW_KINDS",
    "AdmissionBlockedError",
    "build_initial_equity_snapshot",
    "ensure_modeled_cash_movements",
    "freeze_rate_snapshot",
    "verify_initial_portfolio_consistency",
]


#: The exhaustive set of modeled cash-flow kinds for simple returns.
ALLOWED_CASH_FLOW_KINDS = (
    "initial_capital",
    "applied_fill",
    "corporate_action",
)


class AdmissionBlockedError(DomainValidationError):
    """Raised when run creation must be blocked with a fixed reason code."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details or {})


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return value


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(
        value, (Decimal, int, str)
    ):
        raise DomainValidationError(
            f"{field_name} must be Decimal, int, or str"
        )
    normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    if not normalized.is_finite():
        raise DomainValidationError(f"{field_name} must be finite")
    return normalized


def build_initial_equity_snapshot(
    *,
    run_id: str,
    first_formal_session_date,
    market_open_at: datetime,
    valuation_as_of: datetime,
    data_cutoff_at: datetime,
    initial_cash: Decimal | int | str,
    initial_quantities: Mapping[UUID, Decimal | int | str],
    close_facts: Iterable[ClosePriceFact],
    reporting_currency: str,
    accounting_currency: str | None = None,
    source_versions: Mapping[str, Any] | None = None,
    evidence_hash: str | None = None,
) -> InitialEquitySnapshot:
    """Freeze the E0 evidence from strictly-PIT raw close marks.

    A held quantity without a usable mark, or a mark whose knowledge time
    is not strictly before the open, blocks with ``MISSING_INITIAL_MARK``;
    a resulting E0 at or below zero blocks with
    ``NON_POSITIVE_INITIAL_EQUITY``.  Opening-time or later marks are never
    accepted as substitutes.
    """

    _aware(market_open_at, "market_open_at")
    _aware(valuation_as_of, "valuation_as_of")
    _aware(data_cutoff_at, "data_cutoff_at")
    if valuation_as_of >= market_open_at:
        raise AdmissionBlockedError(
            "MISSING_INITIAL_MARK",
            "the E0 valuation instant is not strictly earlier than the "
            "first formal open",
        )
    if accounting_currency is not None and (
        accounting_currency.strip().upper()
        != reporting_currency.strip().upper()
    ):
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            f"reporting currency {reporting_currency!r} does not equal the "
            f"accounting policy currency {accounting_currency!r}",
        )

    facts_by_instrument: dict[UUID, list[ClosePriceFact]] = {}
    for fact in close_facts:
        facts_by_instrument.setdefault(fact.instrument_id, []).append(fact)

    holdings = []
    missing_marks: list[str] = []
    for instrument_id, raw_quantity in sorted(
        initial_quantities.items(), key=lambda item: str(item[0])
    ):
        quantity = _decimal(raw_quantity, "initial quantity")
        if quantity <= 0:
            continue  # zero positions carry no mark requirement
        candidates = facts_by_instrument.get(instrument_id, [])
        # Strict PIT availability: the mark must have been knowable before
        # the first formal open; opening-time or later knowledge is
        # rejected outright.
        usable = [
            fact
            for fact in candidates
            if fact.currency == reporting_currency.strip().upper()
            and min(
                filter(
                    None,
                    (fact.evidence.known_at, fact.evidence.observed_at),
                )
            )
            < market_open_at
        ]
        if not usable:
            missing_marks.append(str(instrument_id))
            continue
        # The last officially closed session wins; ties are impossible
        # because sessions are unique per instrument source.
        mark = max(usable, key=lambda fact: fact.session_date)
        holdings.append((instrument_id, quantity, mark))

    if missing_marks:
        raise AdmissionBlockedError(
            "MISSING_INITIAL_MARK",
            "initial holdings lack a strictly pre-open PIT raw close mark",
            details={"missing_instrument_ids": sorted(missing_marks)},
        )

    from app.backtesting.analysis_inputs import InitialHolding

    try:
        return InitialEquitySnapshot(
            run_id=run_id,
            session_date=first_formal_session_date,
            market_open_at=market_open_at,
            valuation_as_of=valuation_as_of,
            data_cutoff_at=data_cutoff_at,
            reporting_currency=reporting_currency,
            cash=initial_cash,
            holdings=[
                InitialHolding(
                    instrument_id=instrument_id,
                    quantity=quantity,
                    currency=mark.currency,
                    close_price=mark.close_price,
                )
                for instrument_id, quantity, mark in holdings
            ],
            source_versions=source_versions,
            evidence_hash=evidence_hash,
        )
    except DomainValidationError as exc:
        message = str(exc)
        if "NON_POSITIVE_INITIAL_EQUITY" in message or "positive" in message:
            raise AdmissionBlockedError(
                "NON_POSITIVE_INITIAL_EQUITY",
                "the frozen initial equity E0 is not strictly positive",
            ) from exc
        raise


def verify_initial_portfolio_consistency(
    snapshot: InitialEquitySnapshot,
    portfolio_state: Any,
) -> None:
    """The frozen E0 must equal the actual initial portfolio state."""

    account = getattr(portfolio_state, "account", None)
    if account is None:
        raise DomainValidationError(
            "portfolio_state must expose its account state"
        )
    portfolio_equity = _decimal(getattr(account, "equity"), "account equity")
    if portfolio_equity != snapshot.equity_e0:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            f"the frozen E0 {snapshot.equity_e0} does not equal the actual "
            f"initial portfolio equity {portfolio_equity}",
        )


def freeze_rate_snapshot(
    pit_gateway: Any,
    *,
    expected_sessions,
    source_key: str,
    source_version: int,
    query_parameters: Mapping[str, Any] | None = None,
) -> PitRateSnapshot | None:
    """One-shot prefetch of the whole formal window's daily PIT rates."""

    sessions = tuple(expected_sessions)
    if not sessions:
        return None
    facts = pit_gateway.risk_free_rate_snapshot(
        PitRateSnapshotQuery(
            start_session=sessions[0],
            end_session=sessions[-1],
            expected_sessions=sessions,
        )
    )
    rates = {fact.session_date: fact.rate for fact in facts}
    return PitRateSnapshot(
        rates=rates,
        source_key=source_key,
        source_version=source_version,
        query_parameters=query_parameters,
        coverage_start=sessions[0],
        coverage_end=sessions[-1],
        expected_sessions=sessions,
    )


def ensure_modeled_cash_movements(
    movements: Iterable[tuple[str, Any]],
) -> None:
    """Block the run when any cash movement cannot be classified.

    Every movement must declare one of the modeled kinds; an unknown or
    unlabeled kind blocks run creation with the frozen
    ``UNMODELED_EXTERNAL_CASH_FLOW`` reason code instead of letting the
    analyzer guess or adjust returns later.
    """

    unmodeled: list[dict[str, Any]] = []
    for index, (kind, amount) in enumerate(movements):
        if kind not in ALLOWED_CASH_FLOW_KINDS:
            unmodeled.append(
                {
                    "index": index,
                    "kind": kind,
                    "amount": format(_decimal(amount, "movement amount"), "f"),
                }
            )
    if unmodeled:
        raise AdmissionBlockedError(
            "UNMODELED_EXTERNAL_CASH_FLOW",
            "one or more cash movements cannot be classified as initial "
            "capital, applied fills/fees, or modeled corporate actions",
            details={"unmodeled": unmodeled},
        )
