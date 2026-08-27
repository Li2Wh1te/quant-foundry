"""Run-admission gates for analyzer runs (task package 06 section 8/10.1).

The repository currently has no unified run-creation service; this module
provides the frozen admission gates plus one authoritative entry point so
analyzer-enabled runs cannot bypass them:

* :func:`build_initial_equity_snapshot` -- construct the E0 evidence from
  raw close marks whose knowledge time is *strictly* point-in-time before
  the first formal open.  A mark without strict ``known_at`` evidence is
  unusable (fail-closed); missing marks block with ``MISSING_INITIAL_MARK``
  and a non-positive E0 blocks with ``NON_POSITIVE_INITIAL_EQUITY``.
* :func:`verify_initial_portfolio_consistency` -- the frozen E0 must equal
  the actual initial portfolio state.
* :func:`freeze_rate_snapshot` -- one-shot prefetch of the whole formal
  window's PIT daily risk-free rates; facts without ``known_at`` or known
  only after their own session are treated as missing, and every retained
  fact's provenance is frozen into the snapshot (and its hash).
* :func:`ensure_modeled_cash_movements` -- unclassifiable cash movements
  block with ``UNMODELED_EXTERNAL_CASH_FLOW``.
* :func:`admit_analysis_run` -- the single admission entry point: it runs
  every gate, resolves the analyzer configuration, builds the admitted
  engine with its formal timeline pinned, and stamps the admission
  evidence onto the engine.  The deterministic runner refuses engines
  without this stamp, so production runs cannot skip admission.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.backtesting.analysis_inputs import (
    InitialEquitySnapshot,
    FormalSessionTimeline,
    PitRateSnapshot,
    canonical_evidence_json,
    evidence_digest,
    _frozen_mapping,
)
from app.backtesting.data.facts import ClosePriceFact, PitRateFact, PitRateSnapshotQuery
from app.backtesting.data.requests import QualityStatus
from app.backtesting.domain import DomainValidationError

__all__ = [
    "ALLOWED_CASH_FLOW_KINDS",
    "AnalysisAdmissionFailure",
    "AdmissionBlockedError",
    "AnalyzerRunAdmission",
    "FormalSessionTimeline",
    "admit_analysis_run",
    "build_admitted_runner",
    "build_initial_equity_snapshot",
    "bind_initial_equity_snapshot",
    "compute_portfolio_snapshot_binding",
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


@dataclass(frozen=True, slots=True)
class AnalysisAdmissionFailure:
    """Structured response for a run that never became runnable.

    Admission failures are returned to the run-creation caller as a stable
    ``blocked`` response.  They are deliberately not written to
    ``backtest_analysis_summaries``: that table represents an admitted run,
    while no run identity or immutable analyzer snapshot exists after a
    creation gate fails.
    """

    reason_code: str
    message: str
    run_id: str | None = None
    details: Mapping[str, Any] | None = None
    status: str = "blocked"
    persisted: bool = False

    def __post_init__(self) -> None:
        if self.status != "blocked":
            raise DomainValidationError(
                "analysis admission failure status must be 'blocked'"
            )
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise DomainValidationError("reason_code must be non-blank text")
        if not isinstance(self.message, str) or not self.message.strip():
            raise DomainValidationError("message must be non-blank text")
        object.__setattr__(self, "reason_code", self.reason_code.strip())
        object.__setattr__(self, "message", self.message.strip())
        object.__setattr__(
            self,
            "details",
            _frozen_mapping(self.details, "details"),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON/API shape without exposing frozen containers."""

        import json

        return json.loads(
            canonical_evidence_json(
                {
                    "status": self.status,
                    "run_id": self.run_id,
                    "reason_code": self.reason_code,
                    "message": self.message,
                    "details": dict(self.details or {}),
                    "persisted": self.persisted,
                }
            )
        )


class AdmissionBlockedError(DomainValidationError):
    """Raised when run creation must be blocked with a fixed reason code."""

    http_status_code = 422

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        run_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details or {})
        self.run_id = run_id
        self.failure = AnalysisAdmissionFailure(
            reason_code=reason_code,
            message=message,
            run_id=run_id,
            details=self.details,
        )

    def as_response(self) -> dict[str, Any]:
        """Return the stable structured creation-failure response."""

        return self.failure.as_dict()

    def with_run_id(self, run_id: str) -> "AdmissionBlockedError":
        """Attach the run identity once the coordinator has it available."""

        if self.run_id == run_id:
            return self
        return AdmissionBlockedError(
            self.reason_code,
            str(self),
            run_id=run_id,
            details=self.details,
        )


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DomainValidationError(f"{field_name} must be a datetime")
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


def _canonical_datetime_text(value: datetime | None) -> str | None:
    """Render provenance timestamps in the canonical UTC representation."""

    if value is None:
        return None
    normalized = _aware(value, "provenance timestamp").astimezone(timezone.utc)
    rendered = normalized.isoformat(timespec="microseconds")
    return rendered[:-6] + "Z"


def _mark_provenance(fact: ClosePriceFact) -> dict[str, Any]:
    """Canonical provenance block derived from the selected source fact."""

    return {
        "session_date": fact.session_date.isoformat(),
        "close_price": fact.close_price,
        "currency": fact.currency,
        "source": fact.evidence.source,
        "known_at": _canonical_datetime_text(fact.evidence.known_at),
        "observed_at": _canonical_datetime_text(fact.evidence.observed_at),
        "quality_status": fact.evidence.quality_status.value,
        "source_revision": fact.evidence.source_revision,
        "schema": (
            {"key": fact.schema.key, "version": fact.schema.version}
            if fact.schema is not None
            else None
        ),
    }


