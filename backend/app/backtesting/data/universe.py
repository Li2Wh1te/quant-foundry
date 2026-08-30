"""Point-in-time candidate-universe contracts and pure qualification helpers.

The objects in this module are deliberately small, immutable value objects.
They describe *what has already been resolved* by an upstream provider; they
do not know how to query a database, call a network service, discover a
calendar, or invoke a strategy.  This is important for PIT correctness: a
candidate filter must be deterministic and must never repair missing history
with a current catalogue value.

The module is also the narrow boundary shared by dynamic-universe preflight,
the provider implementation, and the final target re-check.  Providers may
attach richer evidence, but the machine-facing fields below remain stable and
JSON-safe.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import normalize_calendar_id
from app.backtesting.data.errors import (
    InvalidDataRequestError,
    UniverseCalendarNotPreflightedError,
    UniversePitBoundaryViolationError,
    UniverseProviderContractViolationError,
)
from app.backtesting.data.requests import (
    ContractRef,
    DataCapability,
    DateRange,
    InstrumentScopeMode,
    MarketScope,
    QueryBoundary,
    UniverseQueryPolicy,
)

try:  # ``reports`` is intentionally not required for import-cycle safety.
    from app.backtesting.data.reports import canonical_hash
except ImportError:  # pragma: no cover - only protects unusual import order
    canonical_hash = None  # type: ignore[assignment]


__all__ = [
    "CANDIDATE_CALENDAR_NOT_PREFLIGHTED",
    "CANDIDATE_CALENDAR_MISSING",
    "CANDIDATE_CORPORATE_ACTION_INCOMPLETE",
    "CANDIDATE_IDENTITY_INCOMPLETE",
    "CANDIDATE_MAPPING_INCOMPLETE",
    "CANDIDATE_MARKET_DATA_INCOMPLETE",
    "CANDIDATE_OUTSIDE_MARKET_SCOPE",
    "CANDIDATE_PIT_BOUNDARY_VIOLATION",
    "CANDIDATE_QUALIFICATION_UNAVAILABLE",
    "CANDIDATE_RULE_INCOMPLETE",
    "CANDIDATE_STATUS_INCOMPLETE",
    "CANDIDATE_QUANTITY_ACTION_COVERAGE_INCOMPLETE",
    "CandidateEligibility",
    "CandidateEligibilityContext",
    "CandidateFilterResult",
    "CandidateInput",
    "InstrumentCandidateInput",
    "CandidateEligibilityResult",
    "ScopeResolutionStatus",
    "UniversePreflightReport",
    "UniverseScopeIssue",
    "UniverseScopeResolution",
    "UniverseScopeStatus",
    "UniverseScopeResolutionStatus",
    "compute_universe_scope_snapshot_hash",
    "build_universe_eligibility_summary",
    "evaluate_candidate",
    "filter_candidates",
    "merge_calendar_ids",
    "scope_issue",
]


# Stable machine codes.  They are intentionally lower snake case because
# they can be copied into ``validation_issues`` and queried by operators.
CANDIDATE_IDENTITY_INCOMPLETE = "candidate_identity_incomplete"
CANDIDATE_MAPPING_INCOMPLETE = "candidate_mapping_incomplete"
CANDIDATE_OUTSIDE_MARKET_SCOPE = "candidate_outside_market_scope"
CANDIDATE_CALENDAR_MISSING = "candidate_calendar_missing"
CANDIDATE_CALENDAR_NOT_PREFLIGHTED = "universe_calendar_not_preflighted"
CANDIDATE_RULE_INCOMPLETE = "candidate_rule_incomplete"
CANDIDATE_MARKET_DATA_INCOMPLETE = "candidate_market_data_incomplete"
CANDIDATE_CORPORATE_ACTION_INCOMPLETE = "candidate_corporate_action_incomplete"
CANDIDATE_QUANTITY_ACTION_COVERAGE_INCOMPLETE = (
    "candidate_quantity_action_coverage_incomplete"
)
CANDIDATE_STATUS_INCOMPLETE = "candidate_status_incomplete"
CANDIDATE_PIT_BOUNDARY_VIOLATION = "universe_pit_boundary_violation"
CANDIDATE_QUALIFICATION_UNAVAILABLE = "candidate_qualification_unavailable"


class UniverseScopeStatus(StrEnum):
    """Outcome of resolving a dynamic scope before strategy execution."""

    READY = "ready"
    BLOCKED = "blocked"


# A descriptive alias used by some callers and older design notes.  It is an
# alias, not a second enum or a second scope model.
ScopeResolutionStatus = UniverseScopeStatus
UniverseScopeResolutionStatus = UniverseScopeStatus


def _plain_date(value: object, field_name: str) -> date:
    """Require a calendar date and reject timezone-carrying datetimes."""

    if isinstance(value, datetime) or not isinstance(value, date):
        raise InvalidDataRequestError(f"{field_name} must be a calendar date")
    return value


def _aware_datetime(value: object, field_name: str) -> datetime:
    """Require an aware instant; a wall-clock value has no PIT meaning."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidDataRequestError(f"{field_name} must be timezone-aware")
    return value


def _evidence_datetime(value: object, field_name: str) -> datetime:
    """Normalize datetime evidence emitted as an ISO string or datetime."""

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise InvalidDataRequestError(f"{field_name} must be ISO datetime") from exc
    return _aware_datetime(value, field_name)


def _non_blank(value: object, field_name: str) -> str:
    """Require a plain non-empty string for machine identifiers."""

    if type(value) is not str or not value.strip():
        raise InvalidDataRequestError(f"{field_name} must be non-blank text")
    return value.strip()


def _json_value(value: object, where: str = "value") -> object:
    """Convert supported domain scalars to canonical JSON-compatible values.

    Evidence is intentionally not allowed to carry arbitrary Python objects.
    Unknown objects are represented by their type name only; this keeps audit
    details safe without serializing ORM rows, clients, credentials, or
    connection handles.
    """

    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidDataRequestError(f"{where} float values must be finite")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, ContractRef):
        return {"key": value.key, "version": value.version}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise InvalidDataRequestError(f"{where} mapping keys must be strings")
            # Audit evidence is allowed to contain operational context, but
            # it must never become a side channel for credentials.  Reject
            # sensitive *keys* before recursing so a token cannot be hidden
            # inside a nested mapping or list and later reach a report/hash.
            normalized_key = key.strip().lower().replace("-", "_").replace(".", "_")
            if any(
                marker in normalized_key
                for marker in (
                    "credential",
                    "token",
                    "password",
                    "secret",
                    "api_key",
                    "api_token",
                    "authorization",
                    "private_key",
                    "access_key",
                )
            ):
                raise InvalidDataRequestError(
                    f"{where} contains a sensitive audit key",
                    details={"field": where, "key": key},
                )
            result[key] = _json_value(item, f"{where}[{key!r}]")
        return result
    if isinstance(value, (tuple, list)):
        return [_json_value(item, where) for item in value]
    # Do not serialize an arbitrary object's ``__dict__``.  The type marker is
    # enough to explain why a provider did not expose JSON evidence.
    return {"type": type(value).__name__}


def _freeze_mapping(value: Mapping[str, object] | None, field_name: str) -> Mapping[str, object]:
    """Deep-freeze a JSON-safe evidence mapping."""

    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise InvalidDataRequestError(f"{field_name} must be a mapping")
    normalized = _json_value(dict(value), field_name)
    if not isinstance(normalized, dict):  # pragma: no cover - defensive
        raise InvalidDataRequestError(f"{field_name} must be a mapping")

    def freeze(item: object) -> object:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(value) for key, value in item.items()})
        if isinstance(item, list):
            return tuple(freeze(value) for value in item)
        return item

    frozen = freeze(normalized)
    assert isinstance(frozen, MappingProxyType)
    return frozen


def _freeze_value(value: object, field_name: str) -> object:
    """Normalize and deep-freeze one JSON-safe audit value."""

    normalized = _json_value(value, field_name)

    def freeze(item: object) -> object:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(value) for key, value in item.items()})
        if isinstance(item, list):
            return tuple(freeze(value) for value in item)
        return item

    return freeze(normalized)


_VOLATILE_AUDIT_KEYS = frozenset(
    {
        "generated_at",
        "run_id",
        "database_id",
        "db_id",
        "auto_increment_id",
        "message",
        "title",
        "candidate_list",
        "candidates",
        "candidate_ids",
        "candidate_count",
        "eligible_instrument_ids",
        "filtered_reason_counts",
        "target_ids",
        "final_rechecks",
        "candidate_snapshots",
        "candidate_results",
        "universe_candidates",
    }
)


def _stable_audit_value(value: object) -> object:
    """Remove display/volatile fields before calculating a scope hash."""

    if isinstance(value, Mapping):
        return {
            key: _stable_audit_value(item)
            for key, item in value.items()
            if key not in _VOLATILE_AUDIT_KEYS
        }
    if isinstance(value, (tuple, list)):
        return [_stable_audit_value(item) for item in value]
    return value


