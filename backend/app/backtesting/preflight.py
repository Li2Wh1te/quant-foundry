"""Pre-creation eligibility preflight for non-zero initial positions.

The service answers one question before a ``backtest_run`` is created: is
every fact needed to account for each opening position available, valid, and
PIT-consistent?  It depends only on the :class:`BacktestPreflightGateway`
protocol; no ORM, FastAPI, Tushare, or database type appears here.  Callers
must treat a ``blocked`` report as a hard gate and must not create a run.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

from app.backtesting.domain import (
    ZERO,
    DomainValidationError,
    PositionSide,
)
from app.backtesting.spec import BacktestSpec, InitialPositionInput
from app.backtesting.calendar_axis import (
    CalendarAxisResolution,
    CalendarAxisStatus,
    CalendarSnapshot,
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
)
from app.backtesting.data.errors import (
    DataPreflightBlockedError,
    UniverseCapabilityMissingError,
    UniverseProviderContractViolationError,
    UniverseScopeUnresolvedError,
)
from app.backtesting.data.requests import (
    DataCapability,
    DataPreflightRequest,
    InstrumentScopeMode,
    fixed_instrument_ids,
)
from app.backtesting.data.universe import (
    CandidateEligibility,
    CandidateEligibilityContext,
    CandidateFilterResult,
    CandidateInput,
    UniversePreflightReport,
    UniverseScopeResolution,
    UniverseScopeStatus,
    scope_issue,
    merge_calendar_ids,
)
from app.instruments.domain import VersionedReference
from app.instruments.rules.contracts import (
    CAPABILITY_DIMENSIONS,
    TradingStatusRequirement,
)


class PreflightStatus(StrEnum):
    """Overall or per-position preflight outcome."""

    READY = "ready"
    BLOCKED = "blocked"


class CheckStatus(StrEnum):
    """Outcome of one preflight category for one instrument."""

    OK = "ok"
    NOT_APPLICABLE = "not_applicable"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CorporateActionEligibility:
    """Small, mode-neutral result shared by fixed/dynamic/selected gates."""

    status: str  # eligible, filter, blocked
    code: str | None = None
    message: str = ""


def evaluate_corporate_action_eligibility(
    *, coverage_status: str | None,
    action_type: str | None = None,
    profile: str = "formal",
    fixture_start: date | None = None,
    fixture_end: date | None = None,
    requested_start: date | None = None,
    requested_end: date | None = None,
    entitlement_frozen: bool = True,
) -> CorporateActionEligibility:
    """Evaluate common company-action gates without changing orchestration.

    Quantity actions are not production-supported: formal runs block while
    internal runs may proceed only when an explicitly bounded fixture covers
    the entire requested range.  Incomplete coverage filters dynamic
    candidates but blocks fixed/selected callers via ``profile`` marker.
    """
    quantity = action_type in {"split", "consolidation", "share_change"}
    if quantity:
        if profile != "internal_link_acceptance":
            return CorporateActionEligibility("blocked", "corporate_action_quantity_coverage_unavailable", "正式运行缺少数量类公司行动覆盖")
        if not (fixture_start and fixture_end and requested_start and requested_end and fixture_start <= requested_start and fixture_end >= requested_end):
            return CorporateActionEligibility("blocked", "corporate_action_fixture_out_of_scope", "数量类 fixture 未覆盖请求区间")
    if not entitlement_frozen:
        return CorporateActionEligibility("blocked", "corporate_action_entitlement_unfrozen", "公司行动权益尚未冻结")
    if coverage_status in (None, "unavailable"):
        return CorporateActionEligibility("blocked", "corporate_action_coverage_unavailable", "公司行动覆盖不可用")
    if coverage_status in ("partial", "invalid", "incomplete"):
        return CorporateActionEligibility("filter", "corporate_action_coverage_incomplete", "公司行动覆盖不完整")
    if coverage_status != "complete":
        return CorporateActionEligibility("blocked", "corporate_action_provider_contract_violation", "公司行动覆盖状态非法")
    return CorporateActionEligibility("eligible")


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
    """Resolved rule evidence with an explicit trading-status declaration.

    The old DTO carried a caller-supplied ``requires_trading_status_facts``
    boolean.  That value could not prove that every applicability dimension
    had actually been resolved, so a malformed or partial rule packet could
    accidentally be treated as ``not_applicable``.  The gateway now carries
    the rule-package reference and the complete declaration instead; the
    boolean below is only a derived convenience property and is never an
    input to the gate.

    Values are intentionally kept permissive at construction.  A gateway
    returning a malformed packet must produce a structured blocked result,
    not an exception that encourages callers to guess a fallback value.
    """

    rule_package_reference: VersionedReference | str | None = None
    trading_status_applicability: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        # Freeze a mapping when possible, while leaving malformed input for
        # the service-level validator to report as a blocking issue.
        if isinstance(self.trading_status_applicability, Mapping):
            object.__setattr__(
                self,
                "trading_status_applicability",
                MappingProxyType(dict(self.trading_status_applicability)),
            )

    @property
    def rule_package_id(self) -> str:
        """Return a display-compatible package reference for old consumers."""

        reference = self.rule_package_reference
        if isinstance(reference, VersionedReference):
            return f"{reference.key}@{reference.version}"
        return str(reference) if reference is not None else ""

    @property
    def requires_trading_status_facts(self) -> bool:
        """Derive the requirement from a declaration, never from a flag."""

        declaration = self.trading_status_applicability
        return isinstance(declaration, Mapping) and any(
            value == TradingStatusRequirement.REQUIRED.value
            for value in declaration.values()
        )

    def declaration_issues(self) -> tuple[tuple[str, str, str], ...]:
        """Return stable validation issues for this rule evidence packet.

        The tuple items are ``(code, field, detail)``.  Keeping validation
        here makes every caller use the same complete-dimension contract and
        prevents malformed declarations from being silently interpreted as
        all ``not_applicable``.
        """

        issues: list[tuple[str, str, str]] = []
        reference = self.rule_package_reference
        if reference is None or (
            isinstance(reference, str) and not reference.strip()
        ):
            issues.append(
                (
                    CODE_RULE_PACKAGE_REFERENCE_MISSING,
                    "rule_package_reference",
                    "规则事实缺少规则包引用，禁止按默认规则继续",
                )
            )
        elif isinstance(reference, str):
            # Compatibility callers may still expose ``key@version`` text,
            # but arbitrary text is not a rule-package reference.
            key, separator, raw_version = reference.strip().rpartition("@")
            try:
                valid_text_reference = bool(key.strip()) and bool(separator) and int(raw_version) > 0
            except (TypeError, ValueError):
                valid_text_reference = False
            if not valid_text_reference:
                issues.append(
                    (
                        CODE_RULE_PACKAGE_REFERENCE_INVALID,
                        "rule_package_reference",
                        "规则事实的规则包引用格式非法，必须为 key@version",
                    )
                )
        elif not isinstance(reference, VersionedReference):
            issues.append(
                (
                    CODE_RULE_PACKAGE_REFERENCE_INVALID,
                    "rule_package_reference",
                    "规则事实的规则包引用格式非法，禁止按默认规则继续",
                )
            )

        declaration = self.trading_status_applicability
        if not isinstance(declaration, Mapping):
            issues.append(
                (
                    CODE_TRADING_STATUS_DECLARATION_MISSING,
                    "trading_status_applicability",
                    "规则事实缺少完整交易状态适用性声明，禁止补为 N/A",
                )
            )
            return tuple(issues)

        expected = set(CAPABILITY_DIMENSIONS)
        actual = set(declaration)
        missing = sorted(expected - actual)
        unknown = sorted(str(item) for item in actual - expected)
        if missing:
            issues.append(
                (
                    CODE_TRADING_STATUS_DECLARATION_MISSING,
                    "trading_status_applicability",
                    "交易状态适用性缺少显式维度：" + ", ".join(missing),
                )
            )
        if unknown:
            issues.append(
                (
                    CODE_TRADING_STATUS_DECLARATION_INVALID,
                    "trading_status_applicability",
                    "交易状态适用性包含未知维度：" + ", ".join(unknown),
                )
            )
        for dimension in sorted(expected & actual):
            value = declaration.get(dimension)
            if value not in {
                TradingStatusRequirement.REQUIRED.value,
                TradingStatusRequirement.NOT_APPLICABLE.value,
            }:
                issues.append(
                    (
                        CODE_TRADING_STATUS_DECLARATION_INVALID,
                        f"trading_status_applicability.{dimension}",
                        f"交易状态适用性维度 {dimension} 的取值非法，必须为 required 或 not_applicable",
                    )
                )
        return tuple(issues)


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
CODE_CASH_DIVIDEND_ENTITLEMENT_OUTSIDE_RUN = "cash_dividend_entitlement_outside_run"
CODE_CASH_DIVIDEND_RECEIVABLE_BEYOND_RUN = "cash_dividend_receivable_beyond_run"
CODE_TRADING_STATUS_FACTS_MISSING = "TRADING_STATUS_FACTS_MISSING"
# Stable reasons for malformed initial-position rule evidence.  These are
# deliberately separate from missing trading-status facts: a malformed
# applicability packet is a rule admission failure and must never be
# downgraded to the N/A path.
CODE_RULE_PACKAGE_REFERENCE_MISSING = "rule_package_reference_missing"
CODE_RULE_PACKAGE_REFERENCE_INVALID = "rule_package_reference_invalid"
CODE_TRADING_STATUS_DECLARATION_MISSING = "trading_status_declaration_missing"
CODE_TRADING_STATUS_DECLARATION_INVALID = "trading_status_declaration_invalid"


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
        rules_declaration_issues: tuple[tuple[str, str, str], ...] = ()
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
            elif not isinstance(rules_facts, InstrumentRulesFacts):
                rules_status = CheckStatus.BLOCKED
                rules_declaration_issues = (
                    (
                        CODE_TRADING_STATUS_DECLARATION_INVALID,
                        "rule_package_reference",
                        "规则事实返回类型非法，无法验证规则包和交易状态适用性声明",
                    ),
                )
                issues.append(
                    _issue(
                        CODE_TRADING_STATUS_DECLARATION_INVALID,
                        instrument_id,
                        "rule_package_reference",
                        "规则事实返回类型非法，无法验证规则包和交易状态适用性声明",
                    )
                )
            else:
                # Applicability is part of the rule fact, not a gateway
                # policy switch.  Validate the complete three-dimension
                # declaration before any status requirement is derived.
                rules_declaration_issues = rules_facts.declaration_issues()
                for code, field, message in rules_declaration_issues:
                    rules_status = CheckStatus.BLOCKED
                    issues.append(_issue(code, instrument_id, field, message))

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
            # Providers may expose lifecycle-specific blockers discovered while
            # evaluating the bounded window. Preserve their stable codes rather
            # than collapsing them into a generic coverage failure.
            for code, label in ((CODE_CASH_DIVIDEND_ENTITLEMENT_OUTSIDE_RUN, "起点前登记日权益未冻结"), (CODE_CASH_DIVIDEND_RECEIVABLE_BEYOND_RUN, "终点后应收分红")):
                details = getattr(corporate_outcome, "details", {}) or {}
                flagged = bool(getattr(corporate_outcome, code, False)) or bool(isinstance(details, Mapping) and details.get(code))
                if flagged:
                    corporate_action_status = CheckStatus.BLOCKED
                    issues.append(_issue(code, instrument_id, "corporate_actions", label))

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
            elif rules_declaration_issues:
                # Invalid or partial declarations must not become a
                # permissive N/A result.  The rules category already carries
                # the detailed issue above; this category records that the
                # status decision is blocked by the same malformed evidence.
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


# ---------------------------------------------------------------------------
# PIT candidate-universe scope preflight (task 15)
# ---------------------------------------------------------------------------


def _scope_issue_from_mapping(value: Mapping[str, object]) -> object:
    """Convert a provider mapping into the immutable scope issue contract."""

    code = value.get("code")
    message = value.get("message", "动态候选范围预检未通过。")
    field = value.get("field")
    details = value.get("details", {})
    if not isinstance(code, str) or not code.strip():
        code = "universe_scope_unresolved"
    if not isinstance(message, str) or not message.strip():
        message = "动态候选范围预检未通过。"
    if not isinstance(field, str) or not field.strip():
        field = None
    if not isinstance(details, Mapping):
        details = {}
    return scope_issue(code, message, field=field, details=details)


def _provider_scope_result(
    provider: object,
    request: DataPreflightRequest,
    *,
    profile: object | None = None,
) -> object:
    """Invoke only the canonical preflight scope capability.

    Aliased methods such as ``resolve_scope`` are intentionally not accepted:
    they frequently mean a candidate enumeration API and cannot prove the
    finite PIT calendar set required before strategy execution.
    """

    method = getattr(provider, "resolve_dynamic_universe_scope", None)
    if callable(method):
        # Inspect the signature before invoking.  Catching a provider's own
        # ``TypeError`` and calling it a second time could duplicate an
        # authoritative read or hide a real contract bug.
        try:
            method_signature = inspect.signature(method)
            parameters = method_signature.parameters
        except (TypeError, ValueError):
            method_signature = None
            parameters = {"request": object()}
        if not parameters:
            return method()
        values = {"request": request}
        if profile is not None:
            values.update({"profile": profile, "preflight_profile": profile})
        if any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return method(**values)
        if "request" not in parameters:
            positional = tuple(
                parameter
                for parameter in parameters.values()
                if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and parameter.default is inspect.Parameter.empty
            )
            if len(positional) == 1:
                keyword_values = {
                    key: value
                    for key, value in values.items()
                    if key in parameters and key != positional[0].name
                }
                return method(request, **keyword_values)
        call_kwargs = {
            key: value
            for key, value in values.items()
            if key in parameters
            and parameters[key].kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }
        try:
            if method_signature is not None:
                method_signature.bind(**call_kwargs)
        except (TypeError, ValueError) as exc:
            raise UniverseProviderContractViolationError(
                "provider scope method does not expose the canonical request contract",
                details={"error_type": type(exc).__name__},
            ) from exc
        return method(**call_kwargs)
    raise UniverseCapabilityMissingError(
        "provider does not expose a dynamic-universe scope capability",
        details={"required_method": "resolve_dynamic_universe_scope"},
    )


def _mapping_to_scope_resolution(
    value: Mapping[str, object],
    request: DataPreflightRequest,
) -> UniverseScopeResolution:
    """Normalize a provider mapping without trusting caller ordering."""

    status = value.get("status", UniverseScopeStatus.READY)
    raw_issues = value.get("issues", ())
    issues: list[object] = []
    for item in raw_issues if isinstance(raw_issues, (tuple, list)) else ():
        if isinstance(item, Mapping):
            issues.append(_scope_issue_from_mapping(item))
        elif hasattr(item, "code") and hasattr(item, "message"):
            issues.append(item)
    raw_calendars = value.get(
        "resolved_calendar_ids",
        value.get("calendar_ids", value.get("frozen_calendar_ids", ())),
    )
    if isinstance(raw_calendars, str):
        raw_calendars = (raw_calendars,)
    if not isinstance(raw_calendars, Iterable):
        raw_calendars = ()
    capability_summary = value.get(
        "capability_summary",
        value.get("provider_capability_summary", value.get("capabilities", {})),
    )
    source_evidence = value.get("source_evidence", value.get("evidence", {}))
    if not isinstance(capability_summary, Mapping):
        capability_summary = {"status": str(capability_summary)}
    if not isinstance(source_evidence, Mapping):
        source_evidence = {}
    return UniverseScopeResolution(
        status=status,
        market_scope=value.get("market_scope", request.market_scope),
        universe_query_policy=value.get(
            "universe_query_policy", request.universe_query_policy
        ),
        rule_package_reference=value.get(
            "rule_package_reference", request.rule_package
        ),
        rule_exception_set_reference=value.get(
            "rule_exception_set_reference", request.rule_exception_set
        ),
        qualification_policy_version=value.get(
            "qualification_policy_version",
            value.get("qualification_policy", request.qualification_policy_version),
        ),
        resolved_calendar_ids=tuple(raw_calendars),
        capability_summary=capability_summary,
        source_evidence=source_evidence,
        issues=tuple(issues),
        calendar_session_signature=value.get(
            "calendar_session_signature", value.get("session_signature")
        ),
        calendar_axis_resolution=value.get("calendar_axis_resolution"),
        scope_mode=value.get("scope_mode", request.instrument_scope_mode),
        data_cutoff=value.get("data_cutoff", request.query_boundary.data_cutoff),
    )


def _axis_evidence(
    axis: object,
    *,
    expected_start: date | None = None,
    expected_end: date | None = None,
) -> tuple[
    UniverseScopeStatus,
    tuple[str, ...],
    str | None,
    Mapping[str, object],
    tuple[object, ...],
]:
    """Project one *real* task-11 strict-axis result into scope evidence.

    A session signature is not a proof by itself.  The result must be the
    immutable ``CalendarAxisResolution`` emitted by the task-11 resolver,
    use ``strict_compatible@1``, and contain no compatibility differences.
    Accepting a provider-owned mapping here would allow an arbitrary string
    (or a ``status=ready`` placeholder) to bypass the calendar gate.
    """

    if isinstance(axis, CalendarSnapshot):
        axis = axis.resolution
    if not isinstance(axis, CalendarAxisResolution):
        raise UniverseScopeUnresolvedError(
            "calendar resolver must return a task-11 CalendarAxisResolution",
            details={"returned_type": type(axis).__name__},
        )
    status = axis.status
    status_text = getattr(status, "value", status)
    ids = tuple(axis.calendar_ids)
    signature = axis.session_signature or None
    differences = tuple(axis.differences)
    evidence = {
        "policy_key": axis.policy_key,
        "policy_version": axis.policy_version,
        "start_date": axis.start_date,
        "end_date": axis.end_date,
        "status": str(status_text),
        "calendar_ids": ids,
        "differences": tuple(
            difference.evidence()
            if callable(getattr(difference, "evidence", None))
            else {"value": type(difference).__name__}
            for difference in differences
        ),
    }
    if (
        axis.policy_key != POLICY_KEY_STRICT_COMPATIBLE
        or str(axis.policy_version) != POLICY_VERSION_STRICT_COMPATIBLE
        or (expected_start is not None and axis.start_date != expected_start)
        or (expected_end is not None and axis.end_date != expected_end)
        or status is not CalendarAxisStatus.COMPATIBLE
        or not signature
        or differences
    ):
        return UniverseScopeStatus.BLOCKED, ids, signature, evidence, differences
    return UniverseScopeStatus.READY, ids, signature, evidence, differences


_SCOPE_CAPABILITY_ALIASES: Mapping[str, frozenset[str]] = {
    "universe": frozenset({"universe", "universe_query", "pit_universe", "candidate_universe", "pit_universe_query"}),
    "identity": frozenset({"identity", "pit_identity", "instrument_identity", "identity_resolution", "instrument_spec", "instrument_spec_qualification"}),
    "mapping": frozenset({"mapping", "mappings", "pit_mapping", "display_mapping", "mapping_resolution", "instrument_mapping"}),
    "rules": frozenset({"rule", "rules", "rule_package", "rule_snapshot", "rule_qualification", "instrument_rule_qualification", "qualification", "candidate_qualification"}),
    "market_data": frozenset({"bar", "bars", "market_data", "raw_market_data", "raw_bars", "raw_bar_coverage", "raw_bar_qualification", "bar_coverage", "coverage", "coverage_qualification", "history", "market_data_coverage"}),
    "corporate_actions": frozenset({"action", "actions", "corporate_action", "corporate_actions", "corporate_action_coverage", "corporate_action_qualification", "quantity_action_coverage", "quantity_actions"}),
    "status": frozenset({"status", "trading_status", "trading_status_facts", "trading_status_qualification", "tradability_status"}),
}
_REQUIRED_DYNAMIC_SCOPE_CAPABILITIES = (
    "universe",
    "identity",
    "mapping",
    "rules",
    "market_data",
)
_FORMAL_GATE_ALIASES: Mapping[str, frozenset[str]] = {
    # G15-5B is the formal preflight/admission convergence point.  The other
    # two dimensions are kept separate so a report can explain exactly which
    # formal dependency is absent instead of silently treating fixture data as
    # production evidence.
    "formal_preflight": frozenset(
        {"formal_preflight", "preflight_16b", "formal_admission", "formal_qualification"}
    ),
    "formal_runtime": frozenset(
        {"formal_runtime", "runtime_boundary", "runner", "strategy_runtime"}
    ),
    "formal_corporate_actions": frozenset(
        {"formal_corporate_actions", "corporate_action_qualification", "actions_18", "task18"}
    ),
    "formal_trading_status": frozenset(
        {"formal_trading_status", "trading_status_qualification", "status_19", "task19"}
    ),
}


def _scope_capability_bucket(key: object) -> str | None:
    """Map provider vocabulary to one of the required scope dimensions."""

    if not isinstance(key, str):
        return None
    normalized = key.strip().lower().replace("-", "_").replace(".", "_")
    for bucket, aliases in _SCOPE_CAPABILITY_ALIASES.items():
        if normalized in aliases:
            return bucket
    return None


def _formal_gate_bucket(key: object) -> str | None:
    """Map explicit formal convergence declarations to a gate name."""

    if not isinstance(key, str):
        return None
    normalized = key.strip().lower().replace("-", "_").replace(".", "_")
    for bucket, aliases in _FORMAL_GATE_ALIASES.items():
        if normalized in aliases:
            return bucket
    return None


def _scope_capability_state(value: object) -> tuple[str, bool]:
    """Return ``(state, explicitly_fixture_backed)`` for one declaration.

    ``state`` is one of ``available``, ``missing`` or ``unknown``.  An absent
    status is never promoted to available: a scope provider must prove each
    required qualification dimension explicitly.
    """

    source = None
    status: object = value
    if isinstance(value, Mapping):
        source = value.get("source")
        status = value.get(
            "status",
            value.get(
                "availability",
                value.get("supported", value.get("complete")),
            ),
        )
        # A named source without a status is still an explicit declaration,
        # but it may only be consumed by the opt-in internal profile.
        if status is None and source is not None:
            status = source
    if hasattr(status, "value"):
        status = status.value
    normalized = status.strip().lower() if isinstance(status, str) else status
    fixture_backed = isinstance(source, str) and source.strip().lower() in {
        "fixture",
        "internal_fixture",
        "transitional",
    }
    if normalized is True or normalized in {
        "available",
        "supported",
        "complete",
        "ok",
        "ready",
        "production",
        "fixture",
        "internal_fixture",
        "transitional",
        "not_applicable",
        "explicit_single_instrument_port",
        "local_spec_and_bar_evidence",
        "local",
    }:
        return "available", fixture_backed or normalized in {"fixture", "internal_fixture", "transitional"}
    if normalized is False or normalized in {
        "missing",
        "unavailable",
        "unsupported",
        "blocked",
        "false",
        "incomplete",
        "invalid",
    }:
        return "missing", fixture_backed
    return "unknown", fixture_backed


def _profile_is_internal(profile: object | None) -> bool:
    """Return whether capability substitutes are explicitly opt-in."""

    if profile is None:
        return False
    key = getattr(profile, "key", None)
    version = getattr(profile, "version", None)
    if isinstance(profile, str) and "@" in profile:
        key, raw_version = profile.rsplit("@", 1)
        key = key.strip()
        try:
            version = int(raw_version)
        except ValueError:
            return False
    return key == "internal_link_acceptance" and version == 1


def _profile_text(profile: object | None) -> str:
    """Normalize a profile reference for non-sensitive scope evidence."""

    if isinstance(profile, str):
        return profile.strip()
    key = getattr(profile, "key", None)
    version = getattr(profile, "version", None)
    if isinstance(key, str) and isinstance(version, int):
        return f"{key}@{version}"
    return "formal@1"


def _capability_status_issues(
    provider: object,
    request: DataPreflightRequest,
    summary: Mapping[str, object],
    *,
    profile: object | None = None,
) -> tuple[object, ...]:
    """Require an explicit, complete capability proof for dynamic scope.

    The previous implementation only inspected keys that happened to be
    present.  That made an empty capability mapping equivalent to a complete
    provider and allowed formal runs to proceed without the 16A/18/19
    contracts.  Every scope dimension is now required; only a caller that
    explicitly selects ``internal_link_acceptance@1`` may use fixture-backed
    declarations.
    """

    if not isinstance(summary, Mapping):
        summary = {}
    internal = _profile_is_internal(profile)
    required_buckets = list(_REQUIRED_DYNAMIC_SCOPE_CAPABILITIES)
    if DataCapability.ACTIONS in request.required_capabilities:
        required_buckets.append("corporate_actions")
    if DataCapability.STATUS in request.required_capabilities:
        required_buckets.append("status")
    # Preserve declaration order while avoiding duplicate checks when future
    # request capability aliases overlap one of the core dimensions.
    required_buckets = list(dict.fromkeys(required_buckets))
    by_bucket: dict[str, tuple[str, object, bool]] = {}
    # Preserve the first declaration for a bucket.  Two aliases declaring
    # different outcomes are ambiguity, not a reason to choose one by order.
    conflicts: set[str] = set()
    for key, value in summary.items():
        bucket = _scope_capability_bucket(key)
        state, fixture_backed = _scope_capability_state(value)
        if bucket is None:
            # Unknown keys remain useful evidence but cannot satisfy a known
            # required dimension.
            continue
        current = by_bucket.get(bucket)
        entry = (state, key, fixture_backed)
        if current is not None and (current[0], current[2]) != (
            entry[0],
            entry[2],
        ):
            conflicts.add(bucket)
        else:
            by_bucket[bucket] = entry

    issues: list[object] = []
    missing = [bucket for bucket in required_buckets if bucket not in by_bucket]
    if missing:
        issues.append(
            scope_issue(
                "universe_capability_missing",
                "Provider 未完整声明动态候选资格能力，无法冻结范围。",
                field="capability_summary",
                details={"missing_capabilities": missing},
            )
        )
    for bucket in required_buckets:
        entry = by_bucket.get(bucket)
        if entry is None:
            continue
        state, source_key, fixture_backed = entry
        if bucket in conflicts:
            issues.append(
                scope_issue(
                    "universe_scope_unresolved",
                    "Provider 的动态候选资格能力声明存在歧义。",
                    field=f"capability_summary.{source_key}",
                    details={"capability": bucket},
                )
            )
        elif state == "unknown":
            issues.append(
                scope_issue(
                    "universe_scope_unresolved",
                    "Provider 的动态候选资格能力状态未知，无法冻结范围。",
                    field=f"capability_summary.{source_key}",
                    details={"capability": bucket},
                )
            )
        elif state == "missing":
            issues.append(
                scope_issue(
                    "universe_capability_missing",
                    "Provider 缺少动态候选范围所需的资格能力。",
                    field=f"capability_summary.{source_key}",
                    details={"capability": bucket},
                )
            )
        elif fixture_backed and not internal:
            issues.append(
                scope_issue(
                    "universe_capability_missing",
                    "formal 路径不允许使用未注册的内部替代资格事实。",
                    field=f"capability_summary.{source_key}",
                    details={"capability": bucket, "source": "fixture"},
                )
            )

    if not internal:
        formal_gates: dict[str, tuple[str, object, bool]] = {}
        for key, value in summary.items():
            bucket = _formal_gate_bucket(key)
            if bucket is None:
                continue
            state, fixture_backed = _scope_capability_state(value)
            current = formal_gates.get(bucket)
            entry = (state, key, fixture_backed)
            if current is None:
                formal_gates[bucket] = entry
            elif current != entry:
                formal_gates[bucket] = ("unknown", key, fixture_backed)
        for bucket in _FORMAL_GATE_ALIASES:
            entry = formal_gates.get(bucket)
            if entry is None:
                issues.append(
                    scope_issue(
                        "universe_capability_missing",
                        "formal 路径依赖的正式预检/运行能力尚未交付。",
                        field="capability_summary",
                        details={"missing_formal_gate": bucket},
                    )
                )
            elif entry[0] != "available" or entry[2]:
                issues.append(
                    scope_issue(
                        "universe_scope_unresolved" if entry[0] == "unknown" else "universe_capability_missing",
                        "formal 路径依赖的正式能力不可用，无法放行。",
                        field=f"capability_summary.{entry[1]}",
                        details={"formal_gate": bucket},
                    )
                )

    manifest_method = getattr(provider, "capability_manifest", None)
    if callable(manifest_method):
        try:
            manifest = manifest_method()
            declared = {
                item.value if hasattr(item, "value") else str(item)
                for item in getattr(manifest, "capabilities", ())
            }
            required = {
                item.value
                if hasattr(item, "value")
                else str(item)
                for item in {DataCapability.UNIVERSE, *request.required_capabilities}
            }
            missing_manifest = sorted(required - declared)
            if missing_manifest:
                issues.append(
                    scope_issue(
                        "universe_capability_missing",
                        "Provider 的能力声明未覆盖动态候选查询所需能力。",
                        field="capability_manifest.capabilities",
                        details={"missing_capabilities": missing_manifest},
                    )
                )
            sources = getattr(manifest, "capability_sources", {})
            if isinstance(sources, Mapping) and not internal:
                fixture_declared = sorted(
                    capability.value if hasattr(capability, "value") else str(capability)
                    for capability, source in sources.items()
                    if str(getattr(source, "value", source)).lower()
                    in {"fixture", "transitional", "internal_fixture"}
                    and (capability in {DataCapability.UNIVERSE, *request.required_capabilities})
                )
                if fixture_declared:
                    issues.append(
                        scope_issue(
                            "universe_capability_missing",
                            "formal 路径不允许使用 fixture/transitional 动态资格能力。",
                            field="capability_manifest.capability_sources",
                            details={"fixture_capabilities": fixture_declared},
                        )
                    )
        except Exception as exc:
            issues.append(
                scope_issue(
                    "universe_capability_missing",
                    "Provider 能力声明无法验证。",
                    field="capability_manifest",
                    details={"error_type": type(exc).__name__},
                )
            )
    return tuple(issues)


def _resolve_calendar_axis(
    resolver: object,
    request: DataPreflightRequest,
    calendar_ids: tuple[str, ...],
) -> object:
    """Call task-11's strict compatibility resolver with explicit PIT inputs.

    The only accepted custom method is named ``resolve_calendar_axis`` (or its
    explicit strict spelling).  Generic ``resolve``/``snapshot`` methods are
    deliberately rejected because their return value may be a provider
    signature rather than the task-11 axis proof.
    """

    kwargs = {
        "policy_key": POLICY_KEY_STRICT_COMPATIBLE,
        "policy_version": POLICY_VERSION_STRICT_COMPATIBLE,
        "calendar_ids": calendar_ids,
        "start_date": request.requested_window.start_date,
        "end_date": request.requested_window.end_date,
        "query_boundary": request.query_boundary,
    }
    for name in ("resolve_calendar_axis", "resolve_strict_compatible_axis"):
        method = getattr(resolver, name, None)
        if not callable(method):
            continue
        try:
            signature = inspect.signature(method)
            parameters = signature.parameters
            call_kwargs = (
                kwargs
                if any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                else {
                    key: value
                    for key, value in kwargs.items()
                    if key in parameters
                    and parameters[key].kind
                    in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    )
                }
            )
            signature.bind(**call_kwargs)
        except (TypeError, ValueError) as exc:
            raise UniverseScopeUnresolvedError(
                "calendar resolver does not expose the task-11 strict contract",
                details={"resolver": name, "error_type": type(exc).__name__},
            ) from exc
        return method(**call_kwargs)

    # The canonical task-11 in-memory/SQL providers expose only ``definitions``
    # and ``fact``.  Delegate them to the one shared resolver rather than
    # implementing any calendar algorithm in task 15.
    if callable(getattr(resolver, "definitions", None)) and callable(
        getattr(resolver, "fact", None)
    ):
        from app.backtesting.calendar_axis import (
            resolve_calendar_axis,
        )

        return resolve_calendar_axis(
            resolver,
            policy_key=POLICY_KEY_STRICT_COMPATIBLE,
            policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
            start_date=request.requested_window.start_date,
            end_date=request.requested_window.end_date,
            calendar_ids=calendar_ids,
            query_boundary=request.query_boundary,
        )
    if callable(resolver) and getattr(resolver, "__name__", "") in {
        "resolve_calendar_axis",
        "resolve_strict_compatible_axis",
    }:
        signature = inspect.signature(resolver)
        call_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in signature.parameters
        }
        try:
            signature.bind(**call_kwargs)
        except (TypeError, ValueError) as exc:
            raise UniverseScopeUnresolvedError(
                "calendar resolver does not expose the task-11 strict contract",
                details={"error_type": type(exc).__name__},
            ) from exc
        return resolver(**call_kwargs)
    raise UniverseScopeUnresolvedError(
        "calendar resolver does not expose strict compatibility capability",
        details={"calendar_ids": calendar_ids},
    )


def resolve_dynamic_universe_scope(
    request: DataPreflightRequest,
    provider: object | None = None,
    *,
    dynamic_scope_provider: object | None = None,
    scope_provider: object | None = None,
    fixed_calendar_ids: Iterable[str] = (),
    initial_position_calendar_ids: Iterable[str] = (),
    calendar_resolver: object | None = None,
    calendar_session_signature: str | None = None,
    profile: object | None = None,
    preflight_profile: object | None = None,
) -> UniverseScopeResolution:
    """Resolve dynamic range capability and finite named calendars.

    This function performs request-level preflight only.  It does not inspect
    candidate rows and does not call a strategy.  If a dynamic provider cannot
    prove its scope or the calendar axis cannot be strictly resolved, the
    result is ``blocked`` with a stable error code.
    """

    if not isinstance(request, DataPreflightRequest):
        raise InvalidDataRequestError("request must be a DataPreflightRequest")
    if profile is not None and preflight_profile is not None and profile != preflight_profile:
        raise InvalidDataRequestError("profile and preflight_profile must agree")
    selected_profile = profile if profile is not None else preflight_profile
    # No profile inference is allowed; an omitted profile is the formal
    # production path and therefore cannot consume internal fixtures.
    selected_profile = selected_profile or "formal@1"
    supplied_providers = [
        item for item in (provider, dynamic_scope_provider, scope_provider)
        if item is not None
    ]
    if len({id(item) for item in supplied_providers}) > 1:
        raise InvalidDataRequestError(
            "provider, dynamic_scope_provider, and scope_provider must agree"
        )
    if provider is None and supplied_providers:
        provider = supplied_providers[0]
    mode = request.instrument_scope_mode
    if provider is None and mode in (InstrumentScopeMode.DYNAMIC, InstrumentScopeMode.HYBRID):
        return UniverseScopeResolution(
            status=UniverseScopeStatus.BLOCKED,
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            rule_package_reference=request.rule_package,
            rule_exception_set_reference=request.rule_exception_set,
            qualification_policy_version=request.qualification_policy_version,
            scope_mode=mode,
            data_cutoff=request.query_boundary.data_cutoff,
            issues=(
                scope_issue(
                    "universe_capability_missing",
                    "Provider 未提供动态候选范围和资格证明能力。",
                    field="provider",
                ),
            ),
        )
    if mode is InstrumentScopeMode.FIXED:
        # Fixed requests do not need a dynamic range provider.  Calendar ids
        # still have to be supplied by the instrument/calendar preflight; this
        # helper never derives them from exchange or code prefixes.
        ids = merge_calendar_ids(fixed_calendar_ids, initial_position_calendar_ids)
        if not calendar_session_signature and ids and calendar_resolver is None:
            # Fixed preflight callers may supply the already-proven session
            # signature; without either that evidence or a strict resolver,
            # the calendar axis is not consumable.
            status = UniverseScopeStatus.BLOCKED
            fixed_issue = (
                scope_issue(
                    "universe_scope_unresolved",
                    "固定标的缺少正式区间日历兼容性证明。",
                    field="calendar_session_signature",
                ),
            )
        else:
            status = UniverseScopeStatus.READY if ids else UniverseScopeStatus.BLOCKED
            fixed_issue = (
                ()
                if ids
                else (
                    scope_issue(
                        "universe_scope_unresolved",
                        "固定标的未解析出具名交易日历。",
                        field="resolved_calendar_ids",
                    ),
                )
            )
        return UniverseScopeResolution(
            status=status,
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            rule_package_reference=request.rule_package,
            rule_exception_set_reference=request.rule_exception_set,
            qualification_policy_version=request.qualification_policy_version,
            resolved_calendar_ids=ids,
            calendar_session_signature=calendar_session_signature,
            scope_mode=mode,
            data_cutoff=request.query_boundary.data_cutoff,
            issues=fixed_issue,
        )

    try:
        raw = _provider_scope_result(provider, request, profile=selected_profile)
    except (UniverseCapabilityMissingError, UniverseScopeUnresolvedError) as exc:
        return UniverseScopeResolution(
            status=UniverseScopeStatus.BLOCKED,
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            rule_package_reference=request.rule_package,
            rule_exception_set_reference=request.rule_exception_set,
            qualification_policy_version=request.qualification_policy_version,
            scope_mode=mode,
            data_cutoff=request.query_boundary.data_cutoff,
            issues=(scope_issue(exc.code, "Provider 未提供动态候选查询或资格证明能力。", details=exc.details),),
        )
    except Exception as exc:
        return UniverseScopeResolution(
            status=UniverseScopeStatus.BLOCKED,
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            rule_package_reference=request.rule_package,
            rule_exception_set_reference=request.rule_exception_set,
            qualification_policy_version=request.qualification_policy_version,
            scope_mode=mode,
            data_cutoff=request.query_boundary.data_cutoff,
            issues=(
                scope_issue(
                    UniverseProviderContractViolationError.code,
                    "Provider 动态范围能力返回异常，已阻断预检。",
                    field="provider",
                    details={"error_type": type(exc).__name__},
                ),
                ),
            )
    try:
        resolution = (
            raw
            if isinstance(raw, UniverseScopeResolution)
            else _mapping_to_scope_resolution(raw, request)
            if isinstance(raw, Mapping)
            else UniverseScopeResolution(
                status=UniverseScopeStatus.READY,
                market_scope=request.market_scope,
                universe_query_policy=request.universe_query_policy,
                rule_package_reference=request.rule_package,
                rule_exception_set_reference=request.rule_exception_set,
                qualification_policy_version=request.qualification_policy_version,
                resolved_calendar_ids=tuple(raw) if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)) else ((raw,) if isinstance(raw, str) else ()),
                scope_mode=mode,
                data_cutoff=request.query_boundary.data_cutoff,
            )
        )
    except (InvalidDataRequestError, TypeError, ValueError) as exc:
        return UniverseScopeResolution(
            status=UniverseScopeStatus.BLOCKED,
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            rule_package_reference=request.rule_package,
            rule_exception_set_reference=request.rule_exception_set,
            qualification_policy_version=request.qualification_policy_version,
            scope_mode=mode,
            data_cutoff=request.query_boundary.data_cutoff,
            issues=(scope_issue("universe_scope_unresolved", "动态候选范围无法解析为有限具名交易日历。", details={"error_type": type(exc).__name__}),),
        )

    try:
        ids = merge_calendar_ids(
            fixed_calendar_ids,
            initial_position_calendar_ids,
            resolution.resolved_calendar_ids,
        )
    except InvalidDataRequestError as exc:
        return UniverseScopeResolution(
            status=UniverseScopeStatus.BLOCKED,
            market_scope=request.market_scope,
            universe_query_policy=request.universe_query_policy,
            rule_package_reference=request.rule_package,
            rule_exception_set_reference=request.rule_exception_set,
            qualification_policy_version=request.qualification_policy_version,
            scope_mode=mode,
            data_cutoff=request.query_boundary.data_cutoff,
            issues=(
                scope_issue(
                    "universe_scope_unresolved",
                    "动态范围无法解析为有限具名交易日历。",
                    field="resolved_calendar_ids",
                    details={"error_type": type(exc).__name__},
                ),
            ),
        )
    extra_issues = list(resolution.issues)
    # Provider evidence cannot replace any frozen request semantics.  A
    # mismatch is a request-level boundary failure, never a narrowed
    # candidate result.
    semantic_mismatches: list[str] = []
    if resolution.market_scope is not None and resolution.market_scope != request.market_scope:
        semantic_mismatches.append("market_scope")
    if resolution.universe_query_policy is not None and resolution.universe_query_policy != request.universe_query_policy:
        semantic_mismatches.append("universe_query_policy")
    if resolution.rule_package_reference is not None and resolution.rule_package_reference != request.rule_package:
        semantic_mismatches.append("rule_package_reference")
    if resolution.rule_exception_set_reference is not None and resolution.rule_exception_set_reference != request.rule_exception_set:
        semantic_mismatches.append("rule_exception_set_reference")
    if (
        resolution.qualification_policy_version is not None
        and resolution.qualification_policy_version
        != request.qualification_policy_version
    ):
        semantic_mismatches.append("qualification_policy_version")
    if resolution.scope_mode is not None and resolution.scope_mode != request.instrument_scope_mode:
        semantic_mismatches.append("scope_mode")
    if resolution.data_cutoff is not None and resolution.data_cutoff != request.query_boundary.data_cutoff:
        semantic_mismatches.append("data_cutoff")
    if semantic_mismatches:
        extra_issues.append(
            scope_issue(
                "universe_pit_boundary_violation",
                "动态范围 Provider 返回的冻结语义与请求不一致。",
                field="scope_snapshot",
                details={"mismatched_fields": sorted(semantic_mismatches)},
            )
        )
    if resolution.status is UniverseScopeStatus.BLOCKED and not extra_issues:
        extra_issues.append(
            scope_issue(
                "universe_scope_unresolved",
                "动态范围预检未通过，无法冻结候选范围。",
                field="status",
            )
        )
    if (
        resolution.status is UniverseScopeStatus.READY
        and mode in (InstrumentScopeMode.DYNAMIC, InstrumentScopeMode.HYBRID)
        and not resolution.resolved_calendar_ids
    ):
        extra_issues.append(
            scope_issue(
                "universe_scope_unresolved",
                "动态范围未解析出自身的有限具名交易日历集合。",
                field="resolved_calendar_ids",
            )
        )
    elif resolution.status is UniverseScopeStatus.READY and not ids:
        extra_issues.append(
            scope_issue(
                "universe_scope_unresolved",
                "动态范围未解析出有限具名交易日历集合。",
                field="resolved_calendar_ids",
            )
        )
    if len(ids) > 32:
        extra_issues.append(
            scope_issue(
                "universe_scope_unresolved",
                "动态范围解析出的交易日历数量超过预检资源上限。",
                field="resolved_calendar_ids",
                details={"observed": len(ids), "limit": 32},
            )
        )

    session_signature = resolution.calendar_session_signature
    source_evidence = dict(resolution.source_evidence)
    source_evidence["preflight_profile"] = _profile_text(selected_profile)
    axis_result: object | None = resolution.calendar_axis_resolution
    extra_issues.extend(
        _capability_status_issues(
            provider,
            request,
            resolution.capability_summary,
            profile=selected_profile,
        )
    )
    # A provider's free-form signature is never sufficient evidence.  The
    # strict task-11 resolver must either be supplied explicitly or have
    # returned its immutable ``CalendarAxisResolution`` as part of the scope
    # result.  In both cases the result is validated below before its
    # signature can enter the frozen scope.
    strict_axis = resolution.calendar_axis_resolution
    if calendar_resolver is not None and ids:
        try:
            axis = _resolve_calendar_axis(calendar_resolver, request, ids)
            axis_result = axis
            axis_status, axis_ids, axis_signature, axis_evidence, differences = _axis_evidence(
                axis,
                expected_start=request.requested_window.start_date,
                expected_end=request.requested_window.end_date,
            )
            if tuple(sorted(axis_ids)) != ids:
                extra_issues.append(
                    scope_issue(
                        "universe_scope_unresolved",
                        "严格日历预检返回的日历集合与冻结范围不一致。",
                        field="resolved_calendar_ids",
                        details={"expected": ids, "actual": tuple(sorted(axis_ids))},
                    )
                )
            if axis_status is UniverseScopeStatus.BLOCKED:
                extra_issues.append(
                    scope_issue(
                        "universe_scope_unresolved",
                        "参与日历未通过正式区间严格兼容性校验。",
                        field="calendar_axis",
                        details=axis_evidence,
                    )
                )
            if (
                resolution.calendar_session_signature
                and axis_signature
                and resolution.calendar_session_signature != axis_signature
            ):
                extra_issues.append(
                    scope_issue(
                        "universe_preflight_hash_mismatch",
                        "Provider 返回的日历会话签名与 strict resolver 结果不一致。",
                        field="calendar_session_signature",
                        details={
                            "provider_signature": resolution.calendar_session_signature,
                            "resolver_signature": axis_signature,
                        },
                    )
                )
            session_signature = axis_signature
            source_evidence["calendar_axis"] = axis_evidence
        except (UniverseScopeUnresolvedError, UniverseCapabilityMissingError) as exc:
            extra_issues.append(scope_issue(exc.code, "交易日历严格预检未完成。", details=exc.details))
            session_signature = None
        except Exception as exc:
            extra_issues.append(
                scope_issue(
                    "universe_scope_unresolved",
                    "交易日历严格预检未完成。",
                    field="calendar_axis",
                    details={"error_type": type(exc).__name__},
                )
            )
            session_signature = None
    elif ids and strict_axis is not None:
        try:
            axis_status, axis_ids, axis_signature, axis_evidence, differences = _axis_evidence(
                strict_axis,
                expected_start=request.requested_window.start_date,
                expected_end=request.requested_window.end_date,
            )
            axis_result = strict_axis
            if tuple(sorted(axis_ids)) != ids:
                extra_issues.append(
                    scope_issue(
                        "universe_scope_unresolved",
                        "严格日历预检返回的日历集合与冻结范围不一致。",
                        field="resolved_calendar_ids",
                        details={"expected": ids, "actual": tuple(sorted(axis_ids))},
                    )
                )
            if axis_status is UniverseScopeStatus.BLOCKED:
                extra_issues.append(
                    scope_issue(
                        "universe_scope_unresolved",
                        "参与日历未通过正式区间严格兼容性校验。",
                        field="calendar_axis",
                        details=axis_evidence,
                    )
                )
            if (
                resolution.calendar_session_signature
                and axis_signature
                and resolution.calendar_session_signature != axis_signature
            ):
                extra_issues.append(
                    scope_issue(
                        "universe_preflight_hash_mismatch",
                        "Provider 返回的日历会话签名与 strict resolver 结果不一致。",
                        field="calendar_session_signature",
                        details={
                            "provider_signature": resolution.calendar_session_signature,
                            "resolver_signature": axis_signature,
                        },
                    )
                )
            session_signature = axis_signature
            source_evidence["calendar_axis"] = axis_evidence
        except UniverseScopeUnresolvedError as exc:
            extra_issues.append(
                scope_issue(
                    exc.code,
                    "交易日历严格预检未完成。",
                    field="calendar_axis",
                    details=exc.details,
                )
            )
            session_signature = None
    elif ids:
        # ``calendar_session_signature`` is retained only as an audit hint in
        # the provider result; it cannot make an unresolved axis consumable.
        extra_issues.append(
            scope_issue(
                "universe_scope_unresolved",
                "动态范围缺少任务包 11 strict_compatible@1 日历兼容性证明。",
                field="calendar_axis",
                details={"provided_signature": bool(calendar_session_signature)},
            )
        )
        session_signature = None
    current_scope_hash = resolution.current_snapshot_hash
    if (
        current_scope_hash is not None
        and current_scope_hash != resolution.snapshot_hash
    ):
        extra_issues.append(
            scope_issue(
                "universe_preflight_hash_mismatch",
                "动态范围会话复检哈希与准入快照不一致，已阻断请求。",
                field="current_snapshot_hash",
                details={
                    "expected": resolution.snapshot_hash,
                    "actual": current_scope_hash,
                },
            )
        )

    status = (
        UniverseScopeStatus.READY
        if resolution.status is UniverseScopeStatus.READY and not extra_issues
        else UniverseScopeStatus.BLOCKED
    )
    return UniverseScopeResolution(
        status=status,
        market_scope=resolution.market_scope or request.market_scope,
        universe_query_policy=resolution.universe_query_policy or request.universe_query_policy,
        rule_package_reference=resolution.rule_package_reference or request.rule_package,
        rule_exception_set_reference=(
            resolution.rule_exception_set_reference
            if resolution.rule_exception_set_reference is not None
            else request.rule_exception_set
        ),
        qualification_policy_version=(
            resolution.qualification_policy_version
            if resolution.qualification_policy_version is not None
            else request.qualification_policy_version
        ),
        resolved_calendar_ids=ids,
        capability_summary=resolution.capability_summary,
        source_evidence=source_evidence,
        issues=tuple(extra_issues),
        calendar_session_signature=session_signature,
        calendar_axis_resolution=axis_result,
        scope_mode=mode,
        data_cutoff=request.query_boundary.data_cutoff,
    )


def fixed_instrument_ids_for_preflight(
    request: DataPreflightRequest,
    *,
    non_zero_initial_position_instrument_ids: Iterable[UUID] = (),
) -> tuple[UUID, ...]:
    """Return the mandatory fixed union for one request."""

    if not isinstance(request, DataPreflightRequest):
        raise InvalidDataRequestError("request must be a DataPreflightRequest")
    return fixed_instrument_ids(
        request.static_instrument_ids,
        request.mandatory_instrument_ids,
        (
            *request.non_zero_initial_position_instrument_ids,
            *tuple(non_zero_initial_position_instrument_ids),
        ),
    )


def _fixed_report_value(report: object, name: str, default: object = None) -> object:
    """Read one field from a report object or JSON-shaped report."""

    if isinstance(report, Mapping):
        return report.get(name, default)
    return getattr(report, name, default)


def _fixed_report_records(report: object) -> tuple[object, ...]:
    """Extract explicit per-instrument results from an existing report.

    A top-level ``blocked`` flag is not sufficient evidence: it says nothing
    about whether every static, mandatory, and opening-position instrument was
    checked.  Only reports carrying an explicit result for each instrument
    may satisfy the fixed-union gate.
    """

    result_fields = (
        "checked_instruments",
        "checked_positions",
        "instrument_results",
        "position_results",
        "results",
    )
    records: list[object] = []
    queue: list[object] = [report]
    visited: set[int] = set()
    while queue:
        current = queue.pop(0)
        if id(current) in visited:
            continue
        visited.add(id(current))
        for name in result_fields:
            value = _fixed_report_value(current, name)
            if value is None:
                continue
            if isinstance(value, Mapping):
                values = tuple(value.values())
            elif isinstance(value, (str, bytes)):
                values = ()
            else:
                try:
                    values = tuple(value)
                except TypeError:
                    values = ()
            records.extend(values)
        # A composition object may expose the two existing task-13/initial
        # position reports under descriptive names.  Recurse into them, but
        # never inspect arbitrary provider rows or candidate lists.
        for name in (
            "fixed_preflight_report",
            "rule_preflight_report",
            "initial_position_preflight_report",
            "position_preflight_report",
        ):
            nested = _fixed_report_value(current, name)
            if nested is not None and nested is not current:
                queue.append(nested)
    return tuple(records)


def _fixed_report_covers_union(
    report: object,
    fixed_ids: Iterable[UUID],
) -> tuple[bool, Mapping[str, object]]:
    """Prove that a fixed report checked the complete required union."""

    expected = tuple(sorted(set(fixed_ids), key=str))
    if not expected:
        return True, {"expected_instrument_ids": ()}
    status = _fixed_report_value(report, "status")
    status_text = getattr(status, "value", status)
    blocked_flag = _fixed_report_value(report, "blocked", False)
    if status is None:
        return False, {
            "expected_instrument_ids": [str(item) for item in expected],
            "report_status": None,
            "reason": "missing_report_status",
        }
    if bool(blocked_flag) or str(status_text).lower() in {
        "blocked",
        "incomplete",
        "failed",
        "error",
    }:
        return False, {
            "expected_instrument_ids": [str(item) for item in expected],
            "report_status": str(status_text),
        }
    records = _fixed_report_records(report)
    observed: set[UUID] = set()
    bad: list[str] = []
    for record in records:
        instrument_id = _fixed_report_value(record, "instrument_id")
        if not isinstance(instrument_id, UUID):
            try:
                instrument_id = UUID(str(instrument_id))
            except (TypeError, ValueError, AttributeError):
                bad.append("missing_instrument_id")
                continue
        observed.add(instrument_id)
        record_status = _fixed_report_value(record, "status")
        record_status = getattr(record_status, "value", record_status)
        issues = _fixed_report_value(record, "issues", ())
        if str(record_status).lower() not in {
            "ready",
            "ok",
            "complete",
            "eligible",
            "not_applicable",
        } or str(record_status).lower() in {
            "blocked",
            "incomplete",
            "failed",
            "error",
            "ineligible",
        } or bool(issues):
            bad.append(str(instrument_id))
    missing = sorted(set(expected) - observed, key=str)
    unexpected = sorted(observed - set(expected), key=str)
    if missing or unexpected or bad:
        return False, {
            "expected_instrument_ids": [str(item) for item in expected],
            "observed_instrument_ids": [str(item) for item in sorted(observed, key=str)],
            "missing_instrument_ids": [str(item) for item in missing],
            "unexpected_instrument_ids": [str(item) for item in unexpected],
            "invalid_instrument_ids": bad,
        }
    return True, {
        "expected_instrument_ids": [str(item) for item in expected],
        "observed_instrument_ids": [str(item) for item in sorted(observed, key=str)],
    }


class UniversePreflightService:
    """Compose fixed-position and dynamic-scope preflight without I/O of its own.

    ``fixed_preflight_service`` is the existing initial-position preflight
    service supplied by the caller.  This class only propagates its result and
    never reimplements accounting, Bar coverage, rules, or calendar logic.
    """

    def __init__(
        self,
        provider: object | None = None,
        *,
        scope_provider: object | None = None,
        dynamic_scope_provider: object | None = None,
        fixed_preflight_service: InitialPositionPreflightService | None = None,
        calendar_resolver: object | None = None,
    ) -> None:
        alternatives = [item for item in (provider, scope_provider, dynamic_scope_provider) if item is not None]
        if len({id(item) for item in alternatives}) > 1:
            raise InvalidDataRequestError(
                "provider, scope_provider, and dynamic_scope_provider must agree"
            )
        self._provider = alternatives[0] if alternatives else None
        self._fixed_preflight_service = fixed_preflight_service
        self._calendar_resolver = calendar_resolver

    def run(
        self,
        request: DataPreflightRequest,
        *,
        spec: BacktestSpec | None = None,
        fixed_calendar_ids: Iterable[str] = (),
        initial_position_calendar_ids: Iterable[str] = (),
        initial_position_instrument_ids: Iterable[UUID] = (),
        non_zero_initial_position_ids: Iterable[UUID] = (),
        fixed_preflight_report: object | None = None,
        candidate_count: int = 0,
        filtered_reason_counts: Mapping[str, int] | None = None,
        profile: object | None = None,
        preflight_profile: object | None = None,
    ) -> UniversePreflightReport:
        """Run one deterministic fixed/dynamic/hybrid preflight composition."""

        if not isinstance(request, DataPreflightRequest):
            raise InvalidDataRequestError("request must be a DataPreflightRequest")
        position_ids = (
            tuple(position.instrument_id for position in spec.non_zero_initial_positions)
            if spec is not None
            else (
                *request.non_zero_initial_position_instrument_ids,
                *tuple(initial_position_instrument_ids),
                *tuple(non_zero_initial_position_ids),
            )
        )
        if spec is not None:
            position_ids = (
                *position_ids,
                *tuple(initial_position_instrument_ids),
                *tuple(non_zero_initial_position_ids),
            )
        fixed_ids = fixed_instrument_ids_for_preflight(
            request, non_zero_initial_position_instrument_ids=position_ids
        )
        if fixed_preflight_report is None and self._fixed_preflight_service is not None and spec is not None:
            fixed_preflight_report = self._fixed_preflight_service.run(spec)
        report_calendar_ids = (
            tuple(getattr(fixed_preflight_report, "resolved_calendar_ids", ()))
            if fixed_preflight_report is not None
            else ()
        )
        report_session_signature = (
            getattr(fixed_preflight_report, "calendar_session_signature", None)
            if fixed_preflight_report is not None
            else None
        )
        resolution = resolve_dynamic_universe_scope(
            request,
            self._provider,
            fixed_calendar_ids=(*tuple(fixed_calendar_ids), *report_calendar_ids),
            initial_position_calendar_ids=initial_position_calendar_ids,
            calendar_resolver=self._calendar_resolver,
            calendar_session_signature=report_session_signature,
            profile=profile,
            preflight_profile=preflight_profile,
        )
        issues = list(resolution.issues)
        if fixed_ids and fixed_preflight_report is None:
            issues.append(
                scope_issue(
                    "universe_scope_unresolved",
                    "固定标的尚未完成完整窗口预检。",
                    field="fixed_preflight_report",
                    details={"fixed_instrument_ids": [str(item) for item in fixed_ids]},
                )
            )
        if fixed_preflight_report is not None and fixed_ids:
            covered, coverage_evidence = _fixed_report_covers_union(
                fixed_preflight_report, fixed_ids
            )
            if not covered:
                issues.append(
                    scope_issue(
                        "universe_scope_unresolved",
                        "固定标的完整窗口预检未覆盖全部强制固定对象，已阻断请求。",
                        field="fixed_preflight_report",
                        details={
                            **coverage_evidence,
                            "report_hash": _fixed_report_value(
                                fixed_preflight_report, "report_hash"
                            ),
                        },
                    )
                )
        if filtered_reason_counts is None:
            filtered_reason_counts = {}
        status = UniverseScopeStatus.READY if resolution.ready and not issues else UniverseScopeStatus.BLOCKED
        return UniversePreflightReport(
            status=status,
            scope_mode=request.instrument_scope_mode,
            fixed_instrument_ids=fixed_ids,
            resolved_calendar_ids=resolution.resolved_calendar_ids,
            scope_resolution=resolution,
            fixed_preflight_report=fixed_preflight_report,
            filtered_reason_counts=filtered_reason_counts,
            candidate_count=candidate_count,
            issues=tuple(issues),
            scope_snapshot_hash=resolution.snapshot_hash,
        )

    preflight = run


# Functional façade names keep integrations declarative while sharing the
# same orchestration implementation and error semantics.
preflight_universe = resolve_dynamic_universe_scope


def run_universe_preflight(
    request: DataPreflightRequest,
    provider: object | None = None,
    **kwargs: object,
) -> UniversePreflightReport:
    """Run the fixed/dynamic/hybrid composition through one service."""

    return UniversePreflightService(provider).run(request, **kwargs)