def _rate_provenance(
    fact: PitRateFact,
    *,
    session_open_at: datetime | None = None,
) -> dict[str, Any]:
    """Canonical provenance block derived from one selected rate fact."""

    return {
        "session_date": fact.session_date.isoformat(),
        "rate": fact.rate,
        "source": fact.evidence.source,
        "known_at": _canonical_datetime_text(fact.evidence.known_at),
        "data_cutoff_at": _canonical_datetime_text(
            fact.data_cutoff_at
        ),
        "session_open_at": _canonical_datetime_text(session_open_at),
        "observed_at": _canonical_datetime_text(fact.evidence.observed_at),
        "quality_status": fact.evidence.quality_status.value,
        "source_revision": fact.evidence.source_revision,
        "schema": (
            {"key": fact.schema.key, "version": fact.schema.version}
            if fact.schema is not None
            else None
        ),
    }


def _select_unique_close_fact(candidates: Sequence[ClosePriceFact]) -> ClosePriceFact:
    """Select one PIT close fact or reject an ambiguous revision set."""

    priority = max(
        (
            fact.session_date,
            fact.evidence.known_at,
            fact.evidence.source_revision or "",
        )
        for fact in candidates
    )
    leaders = [
        fact
        for fact in candidates
        if (
            fact.session_date,
            fact.evidence.known_at,
            fact.evidence.source_revision or "",
        )
        == priority
    ]
    evidence_keys = {
        canonical_evidence_json(_mark_provenance(fact)) for fact in leaders
    }
    if len(evidence_keys) > 1:
        raise AdmissionBlockedError(
            "MISSING_INITIAL_MARK",
            "multiple initial close facts share the same PIT revision key "
            "but carry different evidence",
            details={
                "session_date": priority[0].isoformat(),
                "source_revision": priority[2],
            },
        )
    # Identical duplicates are harmless; ordering is still deterministic.
    return min(leaders, key=lambda fact: canonical_evidence_json(_mark_provenance(fact)))


def _select_unique_rate_fact(
    candidates: Sequence[PitRateFact],
) -> tuple[PitRateFact | None, tuple[PitRateFact, ...]]:
    """Select one PIT rate fact and return conflicting leaders as evidence."""

    priority = max(
        (
            fact.evidence.known_at,
            fact.evidence.source_revision or "",
        )
        for fact in candidates
    )
    leaders = [
        fact
        for fact in candidates
        if (
            fact.evidence.known_at,
            fact.evidence.source_revision or "",
        )
        == priority
    ]
    evidence_keys = {
        canonical_evidence_json(_rate_provenance(fact)) for fact in leaders
    }
    if len(evidence_keys) > 1:
        # Ambiguous rate evidence is a deterministic coverage gap. Sharpe B
        # reports MISSING_PIT_RF while rate-independent analyzers continue.
        return None, tuple(
            sorted(
                leaders,
                key=lambda fact: canonical_evidence_json(_rate_provenance(fact)),
            )
        )
    return (
        min(leaders, key=lambda fact: canonical_evidence_json(_rate_provenance(fact))),
        (),
    )


def _portfolio_quantities(portfolio_state: Any) -> dict[UUID, Decimal]:
    """Read positive starting quantities from the authoritative portfolio."""

    positions = getattr(portfolio_state, "positions", None)
    if isinstance(positions, Mapping):
        values = positions.values()
    else:
        values = positions or ()
    quantities: dict[UUID, Decimal] = {}
    for position in values:
        instrument_id = getattr(position, "instrument_id", None)
        if not isinstance(instrument_id, UUID):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the initial portfolio contains a position without a stable "
                "instrument identity",
            )
        quantity = _decimal(getattr(position, "quantity"), "position quantity")
        if quantity > 0:
            quantities[instrument_id] = quantity
    return quantities