def _ref_payload(reference: ContractRef | str | None) -> object:
    """Return a stable payload for a versioned policy reference."""

    if reference is None:
        return None
    if isinstance(reference, ContractRef):
        return {"key": reference.key, "version": reference.version}
    return _non_blank(reference, "policy reference")


def _sorted_codes(values: Iterable[object]) -> tuple[str, ...]:
    """Normalize reason codes without making result order provider-defined."""

    codes: set[str] = set()
    for value in values:
        if isinstance(value, StrEnum):
            value = value.value
        if not isinstance(value, str) or not value.strip():
            continue
        codes.add(value.strip())
    return tuple(sorted(codes))


@dataclass(frozen=True, slots=True)
class UniverseScopeIssue:
    """One request-level scope resolution issue.

    ``message`` is operator-facing Chinese text; machine logic must branch on
    ``code``.  The message is intentionally excluded from scope hashes.
    """

    code: str
    message: str
    field: str | None = None
    details: Mapping[str, object] = dc_field(default_factory=dict)
    severity: str = "error"

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _non_blank(self.code, "issue.code"))
        object.__setattr__(self, "message", _non_blank(self.message, "issue.message"))
        if self.field is not None:
            object.__setattr__(self, "field", _non_blank(self.field, "issue.field"))
        severity = getattr(self.severity, "value", self.severity)
        if type(severity) is not str or severity.strip().lower() not in {"warning", "error"}:
            raise InvalidDataRequestError("issue.severity must be warning or error")
        object.__setattr__(self, "severity", severity.strip().lower())
        object.__setattr__(self, "details", _freeze_mapping(self.details, "issue.details"))

    def machine_content(self) -> Mapping[str, object]:
        """Return hash-relevant evidence without display copy."""

        return MappingProxyType(
            {
                "code": self.code,
                "field": self.field,
                "severity": self.severity,
                "details": self.details,
            }
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe operator/API projection."""

        return {
            "code": self.code,
            "field": self.field,
            "severity": self.severity,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class UniverseScopeResolution:
    """Immutable result of resolving a dynamic scope and named calendars.

    A ready dynamic/hybrid result always carries a finite, canonical calendar
    set.  The object does not contain a candidate list: daily membership is a
    runtime query and must never change the admission snapshot hash.
    """

    status: UniverseScopeStatus = UniverseScopeStatus.BLOCKED
    market_scope: MarketScope | None = None
    universe_query_policy: UniverseQueryPolicy | None = None
    rule_package_reference: ContractRef | None = None
    rule_exception_set_reference: ContractRef | None = None
    qualification_policy_version: ContractRef | str | None = None
    resolved_calendar_ids: tuple[str, ...] = ()
    capability_summary: Mapping[str, object] = dc_field(default_factory=dict)
    source_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    issues: tuple[UniverseScopeIssue, ...] = ()
    calendar_session_signature: str | None = None
    calendar_axis_resolution: object | None = None
    scope_mode: InstrumentScopeMode | None = None
    data_cutoff: datetime | None = None
    snapshot_hash: str = ""
    # Compatibility spellings for adapters that use the shorter names from
    # the architecture notes.  They normalize into the canonical fields.
    rule_package: ContractRef | None = None
    rule_exception_set: ContractRef | None = None
    qualification_policy: ContractRef | str | None = None
    calendar_ids: tuple[str, ...] = ()
    # Optional authority-side observation used by a session re-check.  It is
    # deliberately excluded from ``snapshot_hash``: the frozen hash remains
    # the expected value and a changed current observation must fail closed.
    current_snapshot_hash: str | None = None

    def __post_init__(self) -> None:
        try:
            status = UniverseScopeStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise InvalidDataRequestError("status must be ready or blocked") from exc
        object.__setattr__(self, "status", status)
        if self.market_scope is not None and not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        if self.universe_query_policy is not None and not isinstance(
            self.universe_query_policy, UniverseQueryPolicy
        ):
            raise InvalidDataRequestError(
                "universe_query_policy must be a UniverseQueryPolicy"
            )
        if self.rule_package_reference is not None and not isinstance(
            self.rule_package_reference, ContractRef
        ):
            raise InvalidDataRequestError("rule_package_reference must be a ContractRef")
        package = self.rule_package_reference or self.rule_package
        if package is not None and not isinstance(package, ContractRef):
            raise InvalidDataRequestError("rule_package must be a ContractRef")
        if self.rule_package_reference is not None and self.rule_package is not None and self.rule_package_reference != self.rule_package:
            raise InvalidDataRequestError("rule_package and rule_package_reference disagree")
        object.__setattr__(self, "rule_package_reference", package)
        object.__setattr__(self, "rule_package", package)
        exception = self.rule_exception_set_reference or self.rule_exception_set
        if self.rule_exception_set_reference is not None and not isinstance(
            self.rule_exception_set_reference, ContractRef
        ):
            raise InvalidDataRequestError(
                "rule_exception_set_reference must be a ContractRef"
            )
        if exception is not None and not isinstance(exception, ContractRef):
            raise InvalidDataRequestError("rule_exception_set must be a ContractRef")
        if self.rule_exception_set_reference is not None and self.rule_exception_set is not None and self.rule_exception_set_reference != self.rule_exception_set:
            raise InvalidDataRequestError("rule_exception_set and rule_exception_set_reference disagree")
        object.__setattr__(self, "rule_exception_set_reference", exception)
        object.__setattr__(self, "rule_exception_set", exception)
        qualification = self.qualification_policy_version or self.qualification_policy
        if qualification is not None and not isinstance(qualification, (ContractRef, str)):
            raise InvalidDataRequestError("qualification_policy must be a ContractRef or text")
        if isinstance(qualification, str):
            qualification = _non_blank(qualification, "qualification_policy")
        if self.qualification_policy_version is not None and self.qualification_policy is not None and self.qualification_policy_version != self.qualification_policy:
            raise InvalidDataRequestError("qualification policy fields disagree")
        object.__setattr__(self, "qualification_policy_version", qualification)
        object.__setattr__(self, "qualification_policy", qualification)
        ids: list[str] = []
        raw_ids = self.resolved_calendar_ids or self.calendar_ids
        try:
            ids = sorted({normalize_calendar_id(item) for item in raw_ids})
        except Exception as exc:
            raise InvalidDataRequestError(
                "resolved_calendar_ids must contain canonical calendar ids"
            ) from exc
        object.__setattr__(self, "resolved_calendar_ids", tuple(ids))
        object.__setattr__(self, "calendar_ids", tuple(ids))
        object.__setattr__(
            self,
            "capability_summary",
            _freeze_mapping(self.capability_summary, "capability_summary"),
        )
        object.__setattr__(
            self,
            "source_evidence",
            _freeze_mapping(self.source_evidence, "source_evidence"),
        )
        issues = tuple(self.issues)
        if any(not isinstance(issue, UniverseScopeIssue) for issue in issues):
            raise InvalidDataRequestError(
                "issues must contain UniverseScopeIssue instances"
            )
        object.__setattr__(
            self,
            "issues",
            tuple(sorted(issues, key=lambda issue: (issue.code, issue.field or "", canonical_hash(dict(issue.details)) if canonical_hash else ""))),
        )
        if self.calendar_session_signature is not None:
            object.__setattr__(
                self,
                "calendar_session_signature",
                _non_blank(self.calendar_session_signature, "calendar_session_signature"),
            )
        if self.scope_mode is not None and not isinstance(
            self.scope_mode, InstrumentScopeMode
        ):
            try:
                object.__setattr__(self, "scope_mode", InstrumentScopeMode(self.scope_mode))
            except (TypeError, ValueError) as exc:
                raise InvalidDataRequestError("scope_mode must be an InstrumentScopeMode") from exc
        if self.data_cutoff is not None:
            object.__setattr__(
                self,
                "data_cutoff",
                _aware_datetime(self.data_cutoff, "data_cutoff"),
            )
        if status is UniverseScopeStatus.READY and self.scope_mode in (
            InstrumentScopeMode.DYNAMIC,
            InstrumentScopeMode.HYBRID,
        ) and not ids:
            raise InvalidDataRequestError(
                "a ready dynamic or hybrid scope requires named calendar ids"
            )
        if status is UniverseScopeStatus.READY and self.scope_mode in (
            InstrumentScopeMode.DYNAMIC,
            InstrumentScopeMode.HYBRID,
        ):
            # A scope object constructed outside the shared resolver must be
            # just as fail-closed as one returned by a provider.  The five
            # core dimensions are the minimum proof needed before optional
            # action/status dimensions can be evaluated from the request.
            aliases = {
                "universe": {"universe", "universe_query", "pit_universe", "candidate_universe"},
                "identity": {"identity", "pit_identity", "instrument_identity", "instrument_spec"},
                "mapping": {"mapping", "mappings", "pit_mapping", "instrument_mapping"},
                "rules": {"rule", "rules", "rule_package", "rule_qualification", "qualification"},
                "market_data": {"bar", "bars", "market_data", "raw_bars", "coverage", "coverage_qualification", "history"},
            }
            declared = {
                str(key).strip().lower().replace("-", "_").replace(".", "_")
                for key in self.capability_summary
            }
            missing = sorted(
                bucket
                for bucket, names in aliases.items()
                if not declared.intersection(names)
            )
            if missing:
                status = UniverseScopeStatus.BLOCKED
                issues = tuple(issues) + (
                    UniverseScopeIssue(
                        code="universe_capability_missing",
                        message="动态范围缺少必要资格能力声明，已阻断预检。",
                        field="capability_summary",
                        details={"missing_capabilities": missing},
                    ),
                )
                object.__setattr__(self, "status", status)
                object.__setattr__(
                    self,
                    "issues",
                    tuple(
                        sorted(
                            issues,
                            key=lambda issue: (
                                issue.code,
                                issue.field or "",
                                canonical_hash(dict(issue.details))
                                if canonical_hash
                                else "",
                            ),
                        )
                    ),
                )
        if self.current_snapshot_hash is not None:
            current_hash = _non_blank(self.current_snapshot_hash, "current_snapshot_hash")
            if len(current_hash) != 64 or any(
                char not in "0123456789abcdef" for char in current_hash
            ):
                raise InvalidDataRequestError(
                    "current_snapshot_hash must be a lowercase SHA-256 digest"
                )
            object.__setattr__(self, "current_snapshot_hash", current_hash)
        computed = compute_universe_scope_snapshot_hash(self)
        # Reports in this codebase recompute their hashes defensively.  Apply
        # the same rule here so generated_at/run ids or a forged placeholder
        # can never alter admission evidence.
        object.__setattr__(self, "snapshot_hash", computed)

    @property
    def ready(self) -> bool:
        """Whether dynamic scope admission can proceed."""

        return self.status is UniverseScopeStatus.READY

    @property
    def blocked(self) -> bool:
        """Whether this resolution is a request-level hard block."""

        return self.status is UniverseScopeStatus.BLOCKED

    @property
    def scope_snapshot_hash(self) -> str:
        """Stable alias used by request/report adapters."""

        return self.snapshot_hash

    @property
    def resolution_hash(self) -> str:
        """Compatibility alias for audit consumers."""

        return self.snapshot_hash

    @property
    def provider_capability_status(self) -> Mapping[str, object]:
        """Stable alias for the capability evidence mapping."""

        return self.capability_summary

    @property
    def qualification_policy_reference(self) -> ContractRef | str | None:
        """Short alias for the frozen candidate qualification policy."""

        return self.qualification_policy_version

    @property
    def primary_issue_code(self) -> str | None:
        """Stable first request-level error code."""

        return self.issues[0].code if self.issues else None

    def canonical_content(self) -> Mapping[str, object]:
        """Return content used by ``snapshot_hash`` only."""

        scope = self.market_scope
        policy = self.universe_query_policy
        return MappingProxyType(
            {
                "status": self.status.value,
                "scope_mode": self.scope_mode.value if self.scope_mode else None,
                "market_scope": {
                    "markets": scope.markets if scope else (),
                    "exchanges": scope.exchanges if scope else (),
                    "asset_classes": scope.asset_classes if scope else (),
                    "currencies": scope.currencies if scope else (),
                },
                "universe_query_policy": (
                    [
                        {"key": ref.key, "version": ref.version}
                        for ref in policy.candidate_set_rules
                    ]
                    if policy
                    else []
                ),
                "rule_package_reference": _ref_payload(self.rule_package_reference),
                "rule_exception_set_reference": _ref_payload(
                    self.rule_exception_set_reference
                ),
                "qualification_policy_version": _ref_payload(
                    self.qualification_policy_version
                ),
                "resolved_calendar_ids": self.resolved_calendar_ids,
                "calendar_session_signature": self.calendar_session_signature,
                "capability_summary": _stable_audit_value(self.capability_summary),
                "source_evidence": _stable_audit_value(self.source_evidence),
                "issues": [issue.machine_content() for issue in self.issues],
                "data_cutoff": self.data_cutoff,
            }
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the resolution without exposing arbitrary provider rows."""

        return {
            "status": self.status.value,
            "scope_mode": self.scope_mode.value if self.scope_mode else None,
            "market_scope": (
                {
                    "markets": self.market_scope.markets,
                    "exchanges": self.market_scope.exchanges,
                    "asset_classes": self.market_scope.asset_classes,
                    "currencies": self.market_scope.currencies,
                }
                if self.market_scope
                else None
            ),
            "universe_query_policy": (
                [
                    {"key": ref.key, "version": ref.version}
                    for ref in self.universe_query_policy.candidate_set_rules
                ]
                if self.universe_query_policy
                else []
            ),
            "rule_package_reference": _ref_payload(self.rule_package_reference),
            "rule_exception_set_reference": _ref_payload(self.rule_exception_set_reference),
            "qualification_policy_version": _ref_payload(
                self.qualification_policy_version
            ),
            "resolved_calendar_ids": self.resolved_calendar_ids,
            "capability_summary": self.capability_summary,
            "source_evidence": self.source_evidence,
            "calendar_session_signature": self.calendar_session_signature,
            "snapshot_hash": self.snapshot_hash,
            "issues": [issue.as_dict() for issue in self.issues],
        }

    to_summary = as_dict


def compute_universe_scope_snapshot_hash(
    resolution: UniverseScopeResolution | Mapping[str, object]
) -> str:
    """Hash stable scope semantics, never candidate lists or display text."""

    if isinstance(resolution, Mapping):
        payload = _stable_audit_value(dict(resolution))
    else:
        payload = resolution.canonical_content()
    if canonical_hash is not None:
        return canonical_hash(payload)
    # The fallback is only reachable during a pathological import cycle.  It
    # deliberately uses the same canonical JSON principles as reports.
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(_json_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateEligibilityContext:
    """Frozen PIT and permission context consumed by ``evaluate_candidate``.

    ``effective_date`` and ``data_cutoff`` are separate required concepts:
    the date selects market identity while the cutoff limits source
    knowledge.  ``query_boundary`` may be supplied as an additional checked
    representation, but this class never invents a cutoff or uses an
    ``as_of`` fallback.
    """

    effective_date: date | None = None
    effective_at: datetime | None = None
    data_cutoff: datetime | None = None
    data_cutoff_at: datetime | None = None
    instrument_id: UUID | None = None
    market_scope: MarketScope | None = None
    universe_query_policy: UniverseQueryPolicy | None = None
    rule_package_reference: ContractRef | None = None
    rule_exception_set_reference: ContractRef | None = None
    qualification_policy_version: ContractRef | str | None = None
    resolved_calendar_ids: tuple[str, ...] = ()
    scope_mode: InstrumentScopeMode | None = None
    required_capabilities: tuple[DataCapability, ...] = ()
    requested_window: DateRange | None = None
    query_boundary: QueryBoundary | None = None
    universe_scope_snapshot_hash: str | None = None
    fixed_authorized_instrument_ids: tuple[UUID, ...] = ()
    frozen_resolved_calendar_ids: tuple[str, ...] = ()
    scope_snapshot_hash: str | None = None
    exception_set_reference: ContractRef | None = None
    provider_capability_summary: Mapping[str, object] = dc_field(default_factory=dict)
    # Constructor aliases keep the contract readable for callers using the
    # task-package vocabulary.  They normalize into the canonical fields.
    rule_package: ContractRef | None = None
    rule_exception_set: ContractRef | None = None
    qualification_policy: ContractRef | str | None = None
    frozen_calendar_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.effective_date is None:
            if self.effective_at is not None:
                effective_at = _aware_datetime(self.effective_at, "effective_at")
                object.__setattr__(self, "effective_date", effective_at.date())
            else:
                raise InvalidDataRequestError("effective_date is required")
        object.__setattr__(self, "effective_date", _plain_date(self.effective_date, "effective_date"))
        if self.effective_at is not None:
            effective_at = _aware_datetime(self.effective_at, "effective_at")
            if effective_at.date() != self.effective_date:
                raise UniversePitBoundaryViolationError(
                    "effective_at and effective_date disagree",
                    details={
                        "effective_at": effective_at.isoformat(),
                        "effective_date": self.effective_date.isoformat(),
                    },
                )
            object.__setattr__(self, "effective_at", effective_at)
        boundary = self.query_boundary
        if boundary is not None and not isinstance(boundary, QueryBoundary):
            raise InvalidDataRequestError("query_boundary must be a QueryBoundary")
        supplied_cutoff = self.data_cutoff or self.data_cutoff_at
        if self.data_cutoff is not None and self.data_cutoff_at is not None:
            if self.data_cutoff != self.data_cutoff_at:
                raise UniversePitBoundaryViolationError(
                    "data_cutoff and data_cutoff_at disagree"
                )
        if supplied_cutoff is None:
            if boundary is None:
                raise InvalidDataRequestError("data_cutoff is required")
            cutoff = boundary.data_cutoff
        else:
            cutoff = _aware_datetime(supplied_cutoff, "data_cutoff")
        if boundary is not None and cutoff != boundary.data_cutoff:
            raise UniversePitBoundaryViolationError(
                "data_cutoff must equal query_boundary.data_cutoff",
                details={
                    "data_cutoff": cutoff.isoformat(),
                    "boundary_data_cutoff": boundary.data_cutoff.isoformat(),
                },
            )
        object.__setattr__(self, "data_cutoff", cutoff)
        object.__setattr__(self, "data_cutoff_at", cutoff)
        if boundary is not None:
            try:
                boundary.require_not_past_cutoff(self.effective_date, "effective_date")
            except Exception as exc:
                raise UniversePitBoundaryViolationError(
                    "effective_date is outside query_boundary visibility",
                    details={
                        "effective_date": self.effective_date.isoformat(),
                        "data_cutoff": cutoff.isoformat(),
                        "cause_code": getattr(exc, "code", type(exc).__name__),
                    },
                ) from exc
        if self.requested_window is not None and not isinstance(self.requested_window, DateRange):
            raise InvalidDataRequestError("requested_window must be a DateRange")
        if self.requested_window is not None and self.effective_date > self.requested_window.end_date:
            raise UniversePitBoundaryViolationError(
                "effective_date must stay inside requested_window",
                details={
                    "effective_date": self.effective_date.isoformat(),
                    "requested_window_end": self.requested_window.end_date.isoformat(),
                },
            )
        if self.effective_date > self.data_cutoff.date():
            raise UniversePitBoundaryViolationError(
                "effective_date is later than the data cutoff date",
                details={
                    "effective_date": self.effective_date.isoformat(),
                    "data_cutoff": self.data_cutoff.isoformat(),
                },
            )
        package = self.rule_package_reference or self.rule_package
        if package is not None and not isinstance(package, ContractRef):
            raise InvalidDataRequestError("rule_package_reference must be a ContractRef")
        if self.rule_package_reference is not None and self.rule_package is not None and self.rule_package_reference != self.rule_package:
            raise InvalidDataRequestError("rule_package and rule_package_reference disagree")
        object.__setattr__(self, "rule_package_reference", package)
        object.__setattr__(self, "rule_package", package)
        exception = self.rule_exception_set_reference or self.rule_exception_set
        if exception is not None and not isinstance(exception, ContractRef):
            raise InvalidDataRequestError("rule_exception_set_reference must be a ContractRef")
        if self.rule_exception_set_reference is not None and self.rule_exception_set is not None and self.rule_exception_set_reference != self.rule_exception_set:
            raise InvalidDataRequestError("rule_exception_set and rule_exception_set_reference disagree")
        object.__setattr__(self, "rule_exception_set_reference", exception)
        object.__setattr__(self, "rule_exception_set", exception)
        qualification = self.qualification_policy_version or self.qualification_policy
        if qualification is not None and not isinstance(qualification, (ContractRef, str)):
            raise InvalidDataRequestError("qualification_policy_version must be a ContractRef or text")
        if isinstance(qualification, str):
            qualification = _non_blank(qualification, "qualification_policy_version")
        if self.qualification_policy_version is not None and self.qualification_policy is not None and self.qualification_policy_version != self.qualification_policy:
            raise InvalidDataRequestError("qualification_policy and qualification_policy_version disagree")
        object.__setattr__(self, "qualification_policy_version", qualification)
        object.__setattr__(self, "qualification_policy", qualification)
        if self.market_scope is None:
            object.__setattr__(self, "market_scope", MarketScope())
        elif not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        if self.universe_query_policy is None:
            object.__setattr__(self, "universe_query_policy", UniverseQueryPolicy())
        elif not isinstance(self.universe_query_policy, UniverseQueryPolicy):
            raise InvalidDataRequestError("universe_query_policy must be a UniverseQueryPolicy")
        ids_source = (
            self.resolved_calendar_ids
            or self.frozen_calendar_ids
            or self.frozen_resolved_calendar_ids
        )
        try:
            ids = tuple(sorted({normalize_calendar_id(item) for item in ids_source}))
        except Exception as exc:
            raise InvalidDataRequestError("resolved_calendar_ids must contain canonical calendar ids") from exc
        if self.resolved_calendar_ids and self.frozen_calendar_ids and ids != tuple(sorted(set(self.frozen_calendar_ids))):
            raise InvalidDataRequestError("resolved_calendar_ids and frozen_calendar_ids disagree")
        if self.resolved_calendar_ids and self.frozen_resolved_calendar_ids and ids != tuple(sorted(set(self.frozen_resolved_calendar_ids))):
            raise InvalidDataRequestError(
                "resolved_calendar_ids and frozen_resolved_calendar_ids disagree"
            )
        object.__setattr__(self, "resolved_calendar_ids", ids)
        object.__setattr__(self, "frozen_calendar_ids", ids)
        object.__setattr__(self, "frozen_resolved_calendar_ids", ids)
        if self.scope_mode is not None:
            try:
                object.__setattr__(self, "scope_mode", InstrumentScopeMode(self.scope_mode))
            except (TypeError, ValueError) as exc:
                raise InvalidDataRequestError("scope_mode must be an InstrumentScopeMode") from exc
        capabilities = tuple(self.required_capabilities)
        if any(not isinstance(item, DataCapability) for item in capabilities):
            raise InvalidDataRequestError("required_capabilities must contain DataCapability values")
        object.__setattr__(self, "required_capabilities", tuple(sorted(set(capabilities), key=lambda item: item.value)))
        ids = tuple(self.fixed_authorized_instrument_ids)
        if any(not isinstance(item, UUID) for item in ids):
            raise InvalidDataRequestError("fixed_authorized_instrument_ids must contain UUIDs")
        object.__setattr__(self, "fixed_authorized_instrument_ids", tuple(sorted(set(ids), key=str)))
        object.__setattr__(self, "provider_capability_summary", _freeze_mapping(self.provider_capability_summary, "provider_capability_summary"))
        supplied_scope_hash = self.universe_scope_snapshot_hash or self.scope_snapshot_hash
        if self.universe_scope_snapshot_hash is not None and self.scope_snapshot_hash is not None and self.universe_scope_snapshot_hash != self.scope_snapshot_hash:
            raise InvalidDataRequestError("scope hash fields disagree")
        if supplied_scope_hash is not None:
            digest = _non_blank(supplied_scope_hash, "universe_scope_snapshot_hash")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise InvalidDataRequestError("universe_scope_snapshot_hash must be a lowercase SHA-256 digest")
            object.__setattr__(self, "universe_scope_snapshot_hash", digest)
            object.__setattr__(self, "scope_snapshot_hash", digest)
        if self.exception_set_reference is not None:
            if not isinstance(self.exception_set_reference, ContractRef):
                raise InvalidDataRequestError(
                    "exception_set_reference must be a ContractRef"
                )
            if self.rule_exception_set_reference is not None and self.exception_set_reference != self.rule_exception_set_reference:
                raise InvalidDataRequestError("exception set reference fields disagree")
            object.__setattr__(self, "exception_set_reference", self.rule_exception_set_reference)
        else:
            object.__setattr__(self, "exception_set_reference", self.rule_exception_set_reference)
        if self.instrument_id is not None and not isinstance(self.instrument_id, UUID):
            raise InvalidDataRequestError("instrument_id must be a UUID")

    @property
    def frozen_scope_calendar_ids(self) -> tuple[str, ...]:
        """Canonical read-only alias for the calendar permission set."""

        return self.resolved_calendar_ids

    @property
    def calendar_ids(self) -> tuple[str, ...]:
        """Short alias for the frozen named calendar set."""

        return self.resolved_calendar_ids

    @property
    def qualification_policy_reference(self) -> ContractRef | str | None:
        """Canonical policy alias used by provider adapters."""

        return self.qualification_policy_version

    def canonical_content(self) -> Mapping[str, object]:
        """Stable PIT context content used by qualification hashes."""

        return MappingProxyType(
            {
                "effective_date": self.effective_date,
                "data_cutoff": self.data_cutoff,
                "market_scope": {
                    "markets": self.market_scope.markets,
                    "exchanges": self.market_scope.exchanges,
                    "asset_classes": self.market_scope.asset_classes,
                    "currencies": self.market_scope.currencies,
                },
                "universe_query_policy": [
                    {"key": ref.key, "version": ref.version}
                    for ref in self.universe_query_policy.candidate_set_rules
                ],
                "rule_package_reference": _ref_payload(self.rule_package_reference),
                "rule_exception_set_reference": _ref_payload(self.rule_exception_set_reference),
                "qualification_policy_version": _ref_payload(self.qualification_policy_version),
                "resolved_calendar_ids": self.resolved_calendar_ids,
                "scope_mode": self.scope_mode.value if self.scope_mode else None,
                "required_capabilities": [item.value for item in self.required_capabilities],
                "requested_window": (
                    {"start_date": self.requested_window.start_date, "end_date": self.requested_window.end_date}
                    if self.requested_window
                    else None
                ),
                "universe_scope_snapshot_hash": self.universe_scope_snapshot_hash,
                "fixed_authorized_instrument_ids": [str(item) for item in self.fixed_authorized_instrument_ids],
                "provider_capability_summary": self.provider_capability_summary,
            }
        )


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """Small optional adapter input for pure candidate qualification tests.

    Production providers may pass their existing ``InstrumentSpec`` or
    qualification DTO directly to :func:`evaluate_candidate`; this adapter is
    useful when a provider has already projected facts into plain values.
    """

    instrument_id: UUID | None = None
    calendar_id: str | None = None
    trading_code: str | None = None
    name: str | None = None
    display_name: str | None = None
    asset_class: str | None = None
    exchange: str | None = None
    currency: str | None = None
    identity_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    mapping_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    rule_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    market_data_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    corporate_action_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    quantity_action_coverage_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    status_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    metadata: Mapping[str, object] = dc_field(default_factory=dict)
    coverage_qualification: object | None = None
    coverage_result: object | None = None
    qualification: object | None = None
    known_at: datetime | None = None
    effective_date: date | None = None
    eligible: bool | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.instrument_id is not None and not isinstance(self.instrument_id, UUID):
            raise InvalidDataRequestError("instrument_id must be a UUID")
        if self.calendar_id is not None:
            try:
                object.__setattr__(self, "calendar_id", normalize_calendar_id(self.calendar_id))
            except Exception as exc:
                raise InvalidDataRequestError("calendar_id must be canonical") from exc
        if self.known_at is not None:
            object.__setattr__(self, "known_at", _aware_datetime(self.known_at, "known_at"))
        if self.effective_date is not None:
            object.__setattr__(self, "effective_date", _plain_date(self.effective_date, "effective_date"))
        for name in (
            "identity_evidence",
            "mapping_evidence",
            "rule_evidence",
            "market_data_evidence",
            "corporate_action_evidence",
            "quantity_action_coverage_evidence",
            "status_evidence",
            "metadata",
        ):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name), name))
        object.__setattr__(self, "reason_codes", _sorted_codes(self.reason_codes))


