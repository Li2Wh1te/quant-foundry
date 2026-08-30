"""Authoritative data-session runtime: state machine and preflight flow.

One :class:`AuthoritativeDataSession` binds a frozen :class:`DataRequest`
to calendar resources and runs the single authoritative preflight that
freezes the formal session tuple and the warmup session tuple:

    created ── preflight() ──┬── blocked ──> closed
                             └── ready ────> closed

Formal sessions come exclusively from the ``strict_compatible@1`` calendar
axis over the request window; warmup sessions are resolved from the first
formal session backwards with the same frozen calendar set and the same
policy.  Warmup sessions never enter the formal timeline and never produce
any backtest business record.

The session never loads or calls a strategy; an optional ``on_ready``
callback only notifies the owning engine after a successful preflight.
This module is deliberately free of ORM, database session, FastAPI, and
Tushare imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from enum import StrEnum
from types import MappingProxyType
from typing import Callable, Mapping, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import (
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
    CalendarAxisDataProvider,
    CalendarAxisResolution,
    CalendarAxisStatus,
    CalendarPITContext,
    CalendarSnapshot,
    CalendarSnapshotRequest,
    CalendarDefinition,
    CapabilityApplicability,
    CapabilityResolution,
    CapabilityValue,
    CAPABILITY_OPENING_AVAILABILITY,
    CAPABILITY_PRICE_LIMIT_TRADABILITY,
    CAPABILITY_SUSPENSION,
    InMemoryCalendarAxisDataProvider,
    SessionPoint,
    resolve_calendar_axis,
    calendar_snapshot_usage,
)
from app.backtesting.data.errors import (
    DataSessionClosedError,
    InvalidDataRequestError,
    UnsupportedCapabilityError,
    CalendarContractError,
    CalendarPreflightResourceLimitExceededError,
    ProviderContractViolationError,
)
from app.backtesting.data.protocols import DataConsistencyContext
from app.backtesting.data.reports import DataPreflightReport, PreflightIssue, canonical_json
from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataChunkQuery,
    DataPreflightRequest,
    DataRequest,
    DateRange,
    IssueSeverity,
    PreflightStatus,
    DataCapability,
)
from app.backtesting.data.warmup import (
    NO_FORMAL_SESSIONS,
    SCOPE_FORMAL,
    SCOPE_WARMUP,
    WarmupCoverageStatus,
    WarmupResolution,
    WarmupSessionResolver,
    WarmupStatus,
    _difference_details,
    resolve_warmup_sessions,
)

__all__ = [
    "AuthoritativeDataSession",
    "DataSessionState",
    "evaluate_calendar_capability_gate",
]


def _fixed_authorized_ids(request: object) -> frozenset[UUID]:
    """Build the run-level fixed authorization union without I/O."""

    values: set[UUID] = set()
    for field_name in (
        "static_instrument_ids",
        "mandatory_instrument_ids",
        "non_zero_initial_position_instrument_ids",
    ):
        raw = getattr(request, field_name, ()) or ()
        if isinstance(raw, Mapping):
            raw = raw.keys()
        for item in raw:
            if isinstance(item, UUID):
                values.add(item)
    positions = getattr(request, "initial_positions", None)
    if isinstance(positions, Mapping):
        for instrument_id, position in positions.items():
            if not isinstance(instrument_id, UUID):
                continue
            quantity = getattr(position, "quantity", position)
            try:
                if quantity != 0:
                    values.add(instrument_id)
            except Exception:
                continue
    return frozenset(values)


def _calendar_difference_issue_code(difference: object) -> str:
    """Map axis evidence to the second-layer report machine code.

    Timezone failures carry a dedicated domain error code so callers can
    distinguish an unsupported timezone from cross-day inconsistency and
    same-day cross-calendar mismatch.  The field fallback keeps older
    providers readable when they omit ``error_code``.
    """

    error_code = getattr(difference, "error_code", None)
    if isinstance(error_code, str) and error_code:
        explicit = {
            "calendar_timezone_inconsistent": "CALENDAR_TIMEZONE_INCONSISTENT",
            "calendar_timezone_mismatch": "CALENDAR_TIMEZONE_MISMATCH",
            "calendar_timezone_unsupported": "CALENDAR_TIMEZONE_UNSUPPORTED",
        }.get(error_code)
        if explicit is not None:
            return explicit
    field = getattr(difference, "field", difference)
    value = getattr(field, "value", str(field))
    return {
        "missing_fact": "CALENDAR_FACT_MISSING",
        "missing_definition": "CALENDAR_DEFINITION_MISSING",
        "unresolved_session": "CALENDAR_SESSION_UNRESOLVED",
        "registry": "CALENDAR_REGISTRY_REFERENCE_INVALID",
        "pit_metadata": "CALENDAR_PIT_METADATA_MISSING",
        "timezone": "CALENDAR_SESSION_INCOMPATIBLE",
    }.get(value, "CALENDAR_SESSION_INCOMPATIBLE")


def _provider_has_canonical_calendar_metadata(
    provider: object,
    calendar_ids: Sequence[str],
) -> bool:
    """Return whether the provider exposes the atomic strict-snapshot API.

    Strict-path detection is a capability check, not a metadata probe.  Calling
    ``registries()`` or ``definitions()`` here performs out-of-band reads for
    SQL providers before ``open_calendar_snapshot()`` can pin its transaction,
    breaking the fixed one-prepare/one-batch-read budget.  Only the built-in
    in-memory fixture may continue through the legacy resolver; a formal
    session rejects every other provider without the atomic entry point
    before it can issue per-day reads.
    """

    # Keep the argument for compatibility with callers that pass the resolved
    # calendar set; capability detection must not inspect that set's rows.
    del calendar_ids
    if not callable(getattr(provider, "open_calendar_snapshot", None)):
        return False

    # SQL and third-party providers declare the strict operation through the
    # atomic method itself.  Only the built-in in-memory fixture needs a
    # compatibility distinction: its immutable tuples are already materialized
    # and can be inspected without issuing a provider read.
    if not isinstance(provider, InMemoryCalendarAxisDataProvider):
        return True
    if tuple(getattr(provider, "_registries", ())):
        return True
    definitions = tuple(getattr(provider, "_definitions", ()))
    if any(
        getattr(definition, field, None) is not None
        for definition in definitions
        for field in ("valid_from", "registry_fact_id", "known_at", "source_priority_fact_id")
    ):
        return True
    facts = tuple(getattr(provider, "_facts", ()))
    return any(
        getattr(fact, field, None) is not None
        for fact in facts
        for field in ("known_at", "registry_fact_id", "definition_fact_id", "source_priority_fact_id")
    )


_CALENDAR_STATUS_CAPABILITIES = (
    CAPABILITY_SUSPENSION,
    CAPABILITY_OPENING_AVAILABILITY,
    CAPABILITY_PRICE_LIMIT_TRADABILITY,
)


def evaluate_calendar_capability_gate(
    provider: object,
    request: DataPreflightRequest,
    snapshot: CalendarSnapshot,
) -> tuple[tuple[PreflightIssue, ...], tuple[Mapping[str, object], ...]]:
    """Evaluate the v1 status-capability declaration gate once per snapshot.

    ``DataCapability.STATUS`` is a family requirement.  Its three canonical
    declarations are resolved against the same PIT context and frozen
    calendar set.  A missing declaration resolves to ``unknown`` evidence;
    it never becomes support merely because a provider manifest lists the
    broad ``status`` capability.  A declaration with missing applicability is
    a contract error.  Only ``required`` plus a non-supported value blocks a
    request; ``not_applicable`` is an explicit, independently supplied
    statement and is not inferred from ``unknown``.
    """

    status_required = DataCapability.STATUS in request.required_capabilities
    if not status_required:
        # The frozen rule snapshot is the authority for applicability.  When
        # it does not require the STATUS family, this gate must be a true
        # no-op: reading a provider manifest or resolving any status
        # declaration would manufacture irrelevant unknown evidence and could
        # let an unavailable status source block an otherwise valid request.
        return (), ()

    ids = tuple(snapshot.calendar_ids)
    # The canonical snapshot scope is the union of both fixed-id sets.  Using
    # ``or`` here would drop mandatory ids whenever static ids are present.
    instruments = tuple(
        dict.fromkeys(
            (*request.static_instrument_ids, *request.mandatory_instrument_ids)
        )
    )
    issues: list[PreflightIssue] = []
    evidence: list[Mapping[str, object]] = []
    resolver = getattr(provider, "resolve_capability", None)
    effective_day = request.requested_window.start_date
    # Resolve per participating calendar.  This preserves calendar-scope
    # specificity for multi-calendar requests while still allowing a
    # provider/rule-package fallback declaration to be selected by its
    # canonical selector.
    scopes: tuple[str | None, ...] = ids or (None,)
    instrument_scopes: tuple[object | None, ...] = instruments or (None,)
    manifest_method = getattr(provider, "capability_manifest", None)
    manifest = None
    manifest_error: Exception | None = None
    if callable(manifest_method):
        try:
            manifest = manifest_method()
        except Exception as exc:
            # A manifest is a provider trust boundary just like the resolver;
            # preserve a stable blocked issue instead of leaking its exception.
            manifest_error = exc
    if status_required and manifest_error is not None:
        issues.append(
            PreflightIssue(
                code="PROVIDER_CONTRACT_VIOLATION",
                severity=IssueSeverity.ERROR,
                scope=SCOPE_FORMAL,
                message="Provider 能力 manifest 读取失败，已阻断回测。",
                field="capability_manifest",
                details={
                    "cause_code": "provider_contract_violation",
                    "error_type": type(manifest_error).__name__,
                },
            )
        )
    if status_required and manifest is not None:
        declared_capabilities = tuple(getattr(manifest, "capabilities", ()))
        if DataCapability.STATUS not in declared_capabilities:
            issues.append(
                PreflightIssue(
                    code="UNSUPPORTED_CAPABILITY",
                    severity=IssueSeverity.ERROR,
                    scope=SCOPE_FORMAL,
                    message="Provider 能力 manifest 未声明交易状态能力，已阻断回测。",
                    field="required_capabilities",
                    details={
                        "cause_code": "rule_capability_unsupported",
                        "manifest_version": getattr(manifest, "manifest_version", None),
                        "capability": DataCapability.STATUS.value,
                    },
                )
            )
    for calendar_id in scopes:
        for instrument_id in instrument_scopes:
            for capability_key in _CALENDAR_STATUS_CAPABILITIES:
                resolution: CapabilityResolution
                resolution_error: str | None = None
                try:
                    if callable(resolver):
                        result = resolver(
                            capability_key,
                            effective_day=effective_day,
                            pit_context=snapshot.pit_context,
                            provider_key=request.provider_key,
                            package_key=request.rule_package.key,
                            package_version=request.rule_package.version,
                            calendar_id=calendar_id,
                            instrument_id=instrument_id,
                        )
                        if not isinstance(result, CapabilityResolution):
                            raise ProviderContractViolationError(
                                "calendar capability resolver returned an invalid result"
                            )
                        resolution = result
                    else:
                        resolution = CapabilityResolution(
                            capability_key,
                            CapabilityValue.UNKNOWN,
                            None,
                            None,
                            missing=True,
                        )
                except CalendarContractError as exc:
                    resolution_error = getattr(exc, "code", "provider_contract_violation")
                    resolution = CapabilityResolution(
                        capability_key,
                        CapabilityValue.UNKNOWN,
                        None,
                        None,
                        missing=True,
                    )
                    if status_required:
                        issues.append(
                            PreflightIssue(
                                code=(
                                    "CAPABILITY_DECLARATION_AMBIGUOUS"
                                    if getattr(exc, "code", "")
                                    == "capability_declaration_ambiguous"
                                    else getattr(exc, "code", "provider_contract_violation").upper()
                                ),
                                severity=IssueSeverity.ERROR,
                                scope=SCOPE_FORMAL,
                                message=f"交易状态能力声明无法确定：{capability_key}。",
                                field=f"capabilities.{capability_key}",
                                details={
                                    "cause_code": getattr(exc, "code", "provider_contract_violation"),
                                    "calendar_id": calendar_id,
                                    "instrument_id": str(instrument_id) if instrument_id is not None else None,
                                },
                            )
                        )
                except Exception as exc:
                    # A third-party resolver is a trust boundary.  Convert an
                    # unexpected implementation failure into a stable blocked
                    # issue instead of letting preflight escape with a raw
                    # provider exception.
                    resolution_error = "provider_contract_violation"
                    resolution = CapabilityResolution(
                        capability_key,
                        CapabilityValue.UNKNOWN,
                        None,
                        None,
                        missing=True,
                    )
                    if status_required:
                        issues.append(
                            PreflightIssue(
                                code="PROVIDER_CONTRACT_VIOLATION",
                                severity=IssueSeverity.ERROR,
                                scope=SCOPE_FORMAL,
                                message=f"交易状态能力声明读取失败：{capability_key}。",
                                field=f"capabilities.{capability_key}",
                                details={
                                    "cause_code": "provider_contract_violation",
                                    "calendar_id": calendar_id,
                                    "instrument_id": str(instrument_id) if instrument_id is not None else None,
                                    "error_type": type(exc).__name__,
                                },
                            )
                        )
                declaration = resolution.declaration
                evidence.append(
                    {
                        "calendar_id": calendar_id,
                        "instrument_id": str(instrument_id) if instrument_id is not None else None,
                        "capability": capability_key,
                        "value": resolution.value,
                        "applicability": resolution.applicability,
                        "specificity": resolution.specificity,
                        "missing": resolution.missing,
                        "selected_fact_id": declaration.fact_id if declaration is not None else None,
                        "fact_version": declaration.fact_version if declaration is not None else None,
                        "scope_kind": declaration.scope_kind if declaration is not None else None,
                        "scope_key": declaration.scope_key if declaration is not None else None,
                        "source": declaration.source if declaration is not None else None,
                        "source_revision": declaration.source_revision if declaration is not None else None,
                        "content_hash": declaration.content_hash if declaration is not None else None,
                    }
                )
                if not status_required:
                    continue
                applicability = resolution.applicability
                if applicability is None and resolution_error is None:
                    issues.append(
                        PreflightIssue(
                            code="CAPABILITY_DECLARATION_INVALID",
                            severity=IssueSeverity.ERROR,
                            scope=SCOPE_FORMAL,
                            message=f"交易状态能力 {capability_key} 缺少 applicability 声明。",
                            field=f"capabilities.{capability_key}.applicability",
                            details={
                                "cause_code": "capability_applicability_missing",
                                "calendar_id": calendar_id,
                                "instrument_id": str(instrument_id) if instrument_id is not None else None,
                                "value": resolution.value.value,
                            },
                        )
                    )
                elif (
                    applicability is CapabilityApplicability.REQUIRED
                    and resolution.value is not CapabilityValue.SUPPORTED
                ):
                    issues.append(
                        PreflightIssue(
                            code="UNSUPPORTED_CAPABILITY",
                            severity=IssueSeverity.ERROR,
                            scope=SCOPE_FORMAL,
                            message=f"规则要求的交易状态能力 {capability_key} 未由 Provider 明确支持。",
                            field=f"capabilities.{capability_key}",
                            details={
                                "cause_code": "rule_capability_unsupported",
                                "calendar_id": calendar_id,
                                "instrument_id": str(instrument_id) if instrument_id is not None else None,
                                "value": resolution.value.value,
                                "applicability": applicability.value,
                            },
                        )
                    )
    return tuple(issues), tuple(evidence)


def _snapshot_report(
    request: DataRequest,
    snapshot: CalendarSnapshot,
    *,
    capability_manifest_version: int,
    extra_issues: Sequence[PreflightIssue] = (),
    capability_evidence: Sequence[Mapping[str, object]] = (),
) -> DataPreflightReport:
    """Project one immutable snapshot into the canonical @2 report."""

    axis = snapshot.resolution
    issues = list(extra_issues)
    if axis.status is CalendarAxisStatus.INCOMPATIBLE:
        issues.extend(
            PreflightIssue(
                code=_calendar_difference_issue_code(difference),
                severity=IssueSeverity.ERROR,
                scope=SCOPE_FORMAL,
                message=f"{difference.date.isoformat()} 的日历会话不兼容，已阻断回测。",
                field=difference.field.value,
                date=difference.date,
                date_range=(difference.date, difference.date + timedelta(days=1)),
                calendar_id=difference.calendar_id,
                values_by_calendar=difference.values_by_calendar,
                details=difference.evidence(),
            )
            for difference in axis.differences
        )
    elif not axis.resolved_sessions:
        issues.append(
            PreflightIssue(
                code=NO_FORMAL_SESSIONS,
                severity=IssueSeverity.ERROR,
                scope=SCOPE_FORMAL,
                message="正式区间没有共同开市会话，无法启动回测。",
                field="resolved_sessions",
            )
        )
    warmup_resolution = None
    if request.warmup_sessions > 0 and axis.resolved_sessions:
        warmup = tuple(snapshot.warmup_sessions)
        if len(warmup) != request.warmup_sessions:
            issues.append(
                PreflightIssue(
                    code="WARMUP_COVERAGE_INSUFFICIENT",
                    severity=IssueSeverity.ERROR,
                    scope=SCOPE_WARMUP,
                    message=f"warmup 覆盖不足：请求 {request.warmup_sessions} 个会话，实际证明 {len(warmup)} 个。",
                    field="warmup_sessions",
                )
            )
        else:
            anchor = axis.resolved_sessions[0].session_date
            warmup_resolution = WarmupResolution(
                requested_sessions=request.warmup_sessions,
                first_formal_session=anchor,
                status=WarmupStatus.READY,
                coverage_status=WarmupCoverageStatus.PROVEN,
                resolved_sessions=warmup,
                # Keep the complete envelope used by the immutable snapshot,
                # including closed/non-session days between the earliest
                # warmup point and the formal anchor.  Trimming this to the
                # selected open dates would erase the very coverage range
                # needed to audit contiguous historical proof.
                history_window=DateRange(
                    snapshot.envelope_start,
                    anchor - timedelta(days=1),
                ),
            )
    errors = [item for item in issues if item.severity is IssueSeverity.ERROR]
    blocked = bool(errors)
    formal = () if blocked else axis.resolved_sessions
    warmup = () if blocked or warmup_resolution is None else warmup_resolution.resolved_sessions
    context = snapshot.pit_context
    usage = calendar_snapshot_usage(snapshot)
    calendar_summary = {
        "policy": {"key": axis.policy_key, "version": int(axis.policy_version)},
        "calendar_ids": axis.calendar_ids,
        "requested_window": {
            "start_date": request.requested_window.start_date,
            "end_date": request.requested_window.end_date,
        },
        "pit_context": dict(context.as_dict),
        "data_cutoff": context.as_dict["data_cutoff"],
        "cutoff_local_date": context.as_dict["cutoff_local_date"],
        "include_cutoff_day": context.as_dict["include_cutoff_day"],
        "pit_profile": context.as_dict["pit_profile"],
        "profile_version": context.as_dict["profile_version"],
        "knowledge_as_of": context.as_dict["knowledge_as_of"],
        "non_strict_pit": axis.non_strict_pit,
        "non_strict_pit_capabilities": axis.non_strict_pit_capabilities,
        "compatibility_status": axis.status.value,
        "timezone": axis.timezone,
        "coverage": dict(snapshot.coverage),
        "resolved_calendar_bindings": dict(snapshot.resolved_calendar_bindings),
        "resolved_calendar_definitions": [
            {
                "calendar_id": item.calendar_id,
                "registry_fact_id": item.registry_fact_id,
                "registry_version": item.registry_version,
                "definition_version": item.definition_version,
                "definition_fact_id": item.fact_id,
                "fact_version": item.fact_version,
                "source": item.source,
                "source_revision": item.source_revision,
            }
            for item in snapshot.resolved_calendar_definitions
        ],
        "calendar_revision_digest": snapshot.calendar_revision_digest,
        "revision_digest": snapshot.calendar_revision_digest,
        "calendar_session_signature": snapshot.calendar_session_signature,
        "warmup_session_signature": snapshot.warmup_session_signature,
        "snapshot_fingerprint": snapshot.snapshot_fingerprint,
        "differences": [difference.evidence() for difference in axis.differences],
        "envelope": {
            "start_date": snapshot.envelope_start,
            "end_date_exclusive": snapshot.envelope_end_exclusive,
        },
        "definition_usage_by_date": usage,
        "capabilities": tuple(capability_evidence),
    }
    # Canonical JSON round-tripping converts UUID/date/enums to the exact wire
    # representation before DataPreflightReport freezes the payload.
    calendar_summary = json.loads(canonical_json(calendar_summary))
    session_summary = {
        "pit_context": dict(context.as_dict),
        "formal_session_count": len(formal),
        "warmup_session_count": len(warmup),
        "formal_sessions": [
            {
                "date": point.session_date,
                "session_id": point.session_id,
                "timezone": point.timezone,
                "sessions": [window.semantic_payload() for window in point.sessions],
            }
            for point in formal
        ],
        "warmup_sessions": [
            {
                "date": point.session_date,
                "session_id": point.session_id,
                "timezone": point.timezone,
                "sessions": [window.semantic_payload() for window in point.sessions],
            }
            for point in warmup
        ],
        "calendar_session_signature": snapshot.calendar_session_signature,
        "warmup_session_signature": snapshot.warmup_session_signature,
        "snapshot_id": str(snapshot.snapshot_id),
        "snapshot_fingerprint": snapshot.snapshot_fingerprint,
    }
    # Keep nested report evidence JSON-native before DataPreflightReport
    # freezes it; dates and enums are canonicalized exactly like the calendar
    # summary above.
    session_summary = json.loads(canonical_json(session_summary))
    return DataPreflightReport(
        status=PreflightStatus.BLOCKED if blocked else PreflightStatus.READY,
        generated_at=datetime.now(context.data_cutoff.tzinfo),
        provider_key=request.provider_key,
        capability_manifest_version=capability_manifest_version,
        requested_window=request.requested_window,
        scope_mode=request.instrument_scope_mode,
        resolved_calendar_ids=axis.calendar_ids,
        resolved_calendar_definitions=snapshot.resolved_calendar_definitions,
        resolved_timezone=axis.timezone,
        calendar_axis_policy=request.calendar_axis_policy,
        calendar_compatibility_status=axis.status,
        calendar_session_signature=snapshot.calendar_session_signature if axis.status is CalendarAxisStatus.COMPATIBLE else "",
        resolved_sessions=formal,
        warmup_sessions=warmup,
        max_lookback_sessions=request.max_lookback_sessions,
        knowledge_as_of=request.query_boundary.knowledge_as_of,
        non_strict_pit_capabilities=tuple(axis.non_strict_pit_capabilities),
        consistency_mode=request.consistency_mode,
        consistency_token_capability=request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
        consistency_token_contract=request.consistency_token_contract if request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN else None,
        data_chunk_policy=request.data_chunk_policy,
        data_chunk_size_sessions=request.data_chunk_size_sessions,
        required_capabilities=request.required_capabilities,
        rule_package=request.rule_package,
        rule_exception_set=request.rule_exception_set,
        static_instrument_ids=request.static_instrument_ids,
        mandatory_instrument_ids=request.mandatory_instrument_ids,
        strategy_price_bases=request.strategy_price_bases,
        engine_price_basis=request.engine_price_basis,
        data_contract_version=request.data_contract_version,
        frequency=request.frequency,
        warmup_sessions_count=request.warmup_sessions,
        market_scope=request.market_scope,
        universe_query_policy=request.universe_query_policy,
        allowed_settlement_rule_class=request.allowed_settlement_rule_class,
        adjustment_series_policy=request.adjustment_series_policy,
        quality_mode=request.quality_mode,
        issues=tuple(issues),
        warmup_resolution=warmup_resolution if not blocked else None,
        warmup_resolution_signature=warmup_resolution.resolution_signature if warmup_resolution is not None and not blocked else None,
        calendar_axis_differences=axis.differences,
        warmup_axis_differences=(),
        query_boundary=request.query_boundary,
        hash_schema_version=2,
        pit_context=context.as_dict,
        calendar_revision_digest=snapshot.calendar_revision_digest,
        snapshot_fingerprint=snapshot.snapshot_fingerprint,
        non_strict_pit=axis.non_strict_pit,
        calendar_semantic_signature=axis.calendar_semantic_signature,
        warmup_session_signature=snapshot.warmup_session_signature,
        definition_usage_by_date=usage,
        calendar_summary=calendar_summary,
        session_summary=session_summary,
    )


def _snapshot_failure_report(
    request: DataRequest,
    *,
    capability_manifest_version: int,
    issues: Sequence[PreflightIssue],
) -> DataPreflightReport:
    """Create an evidence-only blocked report after a pre-read snapshot gate."""

    return DataPreflightReport(
        status=PreflightStatus.BLOCKED,
        generated_at=datetime.now().astimezone(),
        provider_key=request.provider_key,
        capability_manifest_version=capability_manifest_version,
        requested_window=request.requested_window,
        scope_mode=request.instrument_scope_mode,
        resolved_calendar_ids=request.resolved_calendar_ids,
        resolved_calendar_definitions=(),
        resolved_timezone=None,
        calendar_axis_policy=request.calendar_axis_policy,
        calendar_compatibility_status=CalendarAxisStatus.INCOMPATIBLE,
        calendar_session_signature="",
        resolved_sessions=(),
        warmup_sessions=(),
        max_lookback_sessions=request.max_lookback_sessions,
        knowledge_as_of=request.query_boundary.knowledge_as_of,
        non_strict_pit_capabilities=(),
        consistency_mode=request.consistency_mode,
        consistency_token_capability=False,
        consistency_token_contract=None,
        data_chunk_policy=request.data_chunk_policy,
        data_chunk_size_sessions=request.data_chunk_size_sessions,
        required_capabilities=request.required_capabilities,
        rule_package=request.rule_package,
        rule_exception_set=request.rule_exception_set,
        static_instrument_ids=request.static_instrument_ids,
        mandatory_instrument_ids=request.mandatory_instrument_ids,
        strategy_price_bases=request.strategy_price_bases,
        engine_price_basis=request.engine_price_basis,
        data_contract_version=request.data_contract_version,
        frequency=request.frequency,
        warmup_sessions_count=request.warmup_sessions,
        market_scope=request.market_scope,
        universe_query_policy=request.universe_query_policy,
        allowed_settlement_rule_class=request.allowed_settlement_rule_class,
        adjustment_series_policy=request.adjustment_series_policy,
        quality_mode=request.quality_mode,
        issues=tuple(issues),
        query_boundary=request.query_boundary,
        hash_schema_version=1,
    )


class DataSessionState(StrEnum):
    """Lifecycle state of one authoritative data session."""

    CREATED = "created"
    READY = "ready"
    BLOCKED = "blocked"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class _SessionConsistencyContext:
    """Read-only consistency face of one session (no secrets exposed)."""

    mode: ConsistencyMode
    token_contract: ContractRef | None
    context_summary: Mapping[str, object]


class AuthoritativeDataSession:
    """Concrete in-memory-friendly authoritative data session.

    The session satisfies the :class:`DataSession` runtime protocol shape
    for the parts delivered so far: lifecycle, authoritative preflight,
    formal/warmup session access, and its consistency context.  Chunk
    opening belongs to the later chunk-flow deliverable and raises
    :class:`UnsupportedCapabilityError` until then.
    """

    def __init__(
        self,
        *,
        request: DataRequest,
        calendar_provider: CalendarAxisDataProvider,
        warmup_resolver: WarmupSessionResolver | None = None,
        capability_manifest_version: int = 1,
        on_ready: Callable[["AuthoritativeDataSession"], None] | None = None,
        on_close: Callable[["AuthoritativeDataSession"], None] | None = None,
        preflight_service: object | None = None,
        preflight_context: object | None = None,
        admission_preflight: object | None = None,
        admission_report: object | None = None,
    ) -> None:
        if not isinstance(request, DataRequest):
            raise InvalidDataRequestError("request must be a frozen DataRequest")
        self._request = request
        self._calendar_provider = calendar_provider
        self._warmup_resolver = warmup_resolver
        self._capability_manifest_version = capability_manifest_version
        self._on_ready = on_ready
        self._on_close = on_close
        # Optional Phase 2a composition layer.  The calendar/session
        # implementation remains authoritative for calendar facts; the
        # service only enriches that immutable report with profile, fixture,
        # initial-position, and page/session hash evidence.
        self._preflight_service = preflight_service
        self._preflight_context = preflight_context
        if admission_preflight is not None and admission_report is not None and admission_preflight is not admission_report:
            raise InvalidDataRequestError(
                "admission_preflight and admission_report cannot disagree"
            )
        self._admission_preflight = (
            admission_preflight
            if admission_preflight is not None
            else admission_report
        )
        self._preflight_outcome: object | None = None
        self._session_preflight_decision: object | None = None
        self._state = DataSessionState.CREATED
        self._axis: CalendarAxisResolution | None = None
        self._snapshot: CalendarSnapshot | None = None
        self._resolved_sessions: tuple[SessionPoint, ...] | None = None
        self._warmup_sessions: tuple[SessionPoint, ...] | None = None
        self._warmup_resolution: WarmupResolution | None = None
        self._report: DataPreflightReport | None = None
        self._closed_resources = False
        self._preflight_done = False
        # Keep fixed and per-step dynamic authorization separate.  This class
        # does not itself produce candidates; it only stores the bounded
        # permission state that a concrete chunk implementation may consume.
        self._fixed_authorized_instrument_ids = _fixed_authorized_ids(request)
        self._step_candidate_authorized_instrument_ids: frozenset[UUID] = frozenset()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "AuthoritativeDataSession":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool | None:
        self.close()
        return None

    @property
    def state(self) -> DataSessionState:
        """Current lifecycle state."""

        return self._state

    def close(self) -> None:
        """Release provider resources exactly once and forbid further use."""

        if self._state is DataSessionState.CLOSED:
            return
        self._state = DataSessionState.CLOSED
        if not self._closed_resources:
            self._closed_resources = True
            if self._on_close is not None:
                self._on_close(self)

    @property
    def fixed_authorized_instrument_ids(self) -> frozenset[UUID]:
        """Run-level fixed IDs: static, mandatory, and non-zero holdings."""

        return self._fixed_authorized_instrument_ids

    @property
    def step_candidate_authorized_instrument_ids(self) -> frozenset[UUID]:
        """Current decision-step dynamic IDs, initially empty."""

        return self._step_candidate_authorized_instrument_ids

    @property
    def authorized_instrument_ids(self) -> frozenset[UUID]:
        """Read-only union for concrete chunk permission checks."""

        return frozenset(
            self._fixed_authorized_instrument_ids
            | self._step_candidate_authorized_instrument_ids
        )

    @property
    def frozen_calendar_ids(self) -> tuple[str, ...]:
        """The calendar ids frozen by admission; never extended at runtime."""

        return tuple(getattr(self._request, "resolved_calendar_ids", ()))

    def begin_decision_step(self, step_key: object | None = None) -> None:
        """Start a new decision epoch and clear prior dynamic permissions."""

        if self._state is DataSessionState.CLOSED:
            raise DataSessionClosedError(
                "the data session is closed; no decision step may start",
                details={"session_state": self._state.value},
            )
        if self._state is not DataSessionState.READY:
            raise InvalidDataRequestError(
                "decision-step authorization requires a ready preflight"
            )
        self._step_candidate_authorized_instrument_ids = frozenset()
        del step_key

    def authorize_step_candidates(self, instrument_ids, *, query=None) -> None:
        """Record only UUIDs returned by the current bound universe query."""

        if self._state is DataSessionState.CLOSED:
            raise DataSessionClosedError(
                "the data session is closed; candidates cannot be authorized",
                details={"session_state": self._state.value},
            )
        if self._state is not DataSessionState.READY:
            raise InvalidDataRequestError(
                "step candidate authorization requires a ready preflight"
            )
        values = tuple(instrument_ids or ())
        if any(not isinstance(item, UUID) for item in values):
            raise InvalidDataRequestError(
                "step candidate authorization requires UUID instrument ids"
            )
        if values and query is None:
            raise InvalidDataRequestError(
                "dynamic step candidates require the bound UniverseQuery result"
            )
        if query is not None:
            result_ids = getattr(query, "authorized_instrument_ids", None)
            if result_ids is None:
                result_ids = getattr(query, "candidate_ids", None)
            if result_ids is None:
                result = getattr(query, "result", None)
                if result is not None:
                    result_ids = {
                        getattr(item, "instrument_id", None)
                        for item in result
                    }
            if result_ids is None or not set(values).issubset(set(result_ids)):
                raise InvalidDataRequestError(
                    "step candidates are not contained in the bound universe result"
                )
        self._step_candidate_authorized_instrument_ids = frozenset(values)

    bind_step_candidates = authorize_step_candidates
    authorize_step_candidate_ids = authorize_step_candidates

    def clear_step_candidate_authorization(self) -> None:
        """Clear dynamic permissions without altering fixed authorization."""

        self.begin_decision_step()

    # ------------------------------------------------------------------
    # Frozen results
    # ------------------------------------------------------------------

    def _frozen_sessions(self, field_name: str) -> tuple[SessionPoint, ...]:
        """Read a frozen session tuple with stable state-boundary errors."""

        if self._state is DataSessionState.CREATED:
            raise InvalidDataRequestError(
                f"{field_name} are not available before a completed "
                "preflight"
            )
        if self._state is DataSessionState.CLOSED and not self._preflight_done:
            raise DataSessionClosedError(
                "the data session was closed before its preflight completed; "
                f"no frozen {field_name} exist",
                details={"session_state": self._state.value},
            )
        sessions = getattr(self, f"_{field_name}")
        assert sessions is not None
        return sessions

    @property
    def resolved_sessions(self) -> tuple[SessionPoint, ...]:
        """Formal sessions; empty when blocked, forbidden before preflight."""

        return self._frozen_sessions("resolved_sessions")

    @property
    def warmup_sessions(self) -> tuple[SessionPoint, ...]:
        """Warmup sessions; empty when blocked, forbidden before preflight."""

        return self._frozen_sessions("warmup_sessions")

    @property
    def warmup_resolution(self) -> WarmupResolution | None:
        """The mounted warmup resolution, or ``None`` before preflight."""

        return self._warmup_resolution

    @property
    def snapshot(self) -> CalendarSnapshot | None:
        """Immutable calendar snapshot opened by the strict calendar path."""

        return self._snapshot

    @property
    def report(self) -> DataPreflightReport | None:
        """The immutable preflight report, or ``None`` before preflight."""

        return self._report

    @property
    def preflight_outcome(self) -> object | None:
        """Profile-bound Phase 2a outcome, when a service was supplied."""

        return self._preflight_outcome

    @property
    def session_preflight_decision(self) -> object | None:
        """Authoritative page/session decision, when available."""

        return self._session_preflight_decision

    @property
    def admission_report_hash(self) -> str | None:
        """Page admission hash associated with this session report."""

        outcome = self._preflight_outcome
        return getattr(outcome, "admission_report_hash", None)

    @property
    def session_report_hash(self) -> str | None:
        """Profile-bound hash of the authoritative session report."""

        outcome = self._preflight_outcome
        return getattr(outcome, "report_hash", None)

    @property
    def consistency_context(self) -> DataConsistencyContext:
        """The internally bound consistency context of this session."""

        return _SessionConsistencyContext(
            mode=self._request.consistency_mode,
            token_contract=self._request.consistency_token_contract,
            context_summary=MappingProxyType(
                {
                    "session_state": self._state.value,
                    "resolved_session_count": (
                        len(self._resolved_sessions)
                        if self._resolved_sessions is not None
                        else None
                    ),
                    "warmup_session_count": (
                        len(self._warmup_sessions)
                        if self._warmup_sessions is not None
                        else None
                    ),
                }
            ),
        )

    # ------------------------------------------------------------------
    # Authoritative preflight
    # ------------------------------------------------------------------

    def _apply_preflight_service(
        self, report: DataPreflightReport
    ) -> DataPreflightReport:
        """Bind the existing authoritative report to Phase 2a metadata.

        The service receives ``base_report`` so it cannot invoke this session
        recursively.  A supplied admission decision is compared before any
        strategy hook can run; a changed report is therefore a hard
        ``data_preflight`` failure with both hashes retained in the outcome.
        """

        service = self._preflight_service
        if service is None:
            return report
        from dataclasses import replace as _replace

        from app.backtesting.data.preflight_service import PreflightContext

        context = self._preflight_context
        if context is None:
            context = PreflightContext(
                request=self._request,
                profile=getattr(service, "profile", None),
                base_report=report,
            )
        elif isinstance(context, PreflightContext):
            context = _replace(context, request=self._request, base_report=report)
        elif isinstance(context, Mapping):
            context = dict(context)
            context["request"] = self._request
            context["base_report"] = report
        else:
            context = PreflightContext(
                request=self._request,
                profile=getattr(context, "profile", getattr(service, "profile", None)),
                provider=getattr(context, "provider", None),
                fixtures=getattr(context, "fixtures", ()),
                spec=getattr(context, "spec", None),
                initial_position_gateway=getattr(
                    context, "initial_position_gateway", None
                ),
                dynamic_scope_resolver=getattr(
                    context, "dynamic_scope_resolver", None
                ),
                calendar_resolver=getattr(context, "calendar_resolver", None),
                coverage_qualifier=getattr(context, "coverage_qualifier", None),
                base_report=report,
            )
        validate = getattr(service, "validate_session", None)
        if callable(validate):
            decision = validate(context, admission=self._admission_preflight)
            self._session_preflight_decision = decision
            outcome = getattr(decision, "outcome", None)
        else:
            outcome = service.preflight(context, authoritative=True)
        self._preflight_outcome = outcome
        enriched = getattr(outcome, "report", None)
        if not isinstance(enriched, DataPreflightReport):
            raise ProviderContractViolationError(
                "preflight service returned an invalid session report"
            )
        # Keep the session's public report/state aligned with the service
        # decision.  In particular a page/session hash mismatch must turn a
        # previously calendar-ready session into a blocked preflight before
        # the existing ``on_ready`` callback can notify the engine.
        self._report = enriched
        self._resolved_sessions = enriched.resolved_sessions
        self._warmup_sessions = enriched.warmup_sessions
        self._warmup_resolution = enriched.warmup_resolution
        self._state = (
            DataSessionState.BLOCKED
            if enriched.status is PreflightStatus.BLOCKED
            else DataSessionState.READY
        )
        return enriched

    def preflight(
        self, request: DataPreflightRequest | None = None
    ) -> DataPreflightReport:
        """Run the authoritative preflight exactly once, from ``created``.

        Returns the frozen :class:`DataPreflightReport`; the session state
        becomes ``ready`` or ``blocked`` and can never return to an earlier
        state.  On any blocking condition no partial formal or warmup
        session sequence is exposed afterwards.
        """

        if self._state is not DataSessionState.CREATED:
            raise InvalidDataRequestError(
                "preflight must run exactly once from the created state; "
                f"current state is {self._state.value}"
            )
        if request is not None:
            if not isinstance(request, DataPreflightRequest):
                raise InvalidDataRequestError(
                    "request must be a DataPreflightRequest instance"
                )
            if not self._matches_frozen_intent(request):
                raise InvalidDataRequestError(
                    "the preflight request must match the frozen session "
                    "request on every shared business field"
                )
        frozen_request = self._request
        issues: list[PreflightIssue] = []

        # Task-11 providers expose one atomic snapshot operation.  Do not
        # re-use a page handle or fall back to separate formal/warmup reads.
        open_snapshot = getattr(self._calendar_provider, "open_calendar_snapshot", None)
        use_strict_snapshot = callable(open_snapshot) and _provider_has_canonical_calendar_metadata(
            self._calendar_provider,
            frozen_request.resolved_calendar_ids,
        )
        if not use_strict_snapshot and not isinstance(
            self._calendar_provider, InMemoryCalendarAxisDataProvider
        ):
            # Legacy definitions()/fact() providers are retained for direct
            # resolver diagnostics only.  A formal DataSession must not turn
            # per-day reads into an unversioned strict run.
            legacy_issue = PreflightIssue(
                code="unsupported_capability",
                severity=IssueSeverity.ERROR,
                scope=SCOPE_FORMAL,
                message="日历提供方不支持正式会话所需的 PIT 批量快照能力，已阻断回测。",
                field="calendar_provider",
                details={
                    "cause_code": "calendar_provider_legacy",
                    "provider_type": type(self._calendar_provider).__name__,
                },
            )
            self._report = _snapshot_failure_report(
                frozen_request,
                capability_manifest_version=self._capability_manifest_version,
                issues=(legacy_issue,),
            )
            self._resolved_sessions = ()
            self._warmup_sessions = ()
            self._preflight_done = True
            self._state = DataSessionState.BLOCKED
            return self._apply_preflight_service(self._report)
        if use_strict_snapshot and frozen_request.query_boundary is None:
            # A provider exposing the task-11 snapshot protocol is never
            # allowed to fall back to the legacy per-day resolver.  The
            # missing cutoff is a pre-read gate and must not touch facts.
            cutoff_issue = PreflightIssue(
                code="DATA_CUTOFF_REQUIRED",
                severity=IssueSeverity.ERROR,
                scope=SCOPE_FORMAL,
                message="严格日历会话必须显式提供 data_cutoff，系统不会使用墙上时钟推断。",
                field="query_boundary.data_cutoff",
                details={"cause_code": "data_cutoff_required"},
            )
            self._report = _snapshot_failure_report(
                frozen_request,
                capability_manifest_version=self._capability_manifest_version,
                issues=(cutoff_issue,),
            )
            self._resolved_sessions = ()
            self._warmup_sessions = ()
            self._preflight_done = True
            self._state = DataSessionState.BLOCKED
            return self._apply_preflight_service(self._report)
        if use_strict_snapshot and frozen_request.query_boundary is not None:
            try:
                snapshot = open_snapshot(
                    CalendarSnapshotRequest(
                        calendar_ids=frozen_request.resolved_calendar_ids,
                        formal_start=frozen_request.requested_window.start_date,
                        formal_end=frozen_request.requested_window.end_date,
                        warmup_sessions=frozen_request.warmup_sessions,
                        query_boundary=frozen_request.query_boundary,
                        instrument_ids=tuple(
                            dict.fromkeys(
                                (
                                    *frozen_request.static_instrument_ids,
                                    *frozen_request.mandatory_instrument_ids,
                                )
                            )
                        ),
                        provider_key=frozen_request.provider_key,
                        package_key=frozen_request.rule_package.key,
                        package_version=frozen_request.rule_package.version,
                    )
                )
            except CalendarPreflightResourceLimitExceededError:
                # A resource overrun is a creation-gate response, not a
                # report issue.  Re-wrapping it as a blocked @1 report would
                # falsely advertise a pageable/persistable result to the
                # coordinator.  Mark this session terminal while preserving
                # the stable domain error for the caller to project through
                # resource_limited_preflight_failure().
                self._snapshot = None
                self._axis = None
                self._resolved_sessions = ()
                self._warmup_sessions = ()
                self._warmup_resolution = None
                self._report = None
                self._preflight_done = True
                self._state = DataSessionState.BLOCKED
                raise
            except CalendarContractError as exc:
                exc_details = dict(getattr(exc, "details", {}) or {})
                issue_code = getattr(exc, "code", "provider_contract_violation")
                if exc_details.get("cause_code") == "warmup_coverage_insufficient":
                    issue_code = "WARMUP_COVERAGE_INSUFFICIENT"
                issues.append(
                    PreflightIssue(
                        code=issue_code.upper(),
                        severity=IssueSeverity.ERROR,
                        scope=SCOPE_FORMAL,
                        message="交易日历快照无法打开，已阻断回测。",
                        field="calendar_snapshot",
                        details={"cause_code": getattr(exc, "code", "provider_contract_violation"), **exc_details},
                    )
                )
                snapshot = None
            if snapshot is not None:
                self._snapshot = snapshot
                self._axis = snapshot.resolution
                try:
                    capability_issues, capability_evidence = evaluate_calendar_capability_gate(
                        self._calendar_provider,
                        frozen_request,
                        snapshot,
                    )
                    self._report = _snapshot_report(
                        frozen_request,
                        snapshot,
                        capability_manifest_version=self._capability_manifest_version,
                        extra_issues=(*issues, *capability_issues),
                        capability_evidence=capability_evidence,
                    )
                except CalendarPreflightResourceLimitExceededError:
                    # Report construction performs the issue/JSON resource
                    # checks after the immutable snapshot is available.  It
                    # still belongs to the same non-pageable creation gate.
                    self._snapshot = None
                    self._axis = None
                    self._resolved_sessions = ()
                    self._warmup_sessions = ()
                    self._warmup_resolution = None
                    self._report = None
                    self._preflight_done = True
                    self._state = DataSessionState.BLOCKED
                    raise
                self._report = self._apply_preflight_service(self._report)
                self._resolved_sessions = self._report.resolved_sessions
                self._warmup_sessions = self._report.warmup_sessions
                self._warmup_resolution = self._report.warmup_resolution
                self._preflight_done = True
                self._state = DataSessionState.BLOCKED if self._report.status is PreflightStatus.BLOCKED else DataSessionState.READY
                if self._state is DataSessionState.READY and self._on_ready is not None:
                    self._on_ready(self)
                return self._report
            # Snapshot failures are represented as a blocked report below;
            # no legacy resolver is allowed to read around a strict error.
            self._report = _snapshot_failure_report(
                frozen_request,
                capability_manifest_version=self._capability_manifest_version,
                issues=issues,
            )
            self._resolved_sessions = ()
            self._warmup_sessions = ()
            self._preflight_done = True
            self._state = DataSessionState.BLOCKED
            return self._apply_preflight_service(self._report)

        # 1-2. Resolve the formal window strictly through strict_compatible@1.
        try:
            axis = resolve_calendar_axis(
                self._calendar_provider,
                policy_key=POLICY_KEY_STRICT_COMPATIBLE,
                policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
                start_date=frozen_request.requested_window.start_date,
                end_date=frozen_request.requested_window.end_date,
                calendar_ids=frozen_request.resolved_calendar_ids,
                # The compatibility resolver predates canonical registry/PIT
                # metadata.  Its behavior is intentionally preserved for such
                # providers; strict providers never reach this branch.
                query_boundary=None,
            )
        except CalendarPreflightResourceLimitExceededError:
            # The compatibility resolver also enforces the calendar-count
            # guard.  Keep its creation-gate semantics identical to the
            # atomic snapshot path above instead of returning a report.
            self._snapshot = None
            self._axis = None
            self._resolved_sessions = ()
            self._warmup_sessions = ()
            self._warmup_resolution = None
            self._report = None
            self._preflight_done = True
            self._state = DataSessionState.BLOCKED
            raise
        self._axis = axis

        warmup_resolution: WarmupResolution | None = None
        if axis.status is CalendarAxisStatus.INCOMPATIBLE:
            # Map to the stable blocked code and keep the full differences.
            issues.append(
                PreflightIssue(
                    code="data_preflight_blocked",
                    severity=IssueSeverity.ERROR,
                    scope=SCOPE_FORMAL,
                    message=(
                        f"正式区间 "
                        f"{frozen_request.requested_window.start_date.isoformat()}.."
                        f"{frozen_request.requested_window.end_date.isoformat()} "
                        f"日历轴不兼容，共 {len(axis.differences)} 处差异，已阻断回测"
                    ),
                    field="calendar_axis",
                    details={"differences": _difference_details(axis.differences)},
                ),
            )
        elif not axis.resolved_sessions:
            issues.append(
                PreflightIssue(
                    code=NO_FORMAL_SESSIONS,
                    severity=IssueSeverity.ERROR,
                    scope=SCOPE_FORMAL,
                    message=(
                        f"正式区间 "
                        f"{frozen_request.requested_window.start_date.isoformat()}.."
                        f"{frozen_request.requested_window.end_date.isoformat()} "
                        "内没有任何共同开市交易会话，无法启动回测"
                    ),
                    field="resolved_sessions",
                ),
            )
        elif frozen_request.warmup_sessions > 0:
            # 3-5. Warmup resolution anchored at the first formal session.
            warmup_resolution = resolve_warmup_sessions(
                self._calendar_provider,
                calendar_ids=frozen_request.resolved_calendar_ids,
                first_formal_session=axis.resolved_sessions[0].session_date,
                requested_sessions=frozen_request.warmup_sessions,
                resolver=self._warmup_resolver,
                query_boundary=(
                    frozen_request.query_boundary
                    if use_strict_snapshot
                    else None
                ),
                pit_context=(
                    CalendarPITContext.from_query_boundary(
                        frozen_request.query_boundary,
                        "Asia/Shanghai",
                    )
                    if use_strict_snapshot and frozen_request.query_boundary is not None
                    else None
                ),
            )
            issues.extend(warmup_resolution.issues)

        blocked = any(
            issue.severity is IssueSeverity.ERROR for issue in issues
        )
        formal_sessions: tuple[SessionPoint, ...] = (
            () if blocked else axis.resolved_sessions
        )
        warmup_sessions: tuple[SessionPoint, ...] = (
            ()
            if blocked
            or warmup_resolution is None
            or warmup_resolution.status is not WarmupStatus.READY
            else warmup_resolution.resolved_sessions
        )

        # 6-7. Freeze both tuples and build the immutable report.
        report = DataPreflightReport(
            status=(
                PreflightStatus.BLOCKED
                if blocked
                else PreflightStatus.READY
            ),
            generated_at=datetime.now().astimezone(),
            provider_key=frozen_request.provider_key,
            capability_manifest_version=self._capability_manifest_version,
            requested_window=frozen_request.requested_window,
            scope_mode=frozen_request.instrument_scope_mode,
            resolved_calendar_ids=frozen_request.resolved_calendar_ids,
            resolved_calendar_definitions=self._resolved_definitions(
                frozen_request.resolved_calendar_ids
            ),
            resolved_timezone=axis.timezone,
            calendar_axis_policy=frozen_request.calendar_axis_policy,
            calendar_compatibility_status=axis.status,
            # Empty exactly when the axis is incompatible; a compatible axis
            # always publishes its session signature, even when it resolved
            # zero formal sessions (NO_FORMAL_SESSIONS block).
            calendar_session_signature=axis.session_signature,
            resolved_sessions=formal_sessions,
            warmup_sessions=warmup_sessions,
            max_lookback_sessions=frozen_request.max_lookback_sessions,
            knowledge_as_of=frozen_request.query_boundary.knowledge_as_of,
            non_strict_pit_capabilities=(),
            consistency_mode=frozen_request.consistency_mode,
            consistency_token_capability=bool(
                frozen_request.consistency_token_contract
            ),
            consistency_token_contract=(
                frozen_request.consistency_token_contract
                if frozen_request.consistency_mode
                is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
                else None
            ),
            data_chunk_policy=frozen_request.data_chunk_policy,
            data_chunk_size_sessions=frozen_request.data_chunk_size_sessions,
            required_capabilities=frozen_request.required_capabilities,
            rule_package=frozen_request.rule_package,
            rule_exception_set=frozen_request.rule_exception_set,
            static_instrument_ids=frozen_request.static_instrument_ids,
            mandatory_instrument_ids=frozen_request.mandatory_instrument_ids,
            strategy_price_bases=frozen_request.strategy_price_bases,
            engine_price_basis=frozen_request.engine_price_basis,
            data_contract_version=frozen_request.data_contract_version,
            frequency=frozen_request.frequency,
            warmup_sessions_count=frozen_request.warmup_sessions,
            market_scope=frozen_request.market_scope,
            universe_query_policy=frozen_request.universe_query_policy,
            allowed_settlement_rule_class=(
                frozen_request.allowed_settlement_rule_class
            ),
            adjustment_series_policy=frozen_request.adjustment_series_policy,
            quality_mode=frozen_request.quality_mode,
            issues=tuple(issues),
            warmup_resolution=warmup_resolution,
            warmup_resolution_signature=(
                warmup_resolution.resolution_signature
                if warmup_resolution is not None
                else None
            ),
            calendar_axis_differences=axis.differences,
            warmup_axis_differences=(
                warmup_resolution.axis_differences
                if warmup_resolution is not None
                else ()
            ),
            # Even the compatibility report carries the canonical request
            # boundary so admission compares the exact request semantics.
            # It remains hash-schema v1 because this provider has no strict
            # calendar snapshot evidence.
            query_boundary=frozen_request.query_boundary,
        )
        report = self._apply_preflight_service(report)
        self._resolved_sessions = report.resolved_sessions
        self._warmup_sessions = report.warmup_sessions
        self._warmup_resolution = report.warmup_resolution
        self._report = report
        self._preflight_done = True
        self._state = (
            DataSessionState.BLOCKED
            if report.status is PreflightStatus.BLOCKED
            else DataSessionState.READY
        )
        self._step_candidate_authorized_instrument_ids = frozenset()
        # The engine-owned strategy start is only notified on success; a
        # blocked preflight never reaches any strategy hook.
        if self._state is DataSessionState.READY and self._on_ready is not None:
            self._on_ready(self)
        return report

    # ------------------------------------------------------------------
    # Later deliverables
    # ------------------------------------------------------------------

    def open_chunk(self, query: DataChunkQuery) -> "object":
        """Chunk opening belongs to the chunk-flow deliverable."""

        if self._state is DataSessionState.CLOSED:
            raise DataSessionClosedError(
                "the data session is closed; no new chunk may be opened",
                details={"session_state": self._state.value},
            )
        raise UnsupportedCapabilityError(
            "the chunk flow is delivered by a later task; this session only "
            "owns preflight, formal sessions, and warmup sessions"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _matches_frozen_intent(self, request: DataPreflightRequest) -> bool:
        """Compare the original intent with the frozen request by business.

        A session opened from a frozen ``DataRequest`` may legitimately
        receive the original unresolved :class:`DataPreflightRequest` for
        its authoritative re-check; the admission-only fields that preflight
        itself added (resolved calendars, time zone, hashes) are not part
        of the comparison.
        """

        for field_name in DataPreflightRequest.__dataclass_fields__:
            if getattr(request, field_name) != getattr(
                self._request, field_name
            ):
                return False
        return True

    def _resolved_definitions(
        self, calendar_ids: tuple[str, ...]
    ) -> tuple[CalendarDefinition, ...]:
        """Collect the calendar definitions behind the frozen id set."""

        definitions: list[CalendarDefinition] = []
        for calendar_id in calendar_ids:
            definitions.extend(self._calendar_provider.definitions(calendar_id))
        return tuple(definitions)