def compute_portfolio_snapshot_binding(
    portfolio_state: Any,
    *,
    reporting_currency: str,
) -> tuple[str, str]:
    """Return a canonical identity/hash of the authoritative start state.

    The payload includes every cash balance and all accounting-relevant
    position fields.  Two portfolios with equal total equity but different
    cash/holding composition therefore cannot share an admission binding.
    """

    snapshot = (
        portfolio_state.snapshot()
        if callable(getattr(portfolio_state, "snapshot", None))
        else portfolio_state
    )
    account = getattr(snapshot, "account", None)
    positions = getattr(snapshot, "positions", None)
    if account is None or positions is None:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "the initial portfolio cannot produce a complete immutable snapshot",
        )
    cash_balances = getattr(account, "cash_balances", None)
    if not isinstance(cash_balances, Mapping):
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "the initial portfolio snapshot has no cash-balance mapping",
        )
    if isinstance(positions, Mapping):
        position_values = positions.values()
    else:
        position_values = positions
    payload = {
        "contract": "initial_portfolio_snapshot_v1",
        "reporting_currency": reporting_currency.strip().upper(),
        "cash_balances": {
            str(currency).upper(): _decimal(amount, "portfolio cash")
            for currency, amount in sorted(
                cash_balances.items(), key=lambda item: str(item[0])
            )
        },
        "account": {
            name: _decimal(getattr(account, name), name)
            for name in (
                "available_cash",
                "frozen_cash",
                "margin_used",
                "margin_available",
                "equity",
            )
            if hasattr(account, name)
        },
        "positions": [
            {
                "instrument_id": str(position.instrument_id),
                "side": getattr(
                    getattr(position, "side", None),
                    "value",
                    getattr(position, "side", None),
                ),
                "quantity": _decimal(position.quantity, "position quantity"),
                "available_quantity": _decimal(
                    getattr(position, "available_quantity", position.quantity),
                    "position available quantity",
                ),
                "average_price": (
                    _decimal(position.average_price, "position average price")
                    if getattr(position, "average_price", None) is not None
                    else None
                ),
                "mark_price": (
                    _decimal(position.mark_price, "position mark price")
                    if getattr(position, "mark_price", None) is not None
                    else None
                ),
                "realized_pnl": _decimal(
                    getattr(position, "realized_pnl", Decimal("0")),
                    "position realized pnl",
                ),
                "unrealized_pnl": _decimal(
                    getattr(position, "unrealized_pnl", Decimal("0")),
                    "position unrealized pnl",
                ),
            }
            for position in sorted(
                position_values, key=lambda item: str(item.instrument_id)
            )
            if _decimal(position.quantity, "position quantity") > 0
        ],
        "as_of": getattr(snapshot, "as_of", None),
        "valuation_status": getattr(
            getattr(snapshot, "valuation_status", None),
            "value",
            getattr(snapshot, "valuation_status", None),
        ),
    }
    portfolio_hash = evidence_digest(payload)
    return f"portfolio:{portfolio_hash}", portfolio_hash


def bind_initial_equity_snapshot(
    snapshot: InitialEquitySnapshot,
    portfolio_state: Any,
) -> InitialEquitySnapshot:
    """Validate and immutably bind E0 to the exact starting portfolio."""

    verify_initial_portfolio_consistency(snapshot, portfolio_state)
    snapshot_id, snapshot_hash = compute_portfolio_snapshot_binding(
        portfolio_state,
        reporting_currency=snapshot.reporting_currency,
    )
    if snapshot.portfolio_snapshot_hash is not None:
        if (
            snapshot.portfolio_snapshot_id != snapshot_id
            or snapshot.portfolio_snapshot_hash != snapshot_hash
        ):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the E0 snapshot is bound to a different initial portfolio",
            )
        return snapshot
    # ``evidence_hash`` is derived, so clear it while replacing the binding
    # fields and let the DTO recompute the complete E0 evidence digest.
    return replace(
        snapshot,
        portfolio_snapshot_id=snapshot_id,
        portfolio_snapshot_hash=snapshot_hash,
        evidence_hash=None,
    )