# Descriptive aliases are intentionally one model.  They make the contract
# usable by callers that follow the task-package terminology without creating
# a parallel candidate input/result hierarchy.
InstrumentCandidateInput = CandidateInput


@dataclass(frozen=True, slots=True)
class CandidateEligibility:
    """Result of evaluating one candidate under one frozen PIT context."""

    instrument_id: UUID
    eligible: bool
    reason_codes: tuple[str, ...] = ()
    calendar_id: str | None = None
    identity_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    mapping_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    rule_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    market_data_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    corporate_action_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    quantity_action_coverage_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    status_evidence: Mapping[str, object] = dc_field(default_factory=dict)
    failed_check: str | None = None
    expected: object | None = None
    actual: object | None = None
    evidence_summary: Mapping[str, object] = dc_field(default_factory=dict)
    qualification_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise InvalidDataRequestError("instrument_id must be a UUID")
        if type(self.eligible) is not bool:
            raise InvalidDataRequestError("eligible must be a boolean")
        object.__setattr__(self, "reason_codes", _sorted_codes(self.reason_codes))
        if self.calendar_id is not None:
            try:
                object.__setattr__(self, "calendar_id", normalize_calendar_id(self.calendar_id))
            except Exception as exc:
                raise InvalidDataRequestError("calendar_id must be canonical") from exc
        for name in (
            "identity_evidence",
            "mapping_evidence",
            "rule_evidence",
            "market_data_evidence",
            "corporate_action_evidence",
            "quantity_action_coverage_evidence",
            "status_evidence",
            "evidence_summary",
        ):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name), name))
        if self.failed_check is not None:
            object.__setattr__(self, "failed_check", _non_blank(self.failed_check, "failed_check"))
        object.__setattr__(self, "expected", _freeze_value(self.expected, "expected"))
        object.__setattr__(self, "actual", _freeze_value(self.actual, "actual"))
        # Qualification hashes intentionally omit the optional hash field,
        # display text, and any caller-supplied candidate ordering.
        object.__setattr__(self, "qualification_hash", self._compute_hash())

    @property
    def filtered(self) -> bool:
        """Alias for the candidate-level negative result."""

        return not self.eligible

    @property
    def status(self) -> str:
        """Stable compact status for API projections."""

        return "eligible" if self.eligible else "filtered"

    @property
    def result_hash(self) -> str:
        """Alias used when a caller treats qualification as a result DTO."""

        return self.qualification_hash

    def machine_content(self) -> Mapping[str, object]:
        """Return deterministic evidence used by the qualification hash."""

        return MappingProxyType(
            {
                "instrument_id": str(self.instrument_id),
                "eligible": self.eligible,
                "reason_codes": self.reason_codes,
                "calendar_id": self.calendar_id,
                "identity_evidence": _stable_audit_value(self.identity_evidence),
                "mapping_evidence": _stable_audit_value(self.mapping_evidence),
                "rule_evidence": _stable_audit_value(self.rule_evidence),
                "market_data_evidence": _stable_audit_value(self.market_data_evidence),
                "corporate_action_evidence": _stable_audit_value(self.corporate_action_evidence),
                "quantity_action_coverage_evidence": _stable_audit_value(self.quantity_action_coverage_evidence),
                "status_evidence": _stable_audit_value(self.status_evidence),
                "failed_check": self.failed_check,
                "expected": self.expected,
                "actual": self.actual,
                "evidence_summary": _stable_audit_value(self.evidence_summary),
            }
        )

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe candidate audit projection."""

        return {
            "instrument_id": str(self.instrument_id),
            "eligible": self.eligible,
            "status": self.status,
            "reason_codes": self.reason_codes,
            "calendar_id": self.calendar_id,
            "failed_check": self.failed_check,
            "expected": _json_value(self.expected),
            "actual": _json_value(self.actual),
            "evidence_summary": self.evidence_summary,
            "qualification_hash": self.qualification_hash,
        }

    def _compute_hash(self) -> str:
        if canonical_hash is not None:
            return canonical_hash(self.machine_content())
        import hashlib
        import json

        return hashlib.sha256(
            json.dumps(_json_value(self.machine_content()), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


CandidateEligibilityResult = CandidateEligibility


@dataclass(frozen=True, slots=True)
class CandidateFilterResult:
    """Deterministic aggregate returned by the pure candidate filter."""

    eligible_candidates: tuple[object, ...]
    evaluations: tuple[CandidateEligibility, ...]
    filtered_reason_counts: Mapping[str, int]
    result_hash: str

    def __post_init__(self) -> None:
        evaluations = tuple(sorted(self.evaluations, key=lambda item: str(item.instrument_id)))
        object.__setattr__(self, "evaluations", evaluations)
        eligible = tuple(
            candidate
            for candidate in self.eligible_candidates
        )
        object.__setattr__(self, "eligible_candidates", eligible)
        counts = Counter()
        for key, value in self.filtered_reason_counts.items():
            if type(key) is not str or not key.strip() or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidDataRequestError("filtered_reason_counts must map codes to non-negative integers")
            counts[key] = value
        normalized_counts = MappingProxyType(dict(sorted(counts.items())))
        object.__setattr__(self, "filtered_reason_counts", normalized_counts)
        payload = {
            "evaluations": [item.machine_content() for item in evaluations],
            "filtered_reason_counts": normalized_counts,
        }
        object.__setattr__(self, "result_hash", canonical_hash(payload) if canonical_hash else "")

    @property
    def candidates(self) -> tuple[object, ...]:
        """Short alias for the eligible candidate tuple."""

        return self.eligible_candidates

    def as_dict(self) -> dict[str, object]:
        """Return candidate counts and per-candidate audit evidence."""

        return {
            "candidate_count": len(self.eligible_candidates),
            "filtered_reason_counts": self.filtered_reason_counts,
            "eligible_instrument_ids": [
                str(item.instrument_id)
                for item in self.evaluations
                if item.eligible
            ],
            "evaluations": [item.as_dict() for item in self.evaluations],
            "result_hash": self.result_hash,
        }


def merge_calendar_ids(*sources: Iterable[str]) -> tuple[str, ...]:
    """Merge only explicitly resolved named calendars.

    No exchange, code prefix, asset class, or default calendar is ever
    inferred here.  Invalid IDs fail at the boundary instead of being
    silently normalized into a guessed market.
    """

    values: set[str] = set()
    for source in sources:
        if isinstance(source, (str, bytes)):
            raise InvalidDataRequestError("calendar id sources must be iterables")
        try:
            values.update(normalize_calendar_id(item) for item in source)
        except Exception as exc:
            raise InvalidDataRequestError("calendar id sources contain an invalid id") from exc
    return tuple(sorted(values))


def _candidate_value(candidate: object, name: str, default: object = None) -> object:
    """Read one value from a mapping or a plain provider DTO."""

    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _nested_value(candidate: object, spec: object | None, name: str, default: object = None) -> object:
    """Prefer the candidate projection, then its complete underlying spec."""

    value = _candidate_value(candidate, name, None)
    if value is not None:
        return value
    return _candidate_value(spec, name, default) if spec is not None else default


def _mapping_value(mapping: object, *names: str, default: object = None) -> object:
    """Read common status spellings from JSON evidence."""

    if not isinstance(mapping, Mapping):
        return default
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _as_evidence_mapping(value: object) -> Mapping[str, object]:
    """Project a small qualification outcome onto safe evidence fields."""

    if isinstance(value, Mapping):
        return value
    if value is None:
        return {}
    projected: dict[str, object] = {}
    for name in (
        "complete",
        "covered",
        "available",
        "quality_status",
        "status",
        "eligible",
        "valid",
        "known_at",
        "qualification_hash",
        "reason_codes",
    ):
        if hasattr(value, name):
            projected[name] = getattr(value, name)
    return projected


def _evidence_failed(evidence: object) -> bool:
    """Interpret explicit negative evidence without treating an empty table as proof.

    In particular, a missing/empty corporate-action mapping does not imply
    that no action exists.  Only explicit ``complete=False``, an invalid or
    unavailable quality status, or an explicit ineligible status filters a
    candidate.
    """

    evidence = _as_evidence_mapping(evidence)
    if not evidence:
        return False
    complete = _mapping_value(evidence, "complete", "covered", "available")
    if complete is False:
        return True
    status = _mapping_value(evidence, "quality_status", "status")
    if isinstance(status, StrEnum):
        status = status.value
    if isinstance(status, str) and status.strip().lower() in {
        "partial",
        "invalid",
        "unavailable",
        "blocked",
        "ineligible",
        "unknown",
    }:
        return True
    for name in ("eligible", "valid"):
        value = _mapping_value(evidence, name)
        if value is False:
            return True
    return False


def _evidence_complete(evidence: object) -> bool:
    """Return whether evidence explicitly proves a required dimension.

    ``{}``, ``None`` and a mapping containing only descriptive fields are not
    a negative fact, but they are also not a positive qualification proof.
    This distinction lets optional dimensions remain neutral while making a
    required formal/dynamic capability fail closed.
    """

    normalized = _as_evidence_mapping(evidence)
    if not normalized or _evidence_failed(normalized):
        return False
    for name in ("complete", "covered", "available", "valid", "eligible"):
        value = _mapping_value(normalized, name)
        if value is True:
            return True
        if value is False:
            return False
    status = _mapping_value(normalized, "quality_status", "status")
    if isinstance(status, StrEnum):
        status = status.value
    if isinstance(status, str) and status.strip().lower() in {
        "complete",
        "covered",
        "available",
        "valid",
        "eligible",
        "ready",
        "ok",
        "not_applicable",
    }:
        return True
    return False


def _issue_code(issue: object) -> str | None:
    """Project a provider issue to its stable code without serializing it."""

    if isinstance(issue, Mapping):
        value = issue.get("code")
    else:
        value = getattr(issue, "code", None)
    if isinstance(value, StrEnum):
        value = value.value
    return value.strip() if isinstance(value, str) and value.strip() else None


def _existing_reason_codes(candidate: object) -> tuple[str, ...]:
    """Collect explicit qualification issue codes from a provider result."""

    values: list[object] = []
    values.extend(_candidate_value(candidate, "reason_codes", ()) or ())
    issues = _candidate_value(candidate, "issues", ()) or ()
    for issue in issues:
        code = _issue_code(issue)
        if code:
            values.append(code)
    # CoverageQualificationPort results are deliberately consumed as a
    # result object, not reinterpreted by querying Bars or coverage stores.
    for name in ("coverage_qualification", "coverage_result", "qualification"):
        nested = _candidate_value(candidate, name, None)
        if nested is None or nested is candidate:
            continue
        nested_codes = _candidate_value(nested, "reason_codes", ()) or ()
        values.extend(nested_codes)
        for issue in _candidate_value(nested, "issues", ()) or ():
            code = _issue_code(issue)
            if code:
                values.append(code)
    return _sorted_codes(values)


def _classify_issue(code: str) -> str:
    """Map upstream reason vocabulary to stable candidate categories."""

    lowered = code.lower()
    if "calendar" in lowered:
        return CANDIDATE_CALENDAR_MISSING
    if lowered.startswith("identity"):
        return CANDIDATE_IDENTITY_INCOMPLETE
    if "mapping" in lowered or "display" in lowered:
        return CANDIDATE_MAPPING_INCOMPLETE
    if lowered.startswith("rule") or lowered.startswith("rules"):
        return CANDIDATE_RULE_INCOMPLETE
    if "corporate" in lowered or "action" in lowered:
        return CANDIDATE_CORPORATE_ACTION_INCOMPLETE
    if "status" in lowered:
        return CANDIDATE_STATUS_INCOMPLETE
    if "bar" in lowered or "history" in lowered or "coverage" in lowered:
        return CANDIDATE_MARKET_DATA_INCOMPLETE
    return code


def _candidate_evidence(candidate: object, spec: object | None, name: str) -> Mapping[str, object]:
    """Collect one evidence mapping from candidate and optional spec."""

    value = _candidate_value(candidate, name, None)
    if value is None and spec is not None:
        value = _candidate_value(spec, name, None)
    if value is None and name == "market_data_evidence":
        for nested_name in ("coverage_qualification", "coverage_result"):
            nested = _candidate_value(candidate, nested_name, None)
            if nested is not None:
                value = _candidate_value(nested, "evidence_summary", None)
                if value is not None:
                    break
    return _as_evidence_mapping(value)


def evaluate_candidate(
    candidate: object,
    context: CandidateEligibilityContext,
) -> CandidateEligibility:
    """Evaluate one candidate in deterministic PIT qualification order.

    This function is intentionally pure: it performs no I/O and has no
    strategy callback.  Providers should resolve identity/spec facts before
    calling it.  A malformed provider row raises the provider-contract error;
    a well-formed candidate with missing facts returns a filtered result.
    """

    if not isinstance(context, CandidateEligibilityContext):
        raise InvalidDataRequestError("context must be a CandidateEligibilityContext")
    spec = _candidate_value(candidate, "spec", None)
    instrument_id = _nested_value(candidate, spec, "instrument_id", None)
    if not isinstance(instrument_id, UUID):
        raise UniverseProviderContractViolationError(
            "universe provider candidate must carry a UUID instrument_id",
            details={"reason_code": CANDIDATE_IDENTITY_INCOMPLETE},
        )

    calendar_id = _nested_value(candidate, spec, "calendar_id", None)
    if calendar_id is None:
        identity = _candidate_evidence(candidate, spec, "identity_evidence")
        calendar_id = _mapping_value(identity, "calendar_id")
    if calendar_id is not None:
        try:
            calendar_id = normalize_calendar_id(calendar_id)
        except Exception:
            calendar_id = None

    evidence_names = (
        "identity_evidence",
        "mapping_evidence",
        "rule_evidence",
        "market_data_evidence",
        "corporate_action_evidence",
        "quantity_action_coverage_evidence",
        "status_evidence",
    )
    evidence = {
        name: _candidate_evidence(candidate, spec, name) for name in evidence_names
    }
    reasons: list[str] = []
    existing_codes = _existing_reason_codes(candidate)
    # Upstream qualifications already expose stable machine codes.  Preserve
    # those codes verbatim for audit/counting instead of collapsing them into
    # a second vocabulary; task-15 adds its own stable codes only for checks
    # performed at this boundary.
    reasons.extend(existing_codes)

    # 1. PIT identity: calendar and core identity fields must come from a
    # resolved fact.  No source code or current catalogue fallback is used.
    if calendar_id is None:
        reasons.append(CANDIDATE_CALENDAR_MISSING)
    identity_status = _candidate_value(candidate, "identity_status", None)
    if identity_status is None and spec is not None:
        identity_status = _candidate_value(spec, "identity_status", None)
    if isinstance(identity_status, StrEnum):
        identity_status = identity_status.value
    if isinstance(identity_status, str) and identity_status.lower() in {"blocked", "missing", "invalid"}:
        reasons.append(CANDIDATE_IDENTITY_INCOMPLETE)
    if _evidence_failed(evidence["identity_evidence"]):
        reasons.append(CANDIDATE_IDENTITY_INCOMPLETE)

    # 2. PIT mapping/display: values are optional only when the upstream
    # qualification object explicitly proves they are not required.  Blank
    # values, when present, are always a candidate-level failure.
    display = _candidate_value(spec, "display", None)
    for name in ("trading_code", "name", "display_name"):
        value = _candidate_value(candidate, name, None)
        if value is None and display is not None:
            value = _candidate_value(display, name, None)
        # A candidate crossing the strategy boundary must have all three PIT
        # display fields.  Missing labels are not repaired from today's
        # catalogue; they are a candidate-level mapping failure.
        if not isinstance(value, str) or not value.strip():
            reasons.append(CANDIDATE_MAPPING_INCOMPLETE)
    mapping_status = _candidate_value(candidate, "mapping_status", None)
    if mapping_status is None and spec is not None:
        mapping_status = _candidate_value(spec, "mapping_status", None)
    if isinstance(mapping_status, StrEnum):
        mapping_status = mapping_status.value
    if isinstance(mapping_status, str) and mapping_status.lower() in {"blocked", "missing", "invalid"}:
        reasons.append(CANDIDATE_MAPPING_INCOMPLETE)
    if _evidence_failed(evidence["mapping_evidence"]):
        reasons.append(CANDIDATE_MAPPING_INCOMPLETE)

    # 3. Frozen market scope.  Empty axes mean no restriction on that axis;
    # a non-empty axis requires an explicit PIT field on the candidate/spec.
    scope = context.market_scope
    market = _nested_value(candidate, spec, "market", None)
    asset_class = _nested_value(candidate, spec, "asset_class", None)
    exchange = _nested_value(candidate, spec, "exchange", None)
    currency = _nested_value(candidate, spec, "currency", None)
    if scope.markets and (not isinstance(market, str) or market not in scope.markets):
        reasons.append(CANDIDATE_OUTSIDE_MARKET_SCOPE)
    if scope.asset_classes and (not isinstance(asset_class, str) or asset_class not in scope.asset_classes):
        reasons.append(CANDIDATE_OUTSIDE_MARKET_SCOPE)
    if scope.exchanges and (not isinstance(exchange, str) or exchange not in scope.exchanges):
        reasons.append(CANDIDATE_OUTSIDE_MARKET_SCOPE)
    if scope.currencies and (not isinstance(currency, str) or currency not in scope.currencies):
        reasons.append(CANDIDATE_OUTSIDE_MARKET_SCOPE)

    # 4. Calendar permission: a candidate cannot introduce a calendar after
    # the run's axis has been frozen.
    if calendar_id is not None and calendar_id not in context.resolved_calendar_ids:
        reasons.append(CANDIDATE_CALENDAR_NOT_PREFLIGHTED)

    # 5-8. Existing qualification and coverage ports are represented as
    # evidence.  We interpret explicit failures only for optional dimensions,
    # while a dimension required by this request must carry an explicit
    # positive proof.  An empty corporate-action mapping is therefore neutral
    # when actions are optional, but cannot admit a dynamic candidate whose
    # request requires action qualification.
    rules_status = _candidate_value(candidate, "rule_status", None)
    if isinstance(rules_status, StrEnum):
        rules_status = rules_status.value
    if isinstance(rules_status, str) and rules_status.lower() in {"blocked", "missing", "invalid", "incomplete"}:
        reasons.append(CANDIDATE_RULE_INCOMPLETE)
    if _evidence_failed(evidence["rule_evidence"]):
        reasons.append(CANDIDATE_RULE_INCOMPLETE)
    if _evidence_failed(evidence["market_data_evidence"]):
        reasons.append(CANDIDATE_MARKET_DATA_INCOMPLETE)
    if _evidence_failed(evidence["corporate_action_evidence"]):
        reasons.append(CANDIDATE_CORPORATE_ACTION_INCOMPLETE)
    if _evidence_failed(evidence["quantity_action_coverage_evidence"]):
        reasons.append(CANDIDATE_QUANTITY_ACTION_COVERAGE_INCOMPLETE)
    status_required = DataCapability.STATUS in context.required_capabilities
    if status_required and _evidence_failed(evidence["status_evidence"]):
        reasons.append(CANDIDATE_STATUS_INCOMPLETE)
    elif _evidence_failed(evidence["status_evidence"]):
        # An explicitly bad status fact is still a qualification failure even
        # when the run's rule package says the dimension is optional.
        reasons.append(CANDIDATE_STATUS_INCOMPLETE)

    dynamic_scope = context.scope_mode in (
        InstrumentScopeMode.DYNAMIC,
        InstrumentScopeMode.HYBRID,
    ) or bool(
        context.universe_query_policy is not None
        and context.universe_query_policy.has_candidate_rules
    )
    # Dynamic candidates must be backed by the upstream PIT qualification
    # port for identity/mapping/rules.  No current catalogue fallback can
    # turn an empty evidence object into a ready candidate.
    required_evidence: list[tuple[str, str]] = []
    if dynamic_scope:
        required_evidence.extend(
            (
                ("identity_evidence", CANDIDATE_IDENTITY_INCOMPLETE),
                ("mapping_evidence", CANDIDATE_MAPPING_INCOMPLETE),
                ("rule_evidence", CANDIDATE_RULE_INCOMPLETE),
            )
        )
    if DataCapability.MAPPINGS in context.required_capabilities:
        required_evidence.append(("mapping_evidence", CANDIDATE_MAPPING_INCOMPLETE))
    if DataCapability.RULES in context.required_capabilities:
        required_evidence.append(("rule_evidence", CANDIDATE_RULE_INCOMPLETE))
    if DataCapability.BARS in context.required_capabilities or DataCapability.COVERAGE in context.required_capabilities:
        required_evidence.append(("market_data_evidence", CANDIDATE_MARKET_DATA_INCOMPLETE))
    if DataCapability.ACTIONS in context.required_capabilities:
        required_evidence.append(("corporate_action_evidence", CANDIDATE_CORPORATE_ACTION_INCOMPLETE))
    if status_required:
        required_evidence.append(("status_evidence", CANDIDATE_STATUS_INCOMPLETE))
    for evidence_name, reason_code in required_evidence:
        if not _evidence_complete(evidence[evidence_name]):
            reasons.append(reason_code)

    # 9. Known-at and effective-date checks.  These compare explicit provider
    # evidence with the two PIT boundaries; they never derive a date from now.
    known_at = _candidate_value(candidate, "known_at", None)
    if known_at is None:
        known_at = _mapping_value(evidence["identity_evidence"], "known_at")
    if known_at is not None:
        try:
            known_at = _evidence_datetime(known_at, "candidate.known_at")
            if known_at > context.data_cutoff:
                reasons.append(CANDIDATE_PIT_BOUNDARY_VIOLATION)
        except InvalidDataRequestError:
            reasons.append(CANDIDATE_PIT_BOUNDARY_VIOLATION)
    candidate_effective = _candidate_value(candidate, "effective_date", None)
    if candidate_effective is None:
        candidate_effective = _candidate_value(spec, "effective_date", None) if spec is not None else None
    if candidate_effective is not None:
        try:
            candidate_effective = _plain_date(candidate_effective, "candidate.effective_date")
            if candidate_effective != context.effective_date:
                reasons.append(CANDIDATE_PIT_BOUNDARY_VIOLATION)
        except InvalidDataRequestError:
            reasons.append(CANDIDATE_PIT_BOUNDARY_VIOLATION)

    explicit_eligible = _candidate_value(candidate, "eligible", None)
    if explicit_eligible is None:
        explicit_eligible = _candidate_value(candidate, "ready", None)
    if explicit_eligible is False and not existing_codes:
        reasons.append(CANDIDATE_QUALIFICATION_UNAVAILABLE)

    # Keep only stable categories and preserve evidence for audit.  A caller
    # may inspect all original reason codes through ``evidence_summary``.
    reason_codes = _sorted_codes(reasons)
    summary = {
        "effective_date": context.effective_date,
        "data_cutoff": context.data_cutoff,
        "market_scope": {
            "markets": scope.markets,
            "exchanges": scope.exchanges,
            "asset_classes": scope.asset_classes,
            "currencies": scope.currencies,
        },
        "resolved_calendar_ids": context.resolved_calendar_ids,
        "upstream_reason_codes": existing_codes,
    }
    return CandidateEligibility(
        instrument_id=instrument_id,
        eligible=not reason_codes,
        reason_codes=reason_codes,
        calendar_id=calendar_id,
        identity_evidence=evidence["identity_evidence"],
        mapping_evidence=evidence["mapping_evidence"],
        rule_evidence=evidence["rule_evidence"],
        market_data_evidence=evidence["market_data_evidence"],
        corporate_action_evidence=evidence["corporate_action_evidence"],
        quantity_action_coverage_evidence=evidence["quantity_action_coverage_evidence"],
        status_evidence=evidence["status_evidence"],
        failed_check=reason_codes[0] if reason_codes else None,
        expected=(context.resolved_calendar_ids if CANDIDATE_CALENDAR_NOT_PREFLIGHTED in reason_codes else None),
        actual=calendar_id if CANDIDATE_CALENDAR_NOT_PREFLIGHTED in reason_codes else None,
        evidence_summary=summary,
    )


def filter_candidates(
    candidates: Sequence[object],
    context: CandidateEligibilityContext,
) -> CandidateFilterResult:
    """Purely filter a candidate sequence and aggregate stable reason counts."""

    evaluations = tuple(evaluate_candidate(candidate, context) for candidate in candidates)
    # Duplicate stable identities are a provider contract error rather than a
    # candidate-level ineligibility; choosing the first row would make result
    # order observable and could hide a code-change identity collision.
    ids = [evaluation.instrument_id for evaluation in evaluations]
    if len(ids) != len(set(ids)):
        raise UniverseProviderContractViolationError(
            "universe provider returned duplicate instrument_id values",
            details={"instrument_ids": [str(item) for item in sorted(set(ids), key=str)]},
        )
    by_id = sorted(zip(evaluations, candidates), key=lambda pair: str(pair[0].instrument_id))
    eligible = tuple(candidate for evaluation, candidate in by_id if evaluation.eligible)
    counts: Counter[str] = Counter()
    for evaluation in evaluations:
        if not evaluation.eligible:
            counts.update(evaluation.reason_codes)
    result = CandidateFilterResult(
        eligible_candidates=eligible,
        evaluations=tuple(evaluation for evaluation, _ in by_id),
        filtered_reason_counts=counts,
        result_hash="",
    )
    return result


@dataclass(frozen=True, slots=True)
class UniversePreflightReport:
    """Aggregate fixed/dynamic/hybrid preflight evidence.

    This is an in-memory composition result.  Persistence adapters should
    project it into the existing ``DataPreflightReport`` JSON fields instead
    of creating a candidate-specific table.
    """

    status: UniverseScopeStatus
    scope_mode: InstrumentScopeMode
    fixed_instrument_ids: tuple[UUID, ...]
    resolved_calendar_ids: tuple[str, ...]
    scope_resolution: UniverseScopeResolution | None = None
    fixed_preflight_report: object | None = None
    filtered_reason_counts: Mapping[str, int] = dc_field(default_factory=dict)
    candidate_count: int = 0
    issues: tuple[UniverseScopeIssue, ...] = ()
    scope_snapshot_hash: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "status", UniverseScopeStatus(self.status))
            object.__setattr__(self, "scope_mode", InstrumentScopeMode(self.scope_mode))
        except (TypeError, ValueError) as exc:
            raise InvalidDataRequestError("invalid universe preflight status or scope mode") from exc
        ids = tuple(self.fixed_instrument_ids)
        if any(not isinstance(item, UUID) for item in ids):
            raise InvalidDataRequestError("fixed_instrument_ids must contain UUIDs")
        object.__setattr__(self, "fixed_instrument_ids", tuple(sorted(set(ids), key=str)))
        object.__setattr__(self, "resolved_calendar_ids", merge_calendar_ids(self.resolved_calendar_ids))
        if isinstance(self.candidate_count, bool) or not isinstance(self.candidate_count, int) or self.candidate_count < 0:
            raise InvalidDataRequestError("candidate_count must be a non-negative integer")
        counts: Counter[str] = Counter()
        for code, value in self.filtered_reason_counts.items():
            if not isinstance(code, str) or not code.strip() or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidDataRequestError("filtered_reason_counts must map codes to non-negative integers")
            counts[code.strip()] = value
        object.__setattr__(self, "filtered_reason_counts", MappingProxyType(dict(sorted(counts.items()))))
        issues = tuple(self.issues)
        if any(not isinstance(issue, UniverseScopeIssue) for issue in issues):
            raise InvalidDataRequestError("issues must contain UniverseScopeIssue instances")
        object.__setattr__(self, "issues", tuple(sorted(issues, key=lambda item: (item.code, item.field or ""))))
        source_hash = self.scope_resolution.snapshot_hash if self.scope_resolution is not None else ""
        if (
            self.status is UniverseScopeStatus.READY
            and self.scope_mode in (InstrumentScopeMode.DYNAMIC, InstrumentScopeMode.HYBRID)
            and self.scope_resolution is None
        ):
            raise InvalidDataRequestError(
                "a ready dynamic or hybrid preflight requires scope resolution evidence"
            )
        if (
            self.scope_resolution is not None
            and self.scope_resolution.blocked
            and self.status is UniverseScopeStatus.READY
        ):
            raise InvalidDataRequestError(
                "a blocked scope resolution cannot produce a ready preflight report"
            )
        if self.scope_snapshot_hash:
            supplied_hash = _non_blank(self.scope_snapshot_hash, "scope_snapshot_hash")
            if len(supplied_hash) != 64 or any(
                char not in "0123456789abcdef" for char in supplied_hash
            ):
                raise InvalidDataRequestError(
                    "scope_snapshot_hash must be a lowercase SHA-256 digest"
                )
            if source_hash and source_hash != supplied_hash:
                raise InvalidDataRequestError(
                    "scope_snapshot_hash does not match scope_resolution"
                )
        object.__setattr__(self, "scope_snapshot_hash", source_hash or self.scope_snapshot_hash)

    @property
    def blocked(self) -> bool:
        return self.status is UniverseScopeStatus.BLOCKED

    @property
    def ready(self) -> bool:
        return self.status is UniverseScopeStatus.READY

    @property
    def universe_eligibility_summary(self) -> Mapping[str, object]:
        """Projection consumed by the existing admission report."""

        return MappingProxyType(
            {
                "scope_mode": self.scope_mode.value,
                "market_scope": (
                    {
                        "markets": self.scope_resolution.market_scope.markets,
                        "exchanges": self.scope_resolution.market_scope.exchanges,
                        "asset_classes": self.scope_resolution.market_scope.asset_classes,
                        "currencies": self.scope_resolution.market_scope.currencies,
                    }
                    if self.scope_resolution is not None
                    and self.scope_resolution.market_scope is not None
                    else None
                ),
                "universe_query_policy": (
                    [
                        {"key": item.key, "version": item.version}
                        for item in self.scope_resolution.universe_query_policy.candidate_set_rules
                    ]
                    if self.scope_resolution is not None
                    and self.scope_resolution.universe_query_policy is not None
                    else []
                ),
                "qualification_policy_version": (
                    _ref_payload(self.scope_resolution.qualification_policy_version)
                    if self.scope_resolution is not None
                    else None
                ),
                "resolved_calendar_ids": self.resolved_calendar_ids,
                "provider_capability_status": (
                    self.scope_resolution.capability_summary
                    if self.scope_resolution is not None
                    else {}
                ),
                "filtered_reason_counts": self.filtered_reason_counts,
                "candidate_count": self.candidate_count,
                "scope_snapshot_hash": self.scope_snapshot_hash,
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "scope_mode": self.scope_mode.value,
            "fixed_instrument_ids": [str(item) for item in self.fixed_instrument_ids],
            "resolved_calendar_ids": self.resolved_calendar_ids,
            "scope_snapshot_hash": self.scope_snapshot_hash,
            "candidate_count": self.candidate_count,
            "filtered_reason_counts": self.filtered_reason_counts,
            "issues": [issue.as_dict() for issue in self.issues],
            "scope_resolution": self.scope_resolution.as_dict() if self.scope_resolution else None,
        }

    as_summary = as_dict


def scope_issue(
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: Mapping[str, object] | None = None,
    severity: str = "error",
) -> UniverseScopeIssue:
    """Build a normalized scope issue for preflight adapters."""

    return UniverseScopeIssue(
        code=code,
        message=message,
        field=field,
        details=details or {},
        severity=severity,
    )


def build_universe_eligibility_summary(
    resolution: UniverseScopeResolution | None,
    *,
    candidate_count: int = 0,
    filtered_reason_counts: Mapping[str, int] | None = None,
    target_ids: Iterable[UUID] = (),
    final_rechecks: Sequence[Mapping[str, object]] = (),
) -> Mapping[str, object]:
    """Build the existing preflight report's minimal universe summary.

    The summary is audit data, not a second persistence model.  Candidate
    order and display messages are retained only in the caller's decision
    JSON; the frozen scope hash comes from ``resolution``.
    """

    if resolution is not None and not isinstance(resolution, UniverseScopeResolution):
        raise InvalidDataRequestError("resolution must be a UniverseScopeResolution")
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count < 0:
        raise InvalidDataRequestError("candidate_count must be a non-negative integer")
    counts: dict[str, int] = {}
    for code, value in (filtered_reason_counts or {}).items():
        if type(code) is not str or not code.strip() or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidDataRequestError("filtered_reason_counts must map codes to non-negative integers")
        counts[code.strip()] = value
    normalized_targets = tuple(target_ids)
    if any(not isinstance(item, UUID) for item in normalized_targets):
        raise InvalidDataRequestError("target_ids must contain UUID values")
    if any(not isinstance(item, Mapping) for item in final_rechecks):
        raise InvalidDataRequestError("final_rechecks must contain mappings")
    return _freeze_mapping(
        {
            "scope_mode": (
                resolution.scope_mode.value
                if resolution is not None and resolution.scope_mode is not None
                else None
            ),
            "market_scope": (
                {
                    "markets": resolution.market_scope.markets,
                    "exchanges": resolution.market_scope.exchanges,
                    "asset_classes": resolution.market_scope.asset_classes,
                    "currencies": resolution.market_scope.currencies,
                }
                if resolution is not None and resolution.market_scope is not None
                else None
            ),
            "universe_query_policy": (
                [
                    {"key": item.key, "version": item.version}
                    for item in resolution.universe_query_policy.candidate_set_rules
                ]
                if resolution is not None and resolution.universe_query_policy is not None
                else []
            ),
            "qualification_policy_version": (
                _ref_payload(resolution.qualification_policy_version)
                if resolution is not None
                else None
            ),
            "resolved_calendar_ids": resolution.resolved_calendar_ids if resolution else (),
            "provider_capability_status": resolution.capability_summary if resolution else {},
            "candidate_count": candidate_count,
            "filtered_reason_counts": dict(sorted(counts.items())),
            "target_ids": [str(item) for item in sorted(set(normalized_targets), key=str)],
            "final_rechecks": list(final_rechecks),
            "scope_snapshot_hash": resolution.snapshot_hash if resolution else None,
        },
        "universe_eligibility_summary",
    )
