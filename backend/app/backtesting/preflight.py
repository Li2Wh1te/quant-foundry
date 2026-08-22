"""Pre-creation eligibility preflight for non-zero initial positions.

The service answers one question before a ``backtest_run`` is created: is
every fact needed to account for each opening position available, valid, and
PIT-consistent?  It depends only on the :class:`BacktestPreflightGateway`
protocol; no ORM, FastAPI, Tushare, or database type appears here.  Callers
must treat a ``blocked`` report as a hard gate and must not create a run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol, Sequence, runtime_checkable
from uuid import UUID

from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    PositionSide,
)
from app.backtesting.spec import BacktestSpec, InitialPositionInput


class PreflightStatus(StrEnum):
    """Overall or per-position preflight outcome."""

    READY = "ready"
    BLOCKED = "blocked"


class CheckStatus(StrEnum):
    """Outcome of one preflight category for one instrument."""

    OK = "ok"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"


class IssueSeverity(StrEnum):
    """Issue severity; every current preflight failure is blocking."""

    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ResolvedInstrument:
    """PIT-resolved instrument existence plus its trading calendar."""

    instrument_id: UUID
    calendar_id: str | None


@dataclass(frozen=True, slots=True)
class IdentityMappingEntry:
    """One historical trading-code mapping valid at the target point in time."""

    symbol: str
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True, slots=True)
class InstrumentRulesFacts:
    """Resolved rule package facts, including declared applicability."""

    rule_package_id: str
    requires_trading_status_facts: bool


@dataclass(frozen=True, slots=True)
class SettlementAndSellRules:
    """Settlement rule and sell-availability rule resolved for an instrument.

    ``settlement_rule_kind`` carries the rule category so the service can
    verify it belongs to the capability range allowed in the first phase;
    a bare unknown ID must not pass as a valid settlement rule.
    """

    settlement_rule_id: str | None
    sell_rule_id: str | None
    settlement_rule_kind: str | None = None


class SettlementRuleKind(StrEnum):
    """Settlement rule categories supported by the first-phase engine."""

    T1_BEFORE_OPEN_MATCH = "t1_before_open_match"


# Capability range allowed for official first-phase backtests.
SUPPORTED_SETTLEMENT_RULE_KINDS = frozenset({SettlementRuleKind.T1_BEFORE_OPEN_MATCH})


@dataclass(frozen=True, slots=True)
class RawValuationPrice:
    """Raw (unadjusted) price bound to exactly one trading session.

    Carrying the session explicitly lets the service reject stale reads that
    silently substitute a previous session's price.  The price is normalized
    here so a misbehaving gateway cannot smuggle in binary floats or other
    non-decimal values; invalid input is rejected at construction instead of
    blowing up later during arithmetic.
    """

    session: date
    price: Decimal | int | str

    def __post_init__(self) -> None:
        # The session must be a plain calendar date; a misbehaving gateway
        # must fail here instead of crashing later on ``session.isoformat()``.
        if isinstance(self.session, datetime) or not isinstance(self.session, date):
            raise DomainValidationError(
                "RawValuationPrice.session must be a calendar date"
            )
        if isinstance(self.price, bool) or isinstance(self.price, float) or (
            not isinstance(self.price, (Decimal, int, str))
        ):
            raise DomainValidationError(
                "RawValuationPrice.price must be Decimal, int, or str; "
                "float and other types are unsupported"
            )
        try:
            normalized = Decimal(str(self.price))
        except (InvalidOperation, ValueError) as exc:
            raise DomainValidationError(
                "RawValuationPrice.price must be a valid decimal"
            ) from exc
        if not normalized.is_finite():
            raise DomainValidationError("RawValuationPrice.price must be finite")
        object.__setattr__(self, "price", normalized)


@dataclass(frozen=True, slots=True)
class FactCheckOutcome:
    """Result of a gateway-side fact completeness check."""

    complete: bool
    detail: str | None = None


@runtime_checkable
class BacktestPreflightGateway(Protocol):
    """Read-only query protocol answering the facts preflight needs.

    Implementations return PIT-consistent facts for the requested ``as_of``
    or date range.  Returning ``None``, an empty sequence, or an incomplete
    outcome means "fact unavailable", which blocks the affected position.
    """

    def resolve_instrument(self, instrument_id: UUID, *, as_of: date) -> ResolvedInstrument | None: ...

    def resolve_identity_mapping(
        self, instrument_id: UUID, *, as_of: date
    ) -> Sequence[IdentityMappingEntry]: ...

    def resolve_instrument_rules(
        self, instrument_id: UUID, *, as_of: date
    ) -> InstrumentRulesFacts | None: ...

    def resolve_settlement_and_sell_rules(
        self, instrument_id: UUID, *, as_of: date
    ) -> SettlementAndSellRules | None: ...

    def find_first_trading_session_on_or_after(
        self, calendar_id: str, start_date: date
    ) -> date | None: ...

    def get_raw_valuation_price(
        self, instrument_id: UUID, session: date
    ) -> RawValuationPrice | None: ...

    def check_required_corporate_actions(
        self, instrument_id: UUID, *, start_date: date, end_date: date
    ) -> FactCheckOutcome: ...

    def check_required_trading_status(
        self, instrument_id: UUID, *, start_date: date, end_date: date
    ) -> FactCheckOutcome: ...


@dataclass(frozen=True, slots=True)
class InitialPositionPreflightIssue:
    """One locatable blocking reason."""

    code: str
    severity: IssueSeverity
    instrument_id: UUID | None
    field: str | None
    message: str


# Issue codes emitted by this service.  They are stable machine identifiers;
# the human-readable message is display copy and excluded from report_hash.
CODE_INSTRUMENT_NOT_FOUND = "INSTRUMENT_NOT_FOUND"
CODE_IDENTITY_MAPPING_MISSING = "IDENTITY_MAPPING_MISSING"
CODE_IDENTITY_MAPPING_CONFLICT = "IDENTITY_MAPPING_CONFLICT"
CODE_CALENDAR_ID_UNRESOLVED = "CALENDAR_ID_UNRESOLVED"
CODE_RULES_PACKAGE_MISSING = "RULES_PACKAGE_MISSING"
CODE_SETTLEMENT_RULE_MISSING = "SETTLEMENT_RULE_MISSING"
CODE_SETTLEMENT_RULE_UNSUPPORTED = "SETTLEMENT_RULE_UNSUPPORTED"
CODE_SELL_RULE_MISSING = "SELL_RULE_MISSING"
CODE_NO_TRADING_SESSION = "NO_TRADING_SESSION"
CODE_VALUATION_SESSION_AFTER_END_DATE = "VALUATION_SESSION_AFTER_END_DATE"
CODE_VALUATION_PRICE_MISSING = "VALUATION_PRICE_MISSING"
CODE_VALUATION_PRICE_STALE = "VALUATION_PRICE_STALE"
CODE_VALUATION_PRICE_INVALID = "VALUATION_PRICE_INVALID"
CODE_CORPORATE_ACTION_FACTS_MISSING = "CORPORATE_ACTION_FACTS_MISSING"
CODE_TRADING_STATUS_FACTS_MISSING = "TRADING_STATUS_FACTS_MISSING"


def _issue(
    code: str,
    instrument_id: UUID | None,
    field: str | None,
    message: str,
) -> InitialPositionPreflightIssue:
    """Build a blocking issue with uniform severity."""

    return InitialPositionPreflightIssue(
        code=code,
        severity=IssueSeverity.ERROR,
        instrument_id=instrument_id,
        field=field,
        message=message,
    )


@dataclass(frozen=True, slots=True)
class InitialPositionPreflightResult:
    """Per-position preflight outcome with per-category statuses."""

    instrument_id: UUID
    side: PositionSide
    quantity: Decimal
    available_quantity: Decimal
    average_price: Decimal
    valuation_session: date | None
    raw_valuation_price: Decimal | None
    identity_status: CheckStatus
    rules_status: CheckStatus
    settlement_status: CheckStatus
    corporate_action_status: CheckStatus
    trading_status: CheckStatus
    status: PreflightStatus
    issues: tuple[InitialPositionPreflightIssue, ...]


def _sort_issues(
    issues: Sequence[InitialPositionPreflightIssue],
) -> tuple[InitialPositionPreflightIssue, ...]:
    """Sort issues deterministically for stable reports and hashes."""

    return tuple(
        sorted(
            issues,
            key=lambda issue: (
                str(issue.instrument_id) if issue.instrument_id else "",
                issue.code,
                issue.field or "",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class InitialPositionPreflightReport:
    """Hard-gate report consumed by run creation.

    ``report_hash`` is derived from normalized, stably ordered content only;
    generation time and display messages are excluded so equivalent inputs
    always produce identical hashes.
    """

    status: PreflightStatus
    valuation_session: date | None
    checked_positions: tuple[InitialPositionPreflightResult, ...]
    issues: tuple[InitialPositionPreflightIssue, ...]
    report_hash: str

    @property
    def blocked(self) -> bool:
        """Whether run creation must be refused."""

        return self.status is PreflightStatus.BLOCKED

    def canonical_content(self) -> dict[str, object]:
        """Return the hash-relevant content in its canonical shape.

        Messages are deliberately omitted: they are display copy.  Decimals
        use fixed-point formatting so equivalent numeric inputs hash equally
        regardless of whether callers passed ``Decimal``, ``int``, or ``str``.
        """

        def fmt(value: Decimal | None) -> str | None:
            return None if value is None else format(value, "f")

        return {
            "status": self.status.value,
            "valuation_session": (
                self.valuation_session.isoformat() if self.valuation_session else None
            ),
            "checked_positions": [
                {
                    "instrument_id": str(result.instrument_id),
                    "side": result.side.value,
                    "quantity": fmt(result.quantity),
                    "available_quantity": fmt(result.available_quantity),
                    "average_price": fmt(result.average_price),
                    "valuation_session": (
                        result.valuation_session.isoformat()
                        if result.valuation_session
                        else None
                    ),
                    "raw_valuation_price": fmt(result.raw_valuation_price),
                    "identity_status": result.identity_status.value,
                    "rules_status": result.rules_status.value,
                    "settlement_status": result.settlement_status.value,
                    "corporate_action_status": result.corporate_action_status.value,
                    "trading_status": result.trading_status.value,
                    "status": result.status.value,
                    "issues": [
                        {
                            "code": issue.code,
                            "severity": issue.severity.value,
                            "instrument_id": (
                                str(issue.instrument_id) if issue.instrument_id else ""
                            ),
                            "field": issue.field or "",
                        }
                        for issue in result.issues
                    ],
                }
                for result in self.checked_positions
            ],
            "issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity.value,
                    "instrument_id": (
                        str(issue.instrument_id) if issue.instrument_id else ""
                    ),
                    "field": issue.field or "",
                }
                for issue in self.issues
            ],
        }

    def __post_init__(self) -> None:
        # Recompute defensively so a caller cannot forge a mismatched hash.
        object.__setattr__(self, "report_hash", _hash_report(self))


def _hash_report(report: InitialPositionPreflightReport) -> str:
    """Hash canonical JSON of stable report content with SHA-256."""

    payload = json.dumps(
        report.canonical_content(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class InitialPositionPreflightService:
    """Runs the mandatory per-position eligibility checks for one spec."""

    def __init__(self, gateway: BacktestPreflightGateway) -> None:
        self._gateway = gateway

    def run(self, spec: BacktestSpec) -> InitialPositionPreflightReport:
        """Preflight every non-zero initial position of the spec.

        Positions are always checked individually even when the run uses a
        dynamic universe; candidate-set filtering never reduces scope.
        """

        results = tuple(
            self._check_position(position, spec)
            for position in spec.initial_positions  # already stably sorted
        )
        issues = _sort_issues(
            [issue for result in results for issue in result.issues]
        )
        status = (
            PreflightStatus.BLOCKED
            if any(result.status is PreflightStatus.BLOCKED for result in results)
            else PreflightStatus.READY
        )
        # A blocked report must not advertise a usable top-level valuation
        # session: some positions may have failed valuation entirely, so a
        # minimum over the survivors would be misleading.
        sessions = [
            result.valuation_session
            for result in results
            if result.valuation_session is not None
        ]
        valuation_session = min(sessions) if sessions and status is PreflightStatus.READY else None
        return InitialPositionPreflightReport(
            status=status,
            valuation_session=valuation_session,
            checked_positions=results,
            issues=issues,
            # __post_init__ recomputes this field from canonical content.
            report_hash="",
        )

    def _check_position(
        self, position: InitialPositionInput, spec: BacktestSpec
    ) -> InitialPositionPreflightResult:
        """Check one position against every required fact category."""

        instrument_id = position.instrument_id
        as_of = spec.start_date
        issues: list[InitialPositionPreflightIssue] = []

        identity_status = CheckStatus.OK
        rules_status = CheckStatus.OK
        settlement_status = CheckStatus.OK
        corporate_action_status = CheckStatus.OK
        trading_status = CheckStatus.OK

        resolved = self._gateway.resolve_instrument(instrument_id, as_of=as_of)
        if resolved is None:
            identity_status = CheckStatus.BLOCKED
            rules_status = CheckStatus.BLOCKED
            settlement_status = CheckStatus.BLOCKED
            corporate_action_status = CheckStatus.BLOCKED
            trading_status = CheckStatus.BLOCKED
            issues.append(
                _issue(
                    CODE_INSTRUMENT_NOT_FOUND,
                    instrument_id,
                    "instrument_id",
                    f"instrument {instrument_id} does not resolve at {as_of.isoformat()}",
                )
            )

        calendar_id: str | None = None
        mappings: Sequence[IdentityMappingEntry] = []
        if resolved is not None:
            calendar_id = resolved.calendar_id
            if not calendar_id:
                identity_status = CheckStatus.BLOCKED
                issues.append(
                    _issue(
                        CODE_CALENDAR_ID_UNRESOLVED,
                        instrument_id,
                        "calendar_id",
                        f"trading calendar for instrument {instrument_id} does not resolve",
                    )
                )
            mappings = list(
                self._gateway.resolve_identity_mapping(instrument_id, as_of=as_of)
            )
            # Defense in depth: the protocol promises PIT-valid entries, but
            # entries whose validity window does not cover the target date
            # are treated as absent instead of trusted blindly.
            mappings = [
                entry
                for entry in mappings
                if entry.valid_from <= as_of
                and (entry.valid_to is None or entry.valid_to >= as_of)
            ]
            if len(mappings) == 0:
                identity_status = CheckStatus.BLOCKED
                issues.append(
                    _issue(
                        CODE_IDENTITY_MAPPING_MISSING,
                        instrument_id,
                        "identity_mapping",
                        f"no PIT identity mapping exists for {instrument_id} at "
                        f"{as_of.isoformat()}",
                    )
                )
            elif len(mappings) > 1:
                identity_status = CheckStatus.BLOCKED
                issues.append(
                    _issue(
                        CODE_IDENTITY_MAPPING_CONFLICT,
                        instrument_id,
                        "identity_mapping",
                        f"{len(mappings)} conflicting identity mappings exist for "
                        f"{instrument_id} at {as_of.isoformat()}",
                    )
                )

        rules_facts: InstrumentRulesFacts | None = None
        sell_rules: SettlementAndSellRules | None = None
        if resolved is not None:
            rules_facts = self._gateway.resolve_instrument_rules(
                instrument_id, as_of=as_of
            )
            if rules_facts is None:
                rules_status = CheckStatus.BLOCKED
                issues.append(
                    _issue(
                        CODE_RULES_PACKAGE_MISSING,
                        instrument_id,
                        "rule_package",
                        f"instrument rule package for {instrument_id} does not resolve",
                    )
                )

            sell_rules = self._gateway.resolve_settlement_and_sell_rules(
                instrument_id, as_of=as_of
            )
            if sell_rules is None:
                settlement_status = CheckStatus.BLOCKED
                issues.append(
                    _issue(
                        CODE_SETTLEMENT_RULE_MISSING,
                        instrument_id,
                        "settlement_rule",
                        f"settlement rules for {instrument_id} do not resolve",
                    )
                )
                issues.append(
                    _issue(
                        CODE_SELL_RULE_MISSING,
                        instrument_id,
                        "sell_rule",
                        f"sell-availability rules for {instrument_id} do not resolve",
                    )
                )
            else:
                if sell_rules.settlement_rule_id is None:
                    settlement_status = CheckStatus.BLOCKED
                    issues.append(
                        _issue(
                            CODE_SETTLEMENT_RULE_MISSING,
                            instrument_id,
                            "settlement_rule",
                            f"settlement rule for {instrument_id} is missing",
                        )
                    )
                elif not self._settlement_kind_supported(sell_rules):
                    # A present but unknown/unsupported settlement category is
                    # outside the first-phase capability range.
                    settlement_status = CheckStatus.BLOCKED
                    issues.append(
                        _issue(
                            CODE_SETTLEMENT_RULE_UNSUPPORTED,
                            instrument_id,
                            "settlement_rule_kind",
                            f"settlement rule kind "
                            f"{sell_rules.settlement_rule_kind!r} for {instrument_id} "
                            f"is not in the supported range "
                            f"{sorted(kind.value for kind in SUPPORTED_SETTLEMENT_RULE_KINDS)}",
                        )
                    )
                if sell_rules.sell_rule_id is None:
                    settlement_status = CheckStatus.BLOCKED
                    issues.append(
                        _issue(
                            CODE_SELL_RULE_MISSING,
                            instrument_id,
                            "sell_rule",
                            f"sell-availability rule for {instrument_id} is missing",
                        )
                    )

            corporate_outcome = self._gateway.check_required_corporate_actions(
                instrument_id, start_date=spec.start_date, end_date=spec.end_date
            )
            if not corporate_outcome.complete:
                corporate_action_status = CheckStatus.BLOCKED
                issues.append(
                    _issue(
                        CODE_CORPORATE_ACTION_FACTS_MISSING,
                        instrument_id,
                        "corporate_actions",
                        "corporate-action facts required by run accounting are "
                        f"incomplete{self._detail_suffix(corporate_outcome)}",
                    )
                )

        valuation_session: date | None = None
        raw_valuation_price: Decimal | None = None
        if calendar_id:
            session = self._gateway.find_first_trading_session_on_or_after(
                calendar_id, spec.start_date
            )
            if session is None:
                issues.append(
                    _issue(
                        CODE_NO_TRADING_SESSION,
                        instrument_id,
                        "valuation_session",
                        f"calendar {calendar_id} has no trading session on or after "
                        f"{spec.start_date.isoformat()}",
                    )
                )
            else:
                valuation_session = session
                if session > spec.end_date:
                    # The inclusive backtest window ends at end_date; an
                    # initial valuation after it would book opening positions
                    # outside the run's own time range.
                    issues.append(
                        _issue(
                            CODE_VALUATION_SESSION_AFTER_END_DATE,
                            instrument_id,
                            "valuation_session",
                            f"first trading session {session.isoformat()} for "
                            f"{instrument_id} is after the run end date "
                            f"{spec.end_date.isoformat()}",
                        )
                    )
                    valuation_session = None
                else:
                    price, price_issue = self._resolve_valuation_price(
                        instrument_id, calendar_id, session
                    )
                    if price_issue is not None:
                        issues.append(price_issue)
                    else:
                        assert price is not None
                        raw_valuation_price = price

        if resolved is not None:
            if rules_facts is None:
                trading_status = CheckStatus.BLOCKED
            elif not rules_facts.requires_trading_status_facts:
                # The rule package declares trading-status facts out of scope;
                # record this explicitly instead of omitting the category.
                trading_status = CheckStatus.NOT_APPLICABLE
            else:
                trading_outcome = self._gateway.check_required_trading_status(
                    instrument_id,
                    start_date=spec.start_date,
                    end_date=spec.end_date,
                )
                if not trading_outcome.complete:
                    trading_status = CheckStatus.BLOCKED
                    issues.append(
                        _issue(
                            CODE_TRADING_STATUS_FACTS_MISSING,
                            instrument_id,
                            "trading_status",
                            "trading-status facts declared applicable by the rule "
                            f"package are missing{self._detail_suffix(trading_outcome)}",
                        )
                    )

        position_issues = _sort_issues(issues)
        # Spec normalization proves non-zero inputs carry an explicit cost.
        assert position.average_price is not None
        return InitialPositionPreflightResult(
            instrument_id=instrument_id,
            side=position.side,
            quantity=position.quantity,
            available_quantity=position.available_quantity,
            average_price=position.average_price,
            valuation_session=valuation_session,
            raw_valuation_price=raw_valuation_price,
            identity_status=identity_status,
            rules_status=rules_status,
            settlement_status=settlement_status,
            corporate_action_status=corporate_action_status,
            trading_status=trading_status,
            status=(
                PreflightStatus.BLOCKED if position_issues else PreflightStatus.READY
            ),
            issues=position_issues,
        )

    def _resolve_valuation_price(
        self, instrument_id: UUID, calendar_id: str, session: date
    ) -> tuple[Decimal | None, InitialPositionPreflightIssue | None]:
        """Read and validate the raw valuation price for one session.

        Returns ``(price, None)`` on success or ``(None, issue)`` on any
        failure.  Gateway exceptions (misbehaving adapters returning floats,
        garbage strings, or raising) are converted into a blocking issue so
        an invalid fact can never crash the preflight run.
        """

        try:
            raw = self._gateway.get_raw_valuation_price(instrument_id, session)
            if raw is not None:
                # Force normalization even if an adapter built the object
                # through a path that skipped dataclass validation.
                normalized = RawValuationPrice(
                    session=raw.session, price=raw.price
                )
            else:
                normalized = None
        except (
            DomainValidationError,
            TypeError,
            ValueError,
            ArithmeticError,
            AttributeError,
        ) as exc:
            return None, _issue(
                CODE_VALUATION_PRICE_INVALID,
                instrument_id,
                "raw_valuation_price",
                f"unusable raw valuation price for {instrument_id} on "
                f"{session.isoformat()} from calendar {calendar_id}: {exc}",
            )

        if normalized is None:
            return None, _issue(
                CODE_VALUATION_PRICE_MISSING,
                instrument_id,
                "raw_valuation_price",
                f"no raw valuation price for {instrument_id} on "
                f"{session.isoformat()}; fallback prices are forbidden",
            )
        if normalized.session != session:
            # Any price bound to another session is stale data, which
            # includes silent previous-value substitution.
            return None, _issue(
                CODE_VALUATION_PRICE_STALE,
                instrument_id,
                "raw_valuation_price",
                f"raw price for {instrument_id} belongs to session "
                f"{normalized.session.isoformat()}, not requested session "
                f"{session.isoformat()}",
            )
        if normalized.price <= ZERO:
            return None, _issue(
                CODE_VALUATION_PRICE_INVALID,
                instrument_id,
                "raw_valuation_price",
                f"raw valuation price for {instrument_id} on "
                f"{session.isoformat()} is not a positive finite number",
            )
        return normalized.price, None

    @staticmethod
    def _settlement_kind_supported(rules: SettlementAndSellRules) -> bool:
        """Whether the resolved settlement category is first-phase allowed."""

        try:
            kind = SettlementRuleKind(str(rules.settlement_rule_kind))
        except ValueError:
            return False
        return kind in SUPPORTED_SETTLEMENT_RULE_KINDS

    @staticmethod
    def _detail_suffix(outcome: FactCheckOutcome) -> str:
        """Append optional gateway detail to a message for diagnosis."""

        return f" ({outcome.detail})" if outcome.detail else ""