def build_initial_equity_snapshot(
    *,
    run_id: str,
    first_formal_session_date: date,
    formal_sessions: Sequence[date] = (),
    formal_timeline: FormalSessionTimeline | None = None,
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

    Fail-closed PIT rule: a mark is usable only when its fact declares a
    ``known_at`` instant strictly earlier than the open.  Facts without
    strict knowledge-time evidence are never accepted on ``observed_at``
    alone.  Missing marks block with ``MISSING_INITIAL_MARK``; a resulting
    E0 at or below zero blocks with ``NON_POSITIVE_INITIAL_EQUITY``.

    The returned snapshot's ``evidence_hash`` is always recomputed by the
    DTO from the full payload including the per-mark provenance, so caller
    claims cannot shape the input-evidence signature.
    """

    _aware(market_open_at, "market_open_at")
    _aware(valuation_as_of, "valuation_as_of")
    _aware(data_cutoff_at, "data_cutoff_at")
    # Kept as a source-compatible parameter for older callers, but never
    # trusted.  The returned DTO always recomputes its digest from the
    # selected facts below.
    del evidence_hash
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
        usable = [
            fact
            for fact in candidates
            if fact.currency == reporting_currency.strip().upper()
            and fact.session_date < first_formal_session_date
            and fact.session_date <= valuation_as_of.date()
            # Fail-closed: no strict knowledge-time evidence means the
            # mark is NOT point-in-time provable -- observed_at alone is
            # when we read it, not when the market knew it.
            and fact.evidence.known_at is not None
            and fact.evidence.quality_status is QualityStatus.COMPLETE
            and fact.evidence.known_at < market_open_at
            and fact.evidence.known_at <= data_cutoff_at
        ]
        if not usable:
            missing_marks.append(str(instrument_id))
            continue
        # The last officially closed session wins.  If a provider returns
        # multiple revisions for that close, freeze the latest revision that
        # was already known by the declared cutoff rather than depending on
        # gateway iteration order.
        mark = _select_unique_close_fact(usable)
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
                    mark_evidence=_mark_provenance(mark),
                )
                for instrument_id, quantity, mark in holdings
            ],
            source_versions=source_versions,
            formal_timeline=(
                formal_timeline
                if formal_timeline is not None
                else (
                    FormalSessionTimeline(tuple(formal_sessions))
                    if formal_sessions
                    else None
                )
            ),
            formal_sessions=tuple(formal_sessions),
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
    """The frozen E0 must equal the actual initial portfolio state.

    Equity alone is not a sufficient identity: two portfolios with different
    cash/position compositions can have the same total value while producing
    different first-day returns.  Whenever the runtime state exposes the
    normal ``AccountState``/``PositionState`` shape, both the policy-currency
    cash balance and every non-zero position quantity are compared as well.
    """

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

    cash_balances = getattr(account, "cash_balances", None)
    positions = getattr(portfolio_state, "positions", None)
    # Keep this helper useful for small unit-test doubles that only expose
    # ``account.equity``; the authoritative admission coordinator below
    # requires the complete runtime shape before it invokes this gate.
    if cash_balances is None or positions is None:
        return
    currency = snapshot.reporting_currency
    try:
        actual_cash = _decimal(cash_balances[currency], "account cash")
    except (KeyError, TypeError) as exc:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            f"the initial account has no {currency} cash balance",
        ) from exc
    if actual_cash != snapshot.cash:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            f"the frozen initial cash {snapshot.cash} does not equal the "
            f"actual initial portfolio cash {actual_cash}",
        )
    # A no-FX E0 snapshot cannot silently ignore another currency's opening
    # balance.  Zero balances are harmless, but any non-zero extra balance
    # means the frozen composition is incomplete.
    for currency_name, amount in cash_balances.items():
        if str(currency_name).upper() == currency:
            continue
        if _decimal(amount, "account cash") != 0:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the initial portfolio contains a non-reporting-currency "
                f"cash balance ({currency_name}) without an FX valuation",
            )

    expected_quantities = {
        holding.instrument_id: holding.quantity for holding in snapshot.holdings
    }
    if isinstance(positions, Mapping):
        actual_positions = positions.values()
    else:
        actual_positions = positions
    actual_quantities: dict[UUID, Decimal] = {}
    for position in actual_positions:
        instrument_id = getattr(position, "instrument_id", None)
        quantity = _decimal(getattr(position, "quantity"), "position quantity")
        if quantity <= 0:
            continue
        if not isinstance(instrument_id, UUID):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the initial portfolio contains a position without a stable "
                "instrument identity",
            )
        actual_quantities[instrument_id] = quantity
    if actual_quantities != expected_quantities:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "the frozen initial holdings do not equal the actual initial "
            "portfolio positions",
            details={
                "expected_positions": {
                    str(key): format(value, "f")
                    for key, value in expected_quantities.items()
                },
                "actual_positions": {
                    str(key): format(value, "f")
                    for key, value in actual_quantities.items()
                },
            },
        )


def freeze_rate_snapshot(
    pit_gateway: Any,
    *,
    expected_sessions: Sequence[date],
    source_key: str,
    source_version: int,
    query_parameters: Mapping[str, Any] | None = None,
    session_open_at: Mapping[date, datetime] | Callable[[date], datetime],
) -> PitRateSnapshot | None:
    """One-shot prefetch of the whole formal window's daily PIT rates.

    Rate facts without strict ``known_at`` evidence, or known only after
    their own session, fail the PIT gate and count as missing days rather
    than being silently accepted.  Every retained fact's provenance is
    frozen into the snapshot and participates in its hash.
    """

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
    eligible_by_date: dict[date, list[PitRateFact]] = {}
    expected_set = set(sessions)

    def resolve_open(session_date: date) -> datetime:
        value = (
            session_open_at(session_date)
            if callable(session_open_at)
            else session_open_at.get(session_date)
        )
        if value is None or value.tzinfo is None or value.utcoffset() is None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                f"missing timezone-aware session open for {session_date.isoformat()}",
            )
        return value

    for fact in facts:
        # Fail-closed PIT: a daily rate must be knowable no later than its
        # own session; anything else is future knowledge and stays missing.
        if fact.evidence.known_at is None:
            continue
        if fact.session_date not in expected_set:
            continue
        if fact.evidence.quality_status is not QualityStatus.COMPLETE:
            continue
        cutoff = fact.data_cutoff_at
        session_open = resolve_open(fact.session_date)
        # The source cutoff must be no later than that session's open;
        # effective_at/session_date is not a license to use a post-close fact.
        if cutoff > session_open or fact.evidence.known_at > cutoff:
            continue
        if fact.evidence.known_at.date() > fact.session_date:
            continue
        eligible_by_date.setdefault(fact.session_date, []).append(fact)

    rates: dict[date, Decimal] = {}
    fact_evidence: dict[str, dict[str, Any]] = {}
    for session_date, candidates in eligible_by_date.items():
        # Freeze the latest source revision known by the session, independent
        # of the order in which a gateway happens to return duplicate facts.
        fact, ambiguous = _select_unique_rate_fact(candidates)
        if fact is None:
            fact_evidence[session_date.isoformat()] = {
                "status": "ambiguous",
                "session_date": session_date.isoformat(),
                "candidates": [
                    _rate_provenance(
                        candidate,
                        session_open_at=resolve_open(session_date),
                    )
                    for candidate in ambiguous
                ],
            }
            continue
        rates[fact.session_date] = fact.rate
        fact_evidence[fact.session_date.isoformat()] = _rate_provenance(
            fact,
            session_open_at=resolve_open(fact.session_date),
        )
    return PitRateSnapshot(
        rates=rates,
        source_key=source_key,
        source_version=source_version,
        query_parameters=query_parameters,
        coverage_start=sessions[0],
        coverage_end=sessions[-1],
        expected_sessions=sessions,
        fact_evidence=fact_evidence,
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
        normalized_amount = _decimal(amount, "movement amount")
        if kind not in ALLOWED_CASH_FLOW_KINDS:
            unmodeled.append(
                {
                    "index": index,
                    "kind": kind,
                    "amount": format(normalized_amount, "f"),
                }
            )
    if unmodeled:
        raise AdmissionBlockedError(
            "UNMODELED_EXTERNAL_CASH_FLOW",
            "one or more cash movements cannot be classified as initial "
            "capital, applied fills/fees, or modeled corporate actions",
            details={"unmodeled": unmodeled},
        )


@dataclass(frozen=True, slots=True)
class AnalyzerRunAdmission:
    """Everything a runner needs after successful admission."""

    engine: Any
    initial_equity_snapshot: InitialEquitySnapshot
    rate_snapshot: PitRateSnapshot | None
    formal_timeline: FormalSessionTimeline
    # Compatibility alias for callers that still consume a tuple.
    formal_sessions: tuple[date, ...]
    admission_evidence: Mapping[str, Any]
    # A coordinator-issued capability token.  It is intentionally not part
    # of the public constructor; the runner verifies it against the
    # process-local engine registry instead of trusting mutable engine flags.
    _capability_token: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.formal_timeline, FormalSessionTimeline):
            raise DomainValidationError(
                "formal_timeline must be a FormalSessionTimeline"
            )
        if (
            not isinstance(self.formal_sessions, Sequence)
            or isinstance(self.formal_sessions, (str, bytes, bytearray))
        ):
            raise DomainValidationError("formal_sessions must be an ordered sequence")
        sessions = tuple(self.formal_sessions)
        if sessions != self.formal_timeline.sessions:
            raise DomainValidationError(
                "formal_sessions must equal formal_timeline.sessions"
            )
        object.__setattr__(self, "formal_sessions", sessions)
        object.__setattr__(
            self,
            "admission_evidence",
            _frozen_mapping(self.admission_evidence, "admission_evidence"),
        )

    def build_runner(self, **runner_kwargs: Any) -> Any:
        """Construct the only runner shape allowed to consume this engine.

        The admitted engine is injected by this method so callers cannot
        accidentally replace it with a hand-built ``AnalyzerEngine``.
        ``DeterministicBacktestRunner`` is imported lazily to keep the data
        and runtime modules acyclic.
        """

        if "analysis_engine" in runner_kwargs:
            raise DomainValidationError(
                "runner_kwargs must not provide analysis_engine; the admitted "
                "engine is injected by the coordinator"
            )
        from app.backtesting.runtime import DeterministicBacktestRunner

        # Pass the coordinator object itself.  The runner verifies its
        # capability token against the process-local admission registry;
        # mutable engine attributes are never used as the trust boundary.
        runner_kwargs["analysis_admission"] = self
        return DeterministicBacktestRunner(**runner_kwargs)


def _admit_analysis_run_unwrapped(
    *,
    run_id: str,
    formal_sessions: Sequence[date],
    first_step_sequence: int = 0,
    market_open_at: datetime,
    valuation_as_of: datetime,
    data_cutoff_at: datetime,
    reporting_currency: str,
    initial_cash: Decimal | int | str,
    initial_portfolio_state: Any,
    analyzer_specs: Sequence[Any],
    close_facts: Iterable[ClosePriceFact] = (),
    initial_quantities: Mapping[UUID, Decimal | int | str] | None = None,
    accounting_currency: str | None = None,
    pit_gateway: Any | None = None,
    rate_source_key: str | None = None,
    rate_source_version: int | None = None,
    rate_query_parameters: Mapping[str, Any] | None = None,
    rate_session_open_at: Mapping[date, datetime] | Callable[[date], datetime] | None = None,
    cash_movements: Iterable[tuple[str, Any]] = (),
    analyzer_engine: Any | None = None,
) -> AnalyzerRunAdmission:
    """Single admission entry point composing every frozen gate.

    Order follows task package 06 section 10.1: analyzer configuration
    validation (inside engine creation, against the frozen registry
    contracts), currency gates, E0 preflight, portfolio consistency,
    external-cash-flow preflight, rate prefetch for Sharpe B, then engine
    construction with the formal timeline pinned and the admission stamp
    applied.  Any failure raises :class:`AdmissionBlockedError` (or a
    domain error) and produces no runnable engine.
    """

    if (
        not isinstance(formal_sessions, Sequence)
        or isinstance(formal_sessions, (str, bytes, bytearray))
    ):
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "formal_sessions must be an ordered sequence",
        )
    if not formal_sessions:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "run admission requires the official formal session sequence",
        )
    if (
        not isinstance(analyzer_specs, Sequence)
        or isinstance(analyzer_specs, (str, bytes, bytearray))
    ):
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "analyzer_specs must be an ordered sequence",
        )
    analyzer_specs = tuple(analyzer_specs)
    from app.backtesting.analyzers import (
        CONFIG_RF_ANALYZER_KEY,
        PIT_RF_ANALYZER_KEY,
        SHARPE_SIMPLE_ANALYZER_KEY,
    )

    sharpe_keys = {
        SHARPE_SIMPLE_ANALYZER_KEY,
        PIT_RF_ANALYZER_KEY,
        CONFIG_RF_ANALYZER_KEY,
    }
    selected_sharpe_specs = [
        spec
        for spec in analyzer_specs
        if getattr(spec, "analyzer_key", None) in sharpe_keys
    ]
    if len(selected_sharpe_specs) != 1:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "every run must explicitly select exactly one Sharpe analyzer",
        )
    # Validate the complete analyzer contract before touching cash movements,
    # E0 facts, or an external PIT gateway. This preserves the frozen failure
    # precedence and prevents invalid configurations from causing I/O.
    try:
        from app.backtesting.analyzers import (
            AnalyzerSpec,
            resolve_config_rf_daily,
            validate_v1_analyzer_spec,
        )

        seen_identities: set[tuple[str, int]] = set()
        seen_outputs: set[tuple[str, str]] = set()
        for spec in analyzer_specs:
            if not isinstance(spec, AnalyzerSpec):
                raise DomainValidationError(
                    "analyzer_specs entries must be AnalyzerSpec instances"
                )
            identity = (spec.analyzer_key, spec.analyzer_version)
            if identity in seen_identities:
                raise DomainValidationError(
                    f"analyzer {spec.display_identity} is configured more than once"
                )
            seen_identities.add(identity)
            validate_v1_analyzer_spec(spec)
            for descriptor in spec.output_contract:
                logical_key = (descriptor.metric_key, descriptor.formula_version)
                if logical_key in seen_outputs:
                    raise DomainValidationError(
                        f"metric {logical_key[0]}@{logical_key[1]} has multiple producers"
                    )
                seen_outputs.add(logical_key)
            if spec.analyzer_key == CONFIG_RF_ANALYZER_KEY:
                resolve_config_rf_daily(spec)
    except AdmissionBlockedError:
        raise
    except Exception as exc:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            f"invalid analyzer configuration: {exc}",
        ) from exc
    timeline = FormalSessionTimeline(formal_sessions)
    sessions = timeline.sessions
    if isinstance(first_step_sequence, bool) or not isinstance(
        first_step_sequence, int
    ) or first_step_sequence < 0:
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "first_step_sequence must be a non-negative integer",
        )

    # The admission coordinator is deliberately the only production path
    # that can attest a runner.  Requiring the complete runtime shape here
    # prevents an equity-only test double (or a partially initialized
    # account) from hiding a composition mismatch behind the same E0.
    initial_account = getattr(initial_portfolio_state, "account", None)
    if (
        not hasattr(initial_portfolio_state, "positions")
        or getattr(initial_portfolio_state, "positions", None) is None
        or initial_account is None
        or getattr(initial_account, "cash_balances", None) is None
    ):
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            "run admission requires the complete initial portfolio state",
        )

    # Derive the held quantities from the same immutable starting portfolio
    # that is checked below.  A caller may provide an explicit declaration,
    # but it can never omit a real position and still pass admission.
    if initial_quantities is None:
        initial_quantities = _portfolio_quantities(initial_portfolio_state)

    # 1. Currency and E0 preflight from strictly pre-open PIT marks.
    snapshot = build_initial_equity_snapshot(
        run_id=run_id,
        first_formal_session_date=sessions[0],
        formal_sessions=sessions,
        market_open_at=market_open_at,
        valuation_as_of=valuation_as_of,
        data_cutoff_at=data_cutoff_at,
        initial_cash=initial_cash,
        initial_quantities=initial_quantities,
        close_facts=close_facts,
        reporting_currency=reporting_currency,
        accounting_currency=accounting_currency,
        formal_timeline=timeline,
    )

    # 2. The frozen E0 must describe and cryptographically bind the exact
    # starting portfolio, not merely an equal total equity value.
    snapshot = bind_initial_equity_snapshot(snapshot, initial_portfolio_state)

    # 3. External cash-flow preflight follows all E0/portfolio gates. This
    # preserves the task-package failure precedence without touching the PIT
    # rate gateway for a run that already has invalid cash-flow evidence.
    ensure_modeled_cash_movements(cash_movements)

    # 4. Sharpe B requires one frozen PIT window.  Coverage gaps remain
    # explicit input evidence and make only Sharpe B unavailable; they must
    # not block turnover or fee producers from running.
    needs_rates = any(
        getattr(spec, "analyzer_key", None) == PIT_RF_ANALYZER_KEY
        for spec in analyzer_specs
    )
    rate_snapshot: PitRateSnapshot | None = None
    if needs_rates:
        if pit_gateway is None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "sharpe_pit_rf runs require a PIT analysis gateway to "
                "prefetch the whole formal window",
            )
        if rate_source_key is None or rate_source_version is None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the rate snapshot source key/version must be declared at "
                "run admission",
            )
        if rate_session_open_at is None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "Sharpe B admission requires the formal session-open mapping "
                "used as each rate's PIT boundary",
            )
        rate_snapshot = freeze_rate_snapshot(
            pit_gateway,
            expected_sessions=sessions,
            source_key=rate_source_key,
            source_version=rate_source_version,
            query_parameters=rate_query_parameters,
            session_open_at=rate_session_open_at,
        )
        if rate_snapshot is None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the PIT risk-free rate snapshot could not be constructed",
            )

    # 5. Engine creation validates specs against the frozen registry
    # contracts and freezes the Decimal policy.
    from app.backtesting.analyzers import AnalyzerEngine

    if analyzer_engine is None:
        engine = AnalyzerEngine.create(
            snapshot,
            analyzer_specs,
            frozen_rate_snapshot=rate_snapshot,
            accounting_currency=accounting_currency or reporting_currency,
            formal_timeline=timeline,
            first_step_sequence=first_step_sequence,
        )
    else:
        # Existing engines are accepted only after the exact same gates have
        # run and their frozen constructor inputs match the coordinator's
        # newly selected evidence.  Passing an engine here never bypasses
        # admission; it merely preserves object identity for integrations
        # that already hold the engine reference.
        engine = analyzer_engine
        if not isinstance(engine, AnalyzerEngine):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine must be an AnalyzerEngine",
            )
        if getattr(engine, "_admission_evidence", None) is not None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine has already been admitted",
            )
        if getattr(engine, "run_id", None) != run_id:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine belongs to a different run",
            )
        if tuple(getattr(engine, "specs", ())) != tuple(analyzer_specs):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine specs differ from admission",
            )
        existing_analysis_snapshot = engine.snapshot()
        if (
            existing_analysis_snapshot.equity_observations
            or existing_analysis_snapshot.fill_observations
        ):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine must contain no observations "
                "or fills before formal admission",
            )
        if (
            getattr(engine, "formal_timeline", None) != timeline
            or getattr(engine, "_formal_sessions", None) != sessions
            or getattr(engine, "_first_step_sequence", None) != first_step_sequence
        ):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine is bound to a different formal "
                "timeline or first step sequence",
            )
        existing_snapshot = getattr(engine, "_initial_equity_snapshot", None)
        if existing_snapshot is None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine has no initial E0 snapshot",
            )
        # Compare every E0 field that existed before admission.  The only
        # permitted difference is the coordinator's portfolio binding, which
        # is added after this check and is itself verified against the same
        # authoritative portfolio above.
        try:
            existing_e0_payload = existing_snapshot.evidence_payload()
            candidate_e0_payload = snapshot.evidence_payload()
        except Exception as exc:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine carries invalid E0 evidence",
            ) from exc
        for field_name in (
            "run_id",
            "session_date",
            "market_open_at",
            "valuation_as_of",
            "data_cutoff_at",
            "reporting_currency",
            "cash",
            "holdings",
            "market_value",
            "equity_e0",
            "source_versions",
            "formal_timeline",
            "formal_sessions",
            "timeline_hash",
        ):
            if existing_e0_payload.get(field_name) != candidate_e0_payload.get(
                field_name
            ):
                raise AdmissionBlockedError(
                    "INVALID_ANALYZER_CONFIG",
                    "the supplied analyzer engine is bound to different E0 evidence",
                )
        for field_name in ("portfolio_snapshot_id", "portfolio_snapshot_hash"):
            existing_value = existing_e0_payload.get(field_name)
            candidate_value = candidate_e0_payload.get(field_name)
            if existing_value is not None and existing_value != candidate_value:
                raise AdmissionBlockedError(
                    "INVALID_ANALYZER_CONFIG",
                    "the supplied analyzer engine is bound to different E0 evidence",
                )
        # ``bind_initial_equity_snapshot`` legitimately changes the derived
        # evidence hash when it adds the authoritative portfolio binding to a
        # pre-admission E0.  Validate the existing hash against its own
        # payload first, then require an exact hash match once that binding is
        # already present.  This detects object-level tampering without
        # rejecting the one intentional pre-admission transformation.
        try:
            existing_expected_hash = evidence_digest(
                existing_snapshot._evidence_payload_without_hash()
            )
        except Exception as exc:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine carries invalid E0 evidence",
            ) from exc
        if existing_e0_payload.get("evidence_hash") != existing_expected_hash:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine carries a tampered E0 evidence hash",
            )
        if existing_e0_payload.get("portfolio_snapshot_id") is not None:
            if existing_e0_payload.get("evidence_hash") != candidate_e0_payload.get(
                "evidence_hash"
            ):
                raise AdmissionBlockedError(
                    "INVALID_ANALYZER_CONFIG",
                    "the supplied analyzer engine is bound to different E0 evidence",
                )
        try:
            from app.backtesting.registry import (
                ANNUAL_RATE_CONVERTER_KEY_ANNUAL_RATE_DIV_252,
                build_default_component_registry,
            )

            registry = build_default_component_registry()
            expected_registry_snapshot = {
                "registry_entries": [
                    registry.resolve(
                        spec.analyzer_key,
                        spec.analyzer_version,
                    ).describe()
                    for spec in analyzer_specs
                ],
                "annual_rate_converter": (
                    registry.resolve(
                        ANNUAL_RATE_CONVERTER_KEY_ANNUAL_RATE_DIV_252,
                        1,
                    ).describe()
                    if any(
                        spec.analyzer_key == CONFIG_RF_ANALYZER_KEY
                        for spec in analyzer_specs
                    )
                    else None
                ),
            }
            if canonical_evidence_json(getattr(engine, "_registry_snapshot", {})) != (
                canonical_evidence_json(expected_registry_snapshot)
            ):
                raise AdmissionBlockedError(
                    "INVALID_ANALYZER_CONFIG",
                    "the supplied analyzer engine carries a different Registry snapshot",
                )
        except AdmissionBlockedError:
            raise
        except Exception as exc:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine carries an invalid Registry snapshot",
            ) from exc
        existing_rate = getattr(engine, "_rate_snapshot", None)
        if (
            getattr(existing_rate, "snapshot_hash", None)
            != getattr(rate_snapshot, "snapshot_hash", None)
        ):
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "the supplied analyzer engine is bound to different rate evidence",
            )
        if getattr(engine, "finalized_status", None) is not None:
            raise AdmissionBlockedError(
                "INVALID_ANALYZER_CONFIG",
                "a finalized analyzer engine cannot be admitted again",
            )
        # All checks above are read-only.  Keep the compatibility assertion
        # before replacing the pre-admission E0 so a late timeline failure
        # cannot leave the caller's engine partially mutated.
        engine.attach_formal_timeline(
            timeline, first_step_sequence=first_step_sequence
        )
        engine._initial_equity_snapshot = snapshot

    # 6. Stamp the admission evidence; the runner refuses unstamped
    # engines, making the gates impossible to bypass in production.
    admission_evidence = {
        "run_id": run_id,
        "initial_equity_hash": snapshot.evidence_hash,
        "portfolio_snapshot_id": snapshot.portfolio_snapshot_id,
        "portfolio_snapshot_hash": snapshot.portfolio_snapshot_hash,
        "rate_snapshot_hash": (
            rate_snapshot.snapshot_hash if rate_snapshot is not None else None
        ),
        "formal_first_session": sessions[0].isoformat(),
        "formal_last_session": sessions[-1].isoformat(),
        "formal_session_count": len(sessions),
        "formal_sessions": tuple(day.isoformat() for day in sessions),
        "formal_timeline": timeline.as_payload(),
        "timeline_hash": timeline.timeline_hash,
        "first_step_sequence": first_step_sequence,
    }
    capability_token = engine._mark_admitted(admission_evidence)
    frozen_admission_evidence = engine.admission_evidence
    if frozen_admission_evidence is None:  # pragma: no cover - mark guarantees it
        raise DomainValidationError("admission evidence was not frozen")
    admission = AnalyzerRunAdmission(
        engine=engine,
        initial_equity_snapshot=snapshot,
        rate_snapshot=rate_snapshot,
        formal_timeline=timeline,
        formal_sessions=sessions,
        admission_evidence=frozen_admission_evidence,
    )
    object.__setattr__(admission, "_capability_token", capability_token)
    return admission


def admit_analysis_run(**kwargs: Any) -> AnalyzerRunAdmission:
    """Run admission and expose every creation rejection uniformly.

    The caller receives :class:`AdmissionBlockedError` for all four
    creation-gate reason families.  Its ``as_response()`` payload is the
    public ``blocked`` representation; no analysis-summary row is written
    because an analyzer-enabled run was never created.
    """

    run_id = kwargs.get("run_id")
    try:
        return _admit_analysis_run_unwrapped(**kwargs)
    except AdmissionBlockedError as exc:
        raise exc.with_run_id(run_id) from exc
    except Exception as exc:
        # Analyzer/DTO configuration errors all belong to the fixed
        # INVALID_ANALYZER_CONFIG admission family.  Programming failures
        # outside the documented domain types are not translated here.
        from app.backtesting.analyzers import AnalyzerConfigurationError

        if not isinstance(exc, (AnalyzerConfigurationError, DomainValidationError)):
            raise
        raise AdmissionBlockedError(
            "INVALID_ANALYZER_CONFIG",
            str(exc),
            run_id=run_id,
            details={"error_type": type(exc).__name__},
        ) from exc


def build_admitted_runner(
    *,
    admission_kwargs: Mapping[str, Any],
    runner_kwargs: Mapping[str, Any],
) -> Any:
    """Build a runner only after the complete analyzer admission succeeds.

    ``formal_sessions`` may be supplied explicitly in ``admission_kwargs``;
    when omitted it is derived from the runner's immutable ``TimeAxis``.
    The same ``initial_portfolio`` object is used for the E0 consistency gate
    and for runner construction, closing the denominator mismatch reported
    by production runs.
    """

    admission = dict(admission_kwargs)
    runner = dict(runner_kwargs)
    if "analysis_engine" in runner:
        raise DomainValidationError(
            "runner_kwargs must not provide analysis_engine; use the admitted "
            "engine returned by the coordinator"
        )
    if "formal_sessions" not in admission:
        axis = runner.get("axis")
        if axis is None:
            raise DomainValidationError(
                "formal_sessions or runner axis is required for run admission"
            )
        try:
            admission["formal_sessions"] = tuple(
                date.fromisoformat(step.metadata["session_date"])
                for step in tuple(axis)
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise DomainValidationError(
                "runner axis must expose the official session_date timeline"
            ) from exc
    admission.setdefault(
        "rate_session_open_at",
        {
            date.fromisoformat(step.metadata["session_date"]): step.start_time
            for step in tuple(runner.get("axis", ()))
            if isinstance(getattr(step, "metadata", None), Mapping)
            and isinstance(step.metadata.get("session_date"), str)
        },
    )
    admission.setdefault(
        "initial_portfolio_state", runner.get("initial_portfolio")
    )
    if "first_step_sequence" not in admission:
        axis = runner.get("axis")
        if axis is not None:
            try:
                admission["first_step_sequence"] = axis.at(0).sequence
            except (AttributeError, IndexError):
                raise DomainValidationError(
                    "runner axis must contain an official first step"
                ) from None
    if admission["initial_portfolio_state"] is None:
        raise DomainValidationError(
            "initial_portfolio_state or runner initial_portfolio is required"
        )
    admitted = admit_analysis_run(**admission)
    return admitted.build_runner(**runner)
