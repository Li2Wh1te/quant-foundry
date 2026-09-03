"""Phase 2a data-preflight orchestration.

The service in this module is deliberately a composition boundary.  Calendar
resolution, Bar validation, instrument/rule resolution, and dynamic candidate
semantics remain owned by their existing providers.  This module binds those
already-frozen results to the one Phase 2a profile, persists admission/session
evidence through the existing result DTO, and applies the hard gate before a
caller may load a strategy.

There is intentionally no ORM, FastAPI, network client, strategy callback, or
run-creation implementation here.  Internal link-acceptance is the only
ready-capable profile in this phase; formal@1 is represented as blocked until
the production gates delivered by later task packages exist.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timezone
import inspect
import json
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import CalendarAxisStatus
from app.backtesting.data.errors import (
    DataContractError,
    InternalPreflightFixtureMissingError,
    InternalPreflightProfileMismatchError,
    InvalidDataRequestError,
    ProviderContractViolationError,
    freeze_json,
)
from app.backtesting.data.reports import (
    DataPreflightReport,
    PreflightIssue,
    canonical_hash,
    canonical_json,
)
from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DataPreflightRequest,
    DataRequest,
    DateRange,
    InternalFixture,
    INTERNAL_FIXTURE_CAPABILITIES,
    INTERNAL_LINK_ACCEPTANCE_PROFILE,
    INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY,
    INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION,
    INTERNAL_LINK_ACCEPTANCE_RUN_KIND,
    FORMAL_PROFILE,
    FORMAL_PROFILE_KEY,
    FORMAL_PROFILE_VERSION,
    FORMAL_RUN_KIND,
    MAX_LOOKBACK_SESSIONS,
    PreflightProfile,
    PreflightProfileRegistry,
    InstrumentScopeMode,
    IssueSeverity,
    PreflightStatus,
)
from app.backtesting.preflight import (
    BacktestPreflightGateway,
    InitialPositionPreflightReport,
    InitialPositionPreflightService,
    resolve_dynamic_universe_scope,
)
from app.backtesting.spec import BacktestSpec


RUN_KIND_INTERNAL_LINK_ACCEPTANCE = INTERNAL_LINK_ACCEPTANCE_RUN_KIND
RUN_KIND_FORMAL = FORMAL_RUN_KIND
INTERNAL_LINK_ACCEPTANCE_PROFILE_TEXT = (
    f"{INTERNAL_LINK_ACCEPTANCE_PROFILE_KEY}@{INTERNAL_LINK_ACCEPTANCE_PROFILE_VERSION}"
)
FORMAL_PROFILE_TEXT = f"{FORMAL_PROFILE_KEY}@{FORMAL_PROFILE_VERSION}"
PREFLIGHT_METADATA_KEY = "__preflight__"

FIXTURE_QUANTITY_ACTIONS = "quantity_action_coverage"
FIXTURE_TRADING_STATUS = "trading_status"
FIXTURE_SOURCE_REVISIONS = "source_revision_audit"
FIXTURE_REPEATABLE_READ = "transitional_repeatable_read"

# Profile registration is exact: the service never accepts an arbitrary
# ``key@version`` merely because the string is well formed.  A caller may add
# a test fixture key explicitly through ``register_fixture``.
INTERNAL_FIXTURE_REGISTRY: Mapping[str, tuple[int, str]] = MappingProxyType(
    {
        "quantity_action_coverage@1": (1, FIXTURE_QUANTITY_ACTIONS),
        "trading_status@1": (1, FIXTURE_TRADING_STATUS),
        "source_revision_audit@1": (1, FIXTURE_SOURCE_REVISIONS),
        "transitional_repeatable_read@1": (1, FIXTURE_REPEATABLE_READ),
    }
)


# Re-export the canonical error classes from ``errors.py``.  Keeping aliases
# here makes the orchestration API discoverable without creating a second
# error hierarchy or a second source of stable machine codes.
PreflightProfileMismatchError = InternalPreflightProfileMismatchError
InternalFixtureContractError = InternalPreflightFixtureMissingError
PreflightServiceError = DataContractError


def _profile_text(profile: PreflightProfile) -> str:
    """Read the canonical request-layer profile reference."""

    return profile.profile


def _profile_allows_fixtures(profile: PreflightProfile) -> bool:
    """Keep compatibility with the request-layer spelling."""

    return profile.allow_fixture_only


def _profile_allowed_capabilities(profile: PreflightProfile) -> tuple[str, ...]:
    """Return the immutable semantic fixture capability allow-list."""

    return tuple(profile.allowed_fixture_capabilities)


def _issue(
    code: str,
    message: str,
    *,
    field: str | None = None,
    scope: str = "preflight",
    details: Mapping[str, object] | None = None,
    instrument_id: UUID | None = None,
    severity: IssueSeverity = IssueSeverity.ERROR,
) -> PreflightIssue:
    """Build a structured issue whose primary operator text is Chinese."""

    return PreflightIssue(
        code=code,
        severity=severity,
        scope=scope,
        message=message,
        field=field,
        details=details,
        instrument_id=instrument_id,
    )


@dataclass(frozen=True, slots=True)
class PreflightContext:
    """All inputs for one page or authoritative session preflight."""

    request: DataPreflightRequest | DataRequest
    provider: object | None = None
    session: object | None = None
    profile: PreflightProfile | ContractRef | str | None = None
    run_kind: str | None = None
    fixtures: tuple[InternalFixture, ...] = ()
    spec: BacktestSpec | None = None
    initial_position_gateway: BacktestPreflightGateway | None = None
    dynamic_scope_resolver: object | None = None
    calendar_resolver: object | None = None
    coverage_qualifier: object | None = None
    # The fixed rule preflight report is optional for dynamic-only requests,
    # but when supplied it is the only authority allowed to decide whether
    # STATUS belongs in this request's capability set.
    rule_preflight_report: object | None = None
    # A caller that already opened the authoritative session may pass its
    # immutable provider report.  This prevents a second calendar read while
    # the service binds profile metadata and performs the session comparison.
    base_report: DataPreflightReport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, (DataPreflightRequest, DataRequest)):
            raise InvalidDataRequestError(
                "preflight context request must be a DataPreflightRequest"
            )
        if self.base_report is not None and not isinstance(
            self.base_report, DataPreflightReport
        ):
            raise InvalidDataRequestError("base_report must be a DataPreflightReport")
        object.__setattr__(self, "fixtures", tuple(self.fixtures or ()))


@dataclass(frozen=True, slots=True)
class PreflightOutcome:
    """A canonical report plus Phase 2a run/profile and fixture evidence."""

    report: DataPreflightReport
    profile: PreflightProfile
    fixed_instrument_ids: tuple[UUID, ...] = ()
    dynamic_scope: Mapping[str, object] | None = None
    fixtures: tuple[InternalFixture, ...] = ()
    initial_position_report: InitialPositionPreflightReport | None = None
    admission_report_hash: str | None = None
    session_report_hash: str | None = None
    hash_match: bool | None = None
    report_diff: tuple[Mapping[str, object], ...] = ()
    failure_phase: str | None = None
    qualification_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.report, DataPreflightReport):
            raise InvalidDataRequestError("outcome report must be a DataPreflightReport")
        if not isinstance(self.profile, PreflightProfile):
            raise InvalidDataRequestError("outcome profile must be a PreflightProfile")
        ids = tuple(sorted(set(self.fixed_instrument_ids), key=str))
        if any(not isinstance(item, UUID) for item in ids):
            raise InvalidDataRequestError("fixed_instrument_ids entries must be UUIDs")
        object.__setattr__(self, "fixed_instrument_ids", ids)
        fixtures = tuple(self.fixtures)
        if any(not isinstance(item, InternalFixture) for item in fixtures):
            raise InvalidDataRequestError("outcome fixtures must be InternalFixture values")
        if (
            self.profile.allow_fixture_only
            and DataCapability.STATUS not in self.report.required_capabilities
        ):
            # Keep an optional status substitute out of the outcome itself as
            # a second defensive boundary.  The service filters it before
            # validation; this guard also protects callers that construct an
            # outcome directly and would otherwise change its hash.
            fixtures = tuple(
                fixture
                for fixture in fixtures
                if str(getattr(fixture.capability, "value", fixture.capability))
                != FIXTURE_TRADING_STATUS
            )
        object.__setattr__(
            self,
            "fixtures",
            tuple(
                sorted(
                    fixtures,
                    key=lambda item: (
                        item.capability,
                        item.fixture_key,
                        str(item.fixture_version),
                    ),
                )
            ),
        )
        if self.dynamic_scope is None:
            scope: Mapping[str, object] = MappingProxyType({})
        elif isinstance(self.dynamic_scope, Mapping):
            frozen_scope = freeze_json(
                json.loads(canonical_json(dict(self.dynamic_scope))),
                "dynamic_scope",
            )
            if not isinstance(frozen_scope, Mapping):
                raise InvalidDataRequestError("dynamic_scope must be a JSON mapping")
            scope = frozen_scope
        else:
            raise InvalidDataRequestError("dynamic_scope must be a JSON mapping")
        object.__setattr__(self, "dynamic_scope", scope)
        diffs: list[Mapping[str, object]] = []
        for item in self.report_diff:
            if not isinstance(item, Mapping):
                raise InvalidDataRequestError("report_diff entries must be mappings")
            frozen_diff = freeze_json(
                json.loads(canonical_json(dict(item))), "report_diff entry"
            )
            if not isinstance(frozen_diff, Mapping):
                raise InvalidDataRequestError("report_diff entries must be mappings")
            diffs.append(frozen_diff)
        object.__setattr__(self, "report_diff", tuple(diffs))
        object.__setattr__(
            self,
            "report",
            replace(
                self.report,
                run_kind=self.profile.run_kind,
                preflight_profile_key=self.profile.key,
                preflight_profile_version=self.profile.version,
                resolved_instruments=ids,
                fixture_sources=tuple(
                    item.machine_content() for item in self.fixtures
                ),
                report_hash="",
            ),
        )
        object.__setattr__(self, "qualification_hash", _qualification_hash(self))

    @property
    def status(self) -> PreflightStatus:
        """The normalized report status."""

        return self.report.status

    @property
    def blocked(self) -> bool:
        """Whether creation or strategy loading must stop."""

        return self.status is not PreflightStatus.READY

    @property
    def report_hash(self) -> str:
        """The profile-bound qualification hash used for admission binding."""

        return self.qualification_hash

    @property
    def base_report_hash(self) -> str:
        """The underlying canonical DataPreflightReport hash."""

        return self.report.report_hash

    @property
    def profile_reference(self) -> str:
        """Return ``key@version`` for operator and persistence projections."""

        return _profile_text(self.profile)

    @property
    def run_kind(self) -> str:
        """Stable server-owned run kind."""

        return self.profile.run_kind

    @property
    def preflight_profile(self) -> str:
        """Stable ``key@version`` profile reference."""

        return self.profile_reference

    @property
    def preflight_profile_key(self) -> str:
        return self.profile.key

    @property
    def preflight_profile_version(self) -> int:
        return self.profile.version

    @property
    def fixture_sources(self) -> tuple[InternalFixture, ...]:
        """Named internal facts carried by this outcome."""

        return self.fixtures

    def __getattr__(self, name: str) -> object:
        """Expose legacy report attributes without creating a second report type."""

        # Dataclass-generated field access runs before ``__getattr__``.  Any
        # remaining attribute is a compatibility read from the one canonical
        # DataPreflightReport; callers can still use ``outcome.report`` when
        # they need an explicit type boundary.
        return getattr(self.report, name)

    @property
    def as_report(self) -> DataPreflightReport:
        """Return the canonical report object carried by this outcome."""

        return self.report

    def canonical_content(self) -> dict[str, object]:
        """Return business evidence only; phase/timestamps/run IDs are absent."""

        report_content = getattr(self.report, "_hash_content", None)
        base = report_content() if callable(report_content) else {"report_hash": self.report.report_hash}
        return {
            "run_kind": self.profile.run_kind,
            "preflight_profile": self.profile_reference,
            "allow_fixture_only": self.profile.allow_fixture_only,
            "fixed_instrument_ids": [str(item) for item in self.fixed_instrument_ids],
            "dynamic_scope": self.dynamic_scope,
            "fixtures": [item.machine_content() for item in self.fixtures],
            "initial_position": (
                self.initial_position_report.canonical_content()
                if self.initial_position_report is not None
                else None
            ),
            "report": base,
        }

    def as_dict(self) -> dict[str, object]:
        """Return an operator-safe wire projection with explicit internal labels."""

        payload = dict(self.report.as_dict())
        details = dict(payload.get("details") or {})
        internal = self.profile.run_kind == RUN_KIND_INTERNAL_LINK_ACCEPTANCE
        metadata = {
            "run_kind": self.profile.run_kind,
            "preflight_profile_key": self.profile.key,
            "preflight_profile_version": self.profile.version,
            "preflight_profile": self.profile_reference,
            "report_hash": self.report_hash,
            "base_report_hash": self.report.report_hash,
            "fixed_instrument_ids": [str(item) for item in self.fixed_instrument_ids],
            "dynamic_scope": self.dynamic_scope,
            "fixture_sources": [item.machine_content() for item in self.fixtures],
            "admission_report_hash": self.admission_report_hash,
            "session_report_hash": self.session_report_hash,
            "hash_match": self.hash_match,
            "report_diff": list(self.report_diff),
            "failure_phase": self.failure_phase,
            "__consistency__": _consistency_summary(self.report),
        }
        coverage_by_capability = {
            item.capability.value: item.machine_content()
            for item in self.report.coverage_reports
        }
        bars = coverage_by_capability.get(DataCapability.BARS.value)
        mappings = coverage_by_capability.get(DataCapability.MAPPINGS.value)
        rules = coverage_by_capability.get(DataCapability.RULES.value)
        metadata.update(
            {
                # Keep the first-phase trading-status model visible in the
                # operator-facing capability summary.  The canonical report
                # already carries this value (and hashes only its machine
                # fields); exposing it here keeps preflight JSON consistent
                # with the adapter summary without creating a second fact
                # store or implying STATUS coverage.
                "trading_status": self.report.trading_status,
                "instrument_mapping_coverage": mappings,
                "instrument_rule_fact_summary": rules,
                "lookback_session_bar_coverage": bars,
                "bar_validity_summary": bars,
                "adjustment_series_policy": (
                    {
                        "key": self.report.adjustment_series_policy.key,
                        "version": self.report.adjustment_series_policy.version,
                    }
                    if self.report.adjustment_series_policy is not None
                    else None
                ),
                "universe_eligibility_policy_version": (
                    {
                        "key": self.report.universe_eligibility_policy_version.key,
                        "version": self.report.universe_eligibility_policy_version.version,
                    }
                    if isinstance(
                        self.report.universe_eligibility_policy_version, ContractRef
                    )
                    else self.report.universe_eligibility_policy_version
                ),
                "universe_eligibility_summary": self.report.universe_eligibility_summary,
                "missing_bars": (
                    bars.get("missing_ranges") if isinstance(bars, Mapping) else None
                ),
                "missing_fields": (
                    [issue.field for issue in self.report.issues if issue.field]
                    or None
                ),
                "invalid_bars": (
                    bars.get("counts", {}).get("invalid")
                    if isinstance(bars, Mapping)
                    else None
                ),
                "incomplete_rules": (
                    rules.get("counts", {}).get("unavailable")
                    if isinstance(rules, Mapping)
                    else None
                ),
                "non_pit_sources": list(self.report.non_strict_pit_capabilities),
            }
        )
        details.update(metadata)
        payload.update(
            {
                "run_kind": self.profile.run_kind,
                "preflight_profile": self.profile_reference,
                "report_hash": self.report_hash,
                "trading_status": self.report.trading_status,
                "title": "内部链路验收" if internal else "正式回测预检",
                "message": (
                    "内部链路验收预检已通过。"
                    if internal and not self.blocked
                    else "内部链路验收预检未通过，运行未创建。"
                    if internal
                    else "正式回测能力尚未就绪，运行未创建。"
                ),
                "details": details,
            }
        )
        # Normalize the complete wire projection once more so callers may
        # safely hand it to ``json.dumps`` without leaking MappingProxy/date
        # implementation objects.
        return json.loads(canonical_json(payload))

    def to_result_record(
        self,
        run_id: UUID,
        phase: object,
        *,
        admission: "PreflightOutcome | None" = None,
    ) -> object:
        """Build the existing ``backtest_data_preflight`` DTO."""

        from app.backtesting.result_models import (
            BacktestDataPreflightRecord,
            DataPhase,
        )

        if not isinstance(run_id, UUID):
            raise InvalidDataRequestError("run_id must be a UUID")
        if not isinstance(phase, DataPhase):
            try:
                phase = DataPhase(str(phase))
            except ValueError as exc:
                raise InvalidDataRequestError("phase must be admission or session") from exc
        report = self.report
        coverage = {
            item.capability.value: item.machine_content()
            for item in report.coverage_reports
        }
        calendar_summary = report.calendar_summary
        if calendar_summary is None:
            calendar_summary = {
                "calendar_ids": report.resolved_calendar_ids,
                "compatibility_status": report.calendar_compatibility_status.value,
                "calendar_session_signature": report.calendar_session_signature,
            }
        session_summary = report.session_summary
        if session_summary is None:
            session_summary = {
                "formal_session_count": len(report.resolved_sessions),
                "warmup_session_count": len(report.warmup_sessions),
                "calendar_session_signature": report.calendar_session_signature,
            }
        fixture_sources = tuple(item.machine_content() for item in self.fixtures)
        admission_hash = admission.report_hash if admission is not None else self.admission_report_hash
        if phase.value == "admission" and admission_hash is None:
            admission_hash = self.report_hash
        session_hash = self.session_report_hash
        if phase.value == "session" and session_hash is None:
            session_hash = self.report_hash
        metadata = {
            "run_kind": self.profile.run_kind,
            "preflight_profile_key": self.profile.key,
            "preflight_profile_version": self.profile.version,
            "preflight_profile": self.profile_reference,
            "qualification_hash": self.report_hash,
            "base_report_hash": report.report_hash,
            "fixed_instrument_ids": [str(item) for item in self.fixed_instrument_ids],
            "dynamic_scope": self.dynamic_scope,
            "fixture_sources": {"items": fixture_sources},
            "admission_report_hash": admission_hash,
            "session_report_hash": session_hash,
            "hash_match": self.hash_match,
            "report_diff": list(self.report_diff),
            "failure_phase": self.failure_phase,
        }
        capabilities = {
            "provider_key": report.provider_key,
            "manifest_version": report.capability_manifest_version,
            "required_capabilities": [item.value for item in report.required_capabilities],
            PREFLIGHT_METADATA_KEY: metadata,
            "__consistency__": _consistency_summary(report),
        }
        pit_status = (
            "non_strict"
            if report.non_strict_pit
            else "strict"
            if report.pit_context is not None or report.query_boundary is not None
            else None
        )
        return BacktestDataPreflightRecord(
            run_id=run_id,
            phase=phase,
            status=report.status.value,
            report_hash=self.report_hash,
            hash_schema_version=report.hash_schema_version,
            capabilities=capabilities,
            calendar_summary=calendar_summary,
            session_summary=session_summary,
            pit_status=pit_status,
            coverage=coverage,
            source_revisions=report.source_revisions,
            run_kind=self.profile.run_kind,
            preflight_profile_key=self.profile.key,
            preflight_profile_version=self.profile.version,
            admission_report_hash=admission_hash,
            session_report_hash=session_hash,
            hash_match=self.hash_match,
            report_diff=self.report_diff,
            failure_phase=self.failure_phase,
            fixture_sources={"items": fixture_sources},
            scope_summary={
                "fixed_instrument_ids": [str(item) for item in self.fixed_instrument_ids],
                "dynamic_scope": self.dynamic_scope,
            },
        )

    # The shorter spelling mirrors ``BacktestDataPreflightRecord`` callers
    # and is intentionally an alias, not a second persistence path.
    to_record = to_result_record


def _qualification_hash(outcome: PreflightOutcome) -> str:
    """Hash only normalized request/fact/profile evidence."""

    return canonical_hash(outcome.canonical_content())


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Page-level creation decision; no run is created by this object."""

    allowed: bool
    outcome: PreflightOutcome
    reason_code: str | None = None

    @property
    def report(self) -> DataPreflightReport:
        return self.outcome.report

    @property
    def report_hash(self) -> str:
        return self.outcome.report_hash

    @property
    def run_kind(self) -> str:
        return self.outcome.profile.run_kind

    @property
    def preflight_profile(self) -> str:
        return self.outcome.profile_reference

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass(frozen=True, slots=True)
class SessionPreflightDecision:
    """Authoritative session result and page/session hash binding."""

    allowed: bool
    outcome: PreflightOutcome
    admission_report_hash: str | None
    hash_match: bool | None
    report_diff: tuple[Mapping[str, object], ...] = ()
    failure_phase: str | None = None

    @property
    def report(self) -> DataPreflightReport:
        return self.outcome.report

    @property
    def report_hash(self) -> str:
        return self.outcome.report_hash

    @property
    def blocked(self) -> bool:
        return not self.allowed


def _minimal_blocked_report(
    request: DataPreflightRequest,
    issues: Sequence[PreflightIssue],
    *,
    provider_key: str | None = None,
) -> DataPreflightReport:
    """Build a valid blocked report before any provider data read."""

    if not issues:
        issues = (
            _issue(
                "provider_contract_violation",
                "预检无法取得数据提供方报告，已阻断回测。",
            ),
        )
    generated_at = getattr(getattr(request, "query_boundary", None), "data_cutoff", None)
    if not isinstance(generated_at, datetime):
        generated_at = datetime.now(timezone.utc)
    logical = request.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
    return DataPreflightReport(
        status=PreflightStatus.BLOCKED,
        generated_at=generated_at,
        provider_key=provider_key or request.provider_key,
        capability_manifest_version=1,
        requested_window=request.requested_window,
        scope_mode=request.instrument_scope_mode,
        resolved_calendar_ids=(),
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
        consistency_token_capability=logical,
        consistency_token_contract=request.consistency_token_contract if logical else None,
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


def _consistency_summary(report: DataPreflightReport) -> dict[str, object]:
    """Build the run-level consistency projection for persisted preflight JSON.

    This is deliberately an operator/persistence projection, not part of the
    canonical report hash.  Values are derived exclusively from the frozen
    report and therefore cannot alter execution semantics or introduce a
    second run-configuration source.
    """
    contract = report.consistency_token_contract
    formal_count = len(report.resolved_sessions)
    chunk_size = report.data_chunk_size_sessions
    chunk_count = (formal_count + chunk_size - 1) // chunk_size if chunk_size else 0
    summary: dict[str, object] = {
        "provider_key": report.provider_key,
        "data_contract_version": report.data_contract_version,
        "consistency_mode": report.consistency_mode.value,
        "consistency_token_contract": (
            {"key": contract.key, "version": contract.version}
            if contract is not None
            else None
        ),
        "max_lookback_sessions": report.max_lookback_sessions,
        "data_chunk_policy": {
            "key": report.data_chunk_policy.key,
            "version": report.data_chunk_policy.version,
        },
        "data_chunk_size_sessions": chunk_size,
        "context_summary": {
            "formal_session_count": formal_count,
            "warmup_session_count": len(report.warmup_sessions),
            "chunk_count": chunk_count,
        },
        "chunk_token_summary": {
            "chunk_count": chunk_count,
            "chunk_size_sessions": chunk_size,
            "covered_chunk_start": 0,
            "covered_chunk_end": chunk_count,
        },
    }
    # Providers may expose a real watermark in session/data revision evidence;
    # preserve it when present and never synthesize a timestamp or hash.
    watermark = None
    for source in (report.session_summary, report.source_revisions):
        if isinstance(source, Mapping):
            for key in ("data_watermark", "watermark", "revision_watermark"):
                if source.get(key) is not None:
                    watermark = source[key]
                    break
        if watermark is not None:
            break
    if watermark is not None:
        summary["data_watermark"] = watermark
    return summary


def _with_report_issues(
    report: DataPreflightReport,
    add: Sequence[PreflightIssue],
    *,
    remove_codes: Iterable[str] = (),
) -> DataPreflightReport:
    """Return a deterministic immutable report with issue changes applied."""

    removed = set(remove_codes)
    candidates = [issue for issue in report.issues if issue.code not in removed]
    by_key: dict[str, PreflightIssue] = {}
    for issue in (*candidates, *add):
        by_key[canonical_json(issue.machine_fields())] = issue
    issues = tuple(sorted(by_key.values(), key=lambda item: item.sort_key))
    blocked = any(item.severity is IssueSeverity.ERROR for item in issues)
    status = (
        PreflightStatus.BLOCKED
        if blocked
        else PreflightStatus.DEGRADED
        if report.status is PreflightStatus.DEGRADED
        else PreflightStatus.READY
    )
    kwargs: dict[str, object] = {
        "status": status,
        "issues": issues,
        "report_hash": "",
    }
    if blocked:
        kwargs.update(
            {
                "resolved_sessions": (),
                "warmup_sessions": (),
                "warmup_resolution": None,
                "warmup_resolution_signature": None,
                "warmup_axis_differences": (),
            }
        )
    return replace(report, **kwargs)


def _attach_scope_evidence(
    report: DataPreflightReport,
    request: DataPreflightRequest,
    *,
    dynamic_scope: Mapping[str, object],
    fixed_ids: Sequence[UUID],
    resolution: object | None = None,
) -> DataPreflightReport:
    """Project task-15 scope evidence onto the existing report DTO.

    ``DataPreflightReport`` gained the universe fields after the original
    report contract.  The field-presence guard keeps this composition layer
    source-compatible with a legacy provider while using the richer fields
    whenever they are available.
    """

    report_fields = DataPreflightReport.__dataclass_fields__
    values: dict[str, object] = {}
    if "non_zero_initial_position_instrument_ids" in report_fields:
        values["non_zero_initial_position_instrument_ids"] = tuple(
            getattr(request, "non_zero_initial_position_instrument_ids", ())
        )
    qualification_policy = getattr(request, "qualification_policy_version", None)
    if "qualification_policy_version" in report_fields:
        values["qualification_policy_version"] = qualification_policy
    if "universe_eligibility_policy_version" in report_fields:
        values["universe_eligibility_policy_version"] = qualification_policy
    if dynamic_scope:
        if "universe_eligibility_summary" in report_fields:
            values["universe_eligibility_summary"] = {
                "status": dynamic_scope.get("status"),
                "resolved_calendar_ids": dynamic_scope.get("resolved_calendar_ids", ()),
                "capability_summary": dynamic_scope.get("capability_summary", {}),
                "source_evidence": dynamic_scope.get("source_evidence", {}),
                "calendar_session_signature": dynamic_scope.get("calendar_session_signature"),
            }
        if "universe_scope_snapshot_hash" in report_fields:
            candidate_hash = dynamic_scope.get("snapshot_hash")
            if isinstance(candidate_hash, str) and len(candidate_hash) == 64:
                values["universe_scope_snapshot_hash"] = candidate_hash
        if "universe_candidate_count" in report_fields:
            candidate_count = dynamic_scope.get("candidate_count")
            if isinstance(candidate_count, int) and not isinstance(candidate_count, bool):
                values["universe_candidate_count"] = candidate_count
        if "universe_filtered_reason_counts" in report_fields:
            reason_counts = dynamic_scope.get("filtered_reason_counts")
            if isinstance(reason_counts, Mapping):
                values["universe_filtered_reason_counts"] = {
                    str(key): int(value)
                    for key, value in reason_counts.items()
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                }
        if (
            "universe_scope_resolution" in report_fields
            and resolution is not None
            and callable(getattr(resolution, "canonical_content", None))
        ):
            values["universe_scope_resolution"] = resolution
    if not values:
        return report
    return replace(report, **values, report_hash="")


def _scope_payload(value: object) -> dict[str, object]:
    """Read a task-15 scope object without importing its concrete implementation."""

    if isinstance(value, Mapping):
        payload = dict(value)
    elif hasattr(value, "as_dict") and callable(value.as_dict):
        payload = dict(value.as_dict())
    else:
        payload = {
            name: getattr(value, name)
            for name in (
                "status",
                "resolved_calendar_ids",
                "calendar_ids",
                "capability_summary",
                "source_evidence",
                "issues",
                "snapshot_hash",
                "calendar_session_signature",
                "scope_mode",
            )
            if hasattr(value, name)
        }
    try:
        normalized = json.loads(canonical_json(payload))
    except (TypeError, ValueError) as exc:
        raise ProviderContractViolationError(
            "dynamic universe scope is not JSON-safe",
            details={"error_type": type(exc).__name__},
        ) from exc
    if not isinstance(normalized, dict):
        raise ProviderContractViolationError("dynamic universe scope must be a JSON object")
    return normalized


def _scope_issue(item: object) -> PreflightIssue:
    """Project a task-15 UniverseScopeIssue into the shared report issue."""

    operator_message = "动态候选范围预检未通过，已阻断回测。"
    if isinstance(item, PreflightIssue):
        details = dict(item.details or {})
        details["upstream_message"] = item.message
        return replace(item, message=operator_message, details=details)
    if isinstance(item, Mapping):
        code = str(item.get("code", "universe_scope_unresolved"))
        upstream_message = str(item.get("message", ""))
        field = item.get("field")
        raw_details = item.get("details", {})
        details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
        if upstream_message.strip():
            details["upstream_message"] = upstream_message
        severity = (
            IssueSeverity.WARNING
            if getattr(item.get("severity"), "value", item.get("severity")) == "warning"
            else IssueSeverity.ERROR
        )
        return _issue(
            code,
            operator_message,
            field=field if isinstance(field, str) else None,
            details=details,
            severity=severity,
        )
    upstream_message = str(getattr(item, "message", ""))
    raw_details = getattr(item, "details", {})
    details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
    if upstream_message.strip():
        details["upstream_message"] = upstream_message
    severity = (
        IssueSeverity.WARNING
        if getattr(getattr(item, "severity", None), "value", getattr(item, "severity", None))
        == "warning"
        else IssueSeverity.ERROR
    )
    return _issue(
        str(getattr(item, "code", "universe_scope_unresolved")),
        operator_message,
        field=getattr(item, "field", None),
        details=details,
        severity=severity,
    )


def _fixture_substitution_removals(
    report: DataPreflightReport,
    fixtures: Sequence[InternalFixture],
) -> tuple[str, ...]:
    """Identify only provider issues explicitly covered by named fixtures.

    Internal substitutes can stand in for actions/status evidence, but never
    for identity, calendar, Bar, rule, account, fee, settlement, or strategy
    permission gates.  Matching is deliberately narrow and based on the
    structured capability/field metadata, never on display text.
    """

    capabilities = {str(item.capability) for item in fixtures}
    removable: set[str] = set()
    for issue in report.issues:
        code = issue.code.upper()
        if code not in {"UNSUPPORTED_CAPABILITY", "CAPABILITY_DECLARATION_INVALID"}:
            continue
        details = issue.details if isinstance(issue.details, Mapping) else {}
        capability = str(details.get("capability", "")).lower()
        field = str(issue.field or "").lower()
        if (
            (capability in {"actions", "corporate_actions", "quantity_action_coverage"} or "action" in field)
            and FIXTURE_QUANTITY_ACTIONS in capabilities
        ) or (
            (capability in {"status", "trading_status"} or "status" in field)
            and FIXTURE_TRADING_STATUS in capabilities
        ):
            removable.add(issue.code)
    return tuple(sorted(removable))


def _bind_quantity_action_integrity(
    report: DataPreflightReport,
    request: DataPreflightRequest,
    fixtures: Sequence[InternalFixture],
) -> DataPreflightReport:
    """Bind explicit quantity-action evidence without inferring it.

    Cash-action coverage is not a substitute for split, consolidation, or
    share-change coverage.  Only a provider-supplied integrity object or the
    named internal fixture is allowed to populate this field.
    """

    if DataCapability.ACTIONS not in request.required_capabilities:
        return report
    if report.quantity_action_integrity is not None:
        return report
    fixture = next(
        (
            item
            for item in fixtures
            if str(item.capability) == FIXTURE_QUANTITY_ACTIONS
        ),
        None,
    )
    if fixture is None:
        return report
    return replace(
        report,
        quantity_action_integrity={
            "status": "complete",
            "source": fixture.source,
            "fixture_key": fixture.fixture_key,
            "fixture_version": fixture.fixture_version,
            "content_hash": fixture.content_hash,
            "scope": fixture.scope,
            "proof_summary": fixture.proof_summary,
        },
        report_hash="",
    )


def _post_report_gate_issues(
    report: DataPreflightReport,
    request: DataPreflightRequest,
    fixtures: Sequence[InternalFixture],
) -> tuple[PreflightIssue, ...]:
    """Apply only request-level gates that need the provider report.

    This is still orchestration, not a Bar/adjustment algorithm: it checks
    the provider-declared report state and never derives a value or repairs a
    missing series.
    """

    issues: list[PreflightIssue] = []
    fixture_caps = {str(item.capability) for item in fixtures}
    if DataCapability.ACTIONS in request.required_capabilities:
        integrity = report.quantity_action_integrity
        if not isinstance(integrity, Mapping) or integrity.get("status") != "complete":
            issues.append(
                _issue(
                    "quantity_action_integrity_incomplete",
                    "请求要求数量类公司行动完整性证明，但报告未提供独立、完整的来源和覆盖证据，已阻断回测。",
                    field="quantity_action_integrity",
                    details={
                        "required": True,
                        "actual_status": integrity.get("status") if isinstance(integrity, Mapping) else None,
                    },
                )
            )
    non_raw = [item for item in request.strategy_price_bases if getattr(item, "value", item) in {"qfq", "hfq"}]
    if non_raw:
        if request.adjustment_series_policy is None or report.adjustment_policy_status != "active":
            issues.append(
                _issue(
                    "unsupported_capability",
                    "请求的复权序列未处于 active 验证状态，已阻断回测；raw 请求不受影响。",
                    field="adjustment_series_policy",
                    details={"requested_price_bases": [getattr(item, "value", str(item)) for item in non_raw]},
                )
            )
    return tuple(issues)


def _source_revision_audit_issues(
    report: DataPreflightReport,
    request: DataPreflightRequest,
    profile: PreflightProfile,
    fixtures: Sequence[InternalFixture],
) -> tuple[PreflightIssue, ...]:
    """Enforce source-revision audit qualification for consumed Bar facts."""
    consumed = [c for c in report.coverage_reports if c.capability is DataCapability.BARS]
    if not consumed:
        return ()
    summary = report.data_revision_summary
    daily = summary.get("daily_bars") if isinstance(summary, Mapping) else None
    production_ok = isinstance(daily, Mapping) and isinstance(daily.get("audit"), Mapping) and daily["audit"].get("evidence_class") == "production_audit" and daily["audit"].get("status") == "complete"
    revision_ok = isinstance(daily, Mapping) and int(daily.get("missing_revision_count", 1) or 1) == 0
    fixture_ok = False
    fixture_failures: list[Mapping[str, object]] = []
    # The source-revision fixture is deliberately validated here as an
    # evidence object, rather than merely by name.  This gate runs after the
    # provider report is frozen and therefore has the complete request scope
    # available for comparison.
    forbidden = ("token", "secret", "password", "credential")

    def _contains_sensitive(value: object) -> bool:
        if isinstance(value, Mapping):
            return any(
                any(word in str(key).lower() for word in forbidden)
                or _contains_sensitive(item)
                for key, item in value.items()
            )
        if isinstance(value, (tuple, list, set, frozenset)):
            return any(_contains_sensitive(item) for item in value)
        return any(word in str(value).lower() for word in forbidden)

    def _scope_dates(fixture: InternalFixture) -> tuple[date, date]:
        starts = [fixture.start_date]
        ends = [fixture.end_date]
        scope = fixture.scope if isinstance(fixture.scope, Mapping) else {}
        for key in ("formal_envelope", "warmup_envelope", "history_envelope"):
            envelope = scope.get(key)
            if isinstance(envelope, Mapping):
                try:
                    starts.append(date.fromisoformat(str(envelope["start_date"])))
                    ends.append(date.fromisoformat(str(envelope["end_date"])))
                except (KeyError, TypeError, ValueError):
                    fixture_failures.append({"reason": "invalid_date_range", "field": key})
        return min(starts), max(ends)

    for fixture in fixtures:
        ref = f"{fixture.fixture_key}@{fixture.fixture_version}"
        if ref != "source_revision_audit@1":
            continue
        if str(getattr(fixture, "capability", "")) not in {FIXTURE_SOURCE_REVISIONS, "source_revision_audit"}:
            fixture_failures.append({"reason": "capability_mismatch"})
            continue
        proof = getattr(fixture, "proof_summary", None)
        content_hash = str(getattr(fixture, "content_hash", ""))
        valid_hash = len(content_hash) == 64 and all(c in "0123456789abcdefABCDEF" for c in content_hash)
        start, end = _scope_dates(fixture)
        required_ids = set(getattr(request, "static_instrument_ids", ())) | set(
            getattr(request, "mandatory_instrument_ids", ())
        )
        ids_ok = not required_ids or required_ids.issubset(set(getattr(fixture, "instrument_ids", ())))
        required_ranges = [request.requested_window]
        for attr in ("formal_envelope", "warmup_envelope", "history_envelope"):
            envelope = getattr(request, attr, None)
            if envelope is not None and hasattr(envelope, "start_date") and hasattr(envelope, "end_date"):
                required_ranges.append(envelope)
        dates_ok = start <= min(item.start_date for item in required_ranges) and end >= max(
            item.end_date for item in required_ranges
        )
        proof_ok = (isinstance(proof, Mapping) and bool(proof)) or (isinstance(proof, str) and bool(proof.strip()))
        sensitive_ok = not _contains_sensitive(fixture.machine_content())
        # A fixture must never masquerade as a production audit record.
        production_claim = (
            isinstance(proof, Mapping)
            and str(proof.get("evidence_class", "")).lower() == "production_audit"
        ) or str(getattr(fixture, "source", "")).lower() in {"production", "production_audit"}
        substituted = proof.get("substituted_capability") if isinstance(proof, Mapping) else None
        capability_claim_ok = substituted is None or str(substituted).lower() in {
            FIXTURE_SOURCE_REVISIONS,
            "source_revision_audit",
            "source_revisions",
        }
        fixture_ok = bool(
            getattr(fixture, "fixture_only", False) is True
            and getattr(fixture, "source", None) == "internal_fixture"
            and proof_ok and valid_hash and ids_ok and dates_ok and sensitive_ok
            and not production_claim and capability_claim_ok
        )
        if not fixture_ok:
            fixture_failures.append({"reason": "invalid_fixture_scope_or_proof", "fixture_key": fixture.fixture_key})
    if production_ok and revision_ok:
        return ()
    if profile.reference == FORMAL_PROFILE:
        if not summary:
            code = "source_revision_audit_missing"
        elif not revision_ok or not isinstance(daily, Mapping) or not daily.get("audit"):
            code = "source_revision_audit_incomplete"
        else:
            code = "source_revision_audit_not_production"
        return (_issue(code, "来源修订审计证据不满足 formal 生产要求，已阻断回测。", field="source_revisions"),)
    if fixture_ok or production_ok:
        return ()
    return (_issue("source_revision_audit_missing", "来源修订审计证据缺失，且未提供合格 source_revision_audit@1 fixture，已阻断回测。", field="source_revisions", details={"fixture_failures": fixture_failures}),)


def _pit_gate_issues(
    report: DataPreflightReport,
    request: DataPreflightRequest,
) -> tuple[PreflightIssue, ...]:
    """Validate the report's PIT declaration against the frozen boundary.

    ``data_cutoff`` limits valid time.  ``knowledge_as_of`` is a stronger
    cognition-time contract: a fact family without usable ``known_at`` data
    cannot be silently consumed as strict historical cognition.
    """

    boundary = request.query_boundary
    issues: list[PreflightIssue] = []
    if report.knowledge_as_of != boundary.knowledge_as_of:
        issues.append(
            _issue(
                "pit_boundary_mismatch",
                "预检报告的 knowledge_as_of 与冻结查询边界不一致，已阻断回测。",
                field="knowledge_as_of",
                details={
                    "expected": boundary.knowledge_as_of.isoformat() if boundary.knowledge_as_of else None,
                    "actual": report.knowledge_as_of.isoformat() if report.knowledge_as_of else None,
                },
            )
        )
    non_strict = tuple(report.non_strict_pit_capabilities)
    if boundary.knowledge_as_of is not None and non_strict:
        issues.append(
            _issue(
                "strict_pit_unavailable",
                "请求要求严格历史认知，但以下事实缺少可验证的认知时点证据，已阻断回测。",
                field="non_strict_pit_capabilities",
                details={
                    "knowledge_as_of": boundary.knowledge_as_of.isoformat(),
                    "capabilities": tuple(item.value for item in non_strict),
                },
            )
        )
    return tuple(issues)


def _formal_capability_issues(
    report: DataPreflightReport,
    request: DataPreflightRequest,
) -> tuple[PreflightIssue, ...]:
    """Enforce production-only gates for ``formal@1``.

    The orchestration layer consumes declarations produced by the provider;
    it never creates token or fact records itself.  Transitional repeatable
    read is intentionally rejected for formal runs because it cannot provide
    the block-level consistency evidence required by the production profile.
    """

    issues: list[PreflightIssue] = []
    if request.consistency_mode is not ConsistencyMode.CHUNKED_LOGICAL_TOKEN:
        issues.append(
            _issue(
                "formal_consistency_contract_unavailable",
                "formal@1 必须使用一致性 token 模式，生产一致性能力不可用，已阻断回测。",
                field="consistency_mode",
            )
        )
    elif not report.consistency_token_capability or report.consistency_token_contract is None:
        issues.append(
            _issue(
                "formal_consistency_contract_unavailable",
                "formal@1 的一致性 token 契约或覆盖能力缺失，已阻断回测。",
                field="consistency_token_capability",
            )
        )
    # Production source-revision and capability-21 evidence is represented by
    # the provider's structured summary.  Missing summaries remain blocked;
    # no empty/default payload is treated as proof.
    summary = report.session_summary if isinstance(report.session_summary, Mapping) else None
    if summary is not None:
        production = summary.get("production_capabilities")
        if production is not None and (not isinstance(production, Mapping) or production.get("status") != "complete"):
            issues.append(
                _issue(
                    "formal_unavailable_capability",
                    "正式生产能力清单未完成，已阻断回测。",
                    field="capability_manifest",
                )
            )
    return tuple(issues)


def _fixture_session_scope_issues(
    report: DataPreflightReport,
    fixtures: Sequence[InternalFixture],
    fixed_ids: Sequence[UUID],
) -> tuple[PreflightIssue, ...]:
    """Check fixture ranges against the resolved formal and warmup sessions."""

    if not fixtures:
        return ()
    points = [
        item.session_date
        for item in (*report.resolved_sessions, *report.warmup_sessions)
    ]
    if not points:
        return ()
    earliest, latest = min(points), max(points)
    issues: list[PreflightIssue] = []
    for fixture in fixtures:
        if fixture.start_date > earliest or fixture.end_date < latest:
            issues.append(
                _issue(
                    "internal_preflight_fixture_out_of_scope",
                    "内部 fixture 未完整覆盖已解析的正式/warmup 会话范围，已阻断回测。",
                    field="fixture_scope",
                    details={
                        "fixture_key": fixture.fixture_key,
                        "fixture_version": fixture.fixture_version,
                        "required_start_date": earliest,
                        "required_end_date": latest,
                        "fixture_start_date": fixture.start_date,
                        "fixture_end_date": fixture.end_date,
                        "fixed_instrument_ids": [str(item) for item in fixed_ids],
                    },
                )
            )
    return tuple(issues)


def _initial_issue(item: object) -> PreflightIssue:
    """Project an existing initial-position issue without reimplementing it."""

    instrument_id = getattr(item, "instrument_id", None)
    if not isinstance(instrument_id, UUID):
        instrument_id = None
    code = str(getattr(item, "code", "initial_position_preflight_blocked"))
    message = str(getattr(item, "message", "初始持仓预检未通过，已阻断回测。"))
    return _issue(
        code,
        f"初始持仓预检未通过：{message}",
        field=getattr(item, "field", None),
        scope="initial_positions",
        instrument_id=instrument_id,
        details={
            "initial_position_issue_code": code,
            "instrument_id": str(instrument_id) if instrument_id else None,
        },
    )


def _rule_status_requirement_issue(
    request: DataPreflightRequest | DataRequest,
    rule_preflight_report: object | None,
) -> PreflightIssue | None:
    """Validate request STATUS against an already-frozen rule snapshot.

    This check intentionally runs before any provider, manifest, calendar,
    or status-fact read.  A request cannot use a client-selected STATUS bit
    to override the applicability declared by its point-in-time rule
    segments; disagreement is a hard, deterministic contract failure.
    """

    if rule_preflight_report is None:
        return None
    bundle = getattr(rule_preflight_report, "snapshot_bundle", None)
    if bundle is None:
        # A blocked report is handled by the existing rule/fixed gate.  It is
        # important not to reinterpret its lack of a bundle as all N/A.
        return None
    dimensions = getattr(
        rule_preflight_report, "required_trading_status_dimensions", None
    )
    if dimensions is None:
        dimensions = {
            dimension
            for segment in getattr(bundle, "instrument_segments", ())
            for dimension, requirement in getattr(
                segment, "capability_declarations", {}
            ).items()
            if requirement == "required"
        }
    try:
        required_dimensions = tuple(sorted(set(dimensions)))
    except (TypeError, ValueError):
        # A malformed report cannot prove a capability decision.  Leave its
        # own rule admission error to block the request rather than inventing
        # an applicability result here.
        return None
    actual = DataCapability.STATUS in request.required_capabilities
    expected = bool(required_dimensions)
    if actual == expected:
        return None
    return _issue(
        "trading_status_capability_requirement_mismatch",
        "请求能力与冻结规则中的交易状态适用性不一致，已在数据提供方读取前阻断回测。",
        field="required_capabilities",
        details={
            "reason_code": "trading_status_capability_requirement_mismatch",
            "required_status_dimensions": required_dimensions,
            "expected_status": expected,
            "actual_status": actual,
            "required_capabilities": tuple(
                item.value for item in request.required_capabilities
            ),
            "rule_snapshot_hash": getattr(rule_preflight_report, "snapshot_hash", None),
        },
    )


class DataPreflightService:
    """Compose Phase 2a preflight facts and enforce page/session gates."""

    def __init__(
        self,
        provider: object | None = None,
        *,
        profile: PreflightProfile | ContractRef | str = INTERNAL_LINK_ACCEPTANCE_PROFILE,
        profile_registry: PreflightProfileRegistry | None = None,
        fixture_registry: Mapping[str, tuple[int, str]] | None = None,
    ) -> None:
        self.provider = provider
        self.profile_registry = profile_registry or PreflightProfileRegistry()
        self.profile = self.resolve_profile(profile)
        self.fixture_registry = dict(fixture_registry or INTERNAL_FIXTURE_REGISTRY)

    # ------------------------------------------------------------------
    # Context and profile helpers
    # ------------------------------------------------------------------

    def resolve_profile(
        self, profile: PreflightProfile | ContractRef | str | None
    ) -> PreflightProfile:
        """Resolve only an exact registered profile reference."""

        if profile is None:
            return self.profile_registry.resolve(INTERNAL_LINK_ACCEPTANCE_PROFILE)
        try:
            if isinstance(profile, PreflightProfile):
                candidate = self.profile_registry.resolve(profile.reference)
            elif isinstance(profile, (ContractRef, str)):
                candidate = self.profile_registry.resolve(profile)
            else:
                raise InvalidDataRequestError("preflight profile must be versioned")
        except (InvalidDataRequestError, TypeError, ValueError) as exc:
            raise PreflightProfileMismatchError(
                "unsupported preflight profile",
                details={"profile": str(profile)},
            ) from exc
        if candidate.reference not in {INTERNAL_LINK_ACCEPTANCE_PROFILE, FORMAL_PROFILE}:
            raise PreflightProfileMismatchError(
                "only internal_link_acceptance@1 and formal@1 are registered in Phase 2a",
                details={"profile": candidate.profile},
            )
        expected_kind = (
            INTERNAL_LINK_ACCEPTANCE_RUN_KIND
            if candidate.reference == INTERNAL_LINK_ACCEPTANCE_PROFILE
            else FORMAL_RUN_KIND
        )
        if candidate.run_kind != expected_kind:
            raise PreflightProfileMismatchError(
                "profile run_kind does not match its registered profile",
                details={
                    "profile": candidate.profile,
                    "expected_run_kind": expected_kind,
                    "actual_run_kind": candidate.run_kind,
                },
            )
        return candidate

    def register_fixture(
        self,
        fixture_key: str,
        *,
        version: int,
        capability: str,
    ) -> None:
        """Register one exact fixture key for a controlled internal setup."""

        if not isinstance(fixture_key, str) or not fixture_key.strip():
            raise InvalidDataRequestError("fixture_key must be non-blank")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise InvalidDataRequestError("fixture version must be positive")
        if capability not in INTERNAL_FIXTURE_CAPABILITIES:
            raise InternalFixtureContractError(
                "fixture capability is outside the internal profile",
                details={"capability": capability},
            )
        self.fixture_registry[f"{fixture_key.strip()}@{version}"] = (version, capability)

    def _context(self, value: object, **overrides: object) -> PreflightContext:
        """Accept either a typed context or a direct request."""

        if isinstance(value, PreflightContext):
            payload = {field.name: getattr(value, field.name) for field in fields(PreflightContext)}
        elif isinstance(value, (DataPreflightRequest, DataRequest)):
            payload = {"request": value}
        elif isinstance(value, Mapping):
            payload = dict(value)
        else:
            request = getattr(value, "request", getattr(value, "data_request", None))
            if request is None:
                raise InvalidDataRequestError("preflight context must carry request")
            payload = {
                "request": request,
                **{
                    name: getattr(value, name)
                    for name in (
                        "provider",
                        "session",
                        "profile",
                        "run_kind",
                        "fixtures",
                        "spec",
                        "initial_position_gateway",
                        "dynamic_scope_resolver",
                        "calendar_resolver",
                        "coverage_qualifier",
                        "rule_preflight_report",
                        "base_report",
                    )
                    if hasattr(value, name)
                },
            }
        if "preflight_profile" in payload and "profile" not in payload:
            payload["profile"] = payload.pop("preflight_profile")
        if "data_provider" in payload and "provider" not in payload:
            payload["provider"] = payload.pop("data_provider")
        if "data_session" in payload and "session" not in payload:
            payload["session"] = payload.pop("data_session")
        if "initial_spec" in payload and "spec" not in payload:
            payload["spec"] = payload.pop("initial_spec")
        payload.update({key: item for key, item in overrides.items() if item is not None})
        if "preflight_profile" in payload and "profile" not in payload:
            payload["profile"] = payload.pop("preflight_profile")
        if "data_provider" in payload and "provider" not in payload:
            payload["provider"] = payload.pop("data_provider")
        if "data_session" in payload and "session" not in payload:
            payload["session"] = payload.pop("data_session")
        if "initial_spec" in payload and "spec" not in payload:
            payload["spec"] = payload.pop("initial_spec")
        payload.setdefault("provider", self.provider)
        payload.setdefault("profile", self.profile)
        payload.setdefault("fixtures", ())
        return PreflightContext(**payload)

    def _bound_profile(self, context: PreflightContext) -> PreflightProfile:
        profile = self.resolve_profile(context.profile or self.profile)
        if context.run_kind is not None and context.run_kind != profile.run_kind:
            raise PreflightProfileMismatchError(
                "run_kind is fixed by the server-side profile",
                details={
                    "expected_run_kind": profile.run_kind,
                    "actual_run_kind": context.run_kind,
                    "preflight_profile": _profile_text(profile),
                },
            )
        return profile

    @staticmethod
    def _fixed_ids(context: PreflightContext) -> tuple[UUID, ...]:
        request = context.request
        values: list[UUID] = [
            *request.static_instrument_ids,
            *request.mandatory_instrument_ids,
            *getattr(request, "non_zero_initial_position_instrument_ids", ()),
        ]
        if context.spec is not None:
            values.extend(
                position.instrument_id
                for position in context.spec.non_zero_initial_positions
            )
        return tuple(sorted(set(values), key=str))

    def fixed_instrument_ids(
        self,
        value: PreflightContext | DataPreflightRequest | DataRequest | object,
        *,
        spec: BacktestSpec | None = None,
    ) -> tuple[UUID, ...]:
        """Return the stable fixed union without performing a provider read."""

        context = self._context(value, spec=spec)
        return self._fixed_ids(context)

    @staticmethod
    def _fixtures(context: PreflightContext) -> tuple[InternalFixture, ...]:
        result: list[InternalFixture] = []
        for item in context.fixtures:
            if isinstance(item, InternalFixture):
                result.append(item)
                continue
            if not isinstance(item, Mapping):
                raise InternalFixtureContractError(
                    "internal fixture is not a typed fixture or mapping"
                )
            try:
                result.append(InternalFixture(**dict(item)))
            except (TypeError, ValueError, DataContractError) as exc:
                raise InternalFixtureContractError(
                    "internal fixture is invalid",
                    details={"error_type": type(exc).__name__},
                ) from exc
        return tuple(result)

    @staticmethod
    def _consumed_fixtures(
        profile: PreflightProfile,
        request: DataPreflightRequest | DataRequest,
        fixtures: Sequence[InternalFixture],
    ) -> tuple[InternalFixture, ...]:
        """Keep only fixture evidence consumed by this request.

        A shared internal fixture bundle may contain trading-status evidence
        for another capability path.  When STATUS is absent from the frozen
        request, that evidence must not become a report source, a fixture
        gate, or a qualification-hash input.  Formal profiles retain the
        original tuple so their existing fixture-forbidden gate still fails
        closed rather than silently accepting an attached fixture.
        """

        if (
            not profile.allow_fixture_only
            or DataCapability.STATUS in request.required_capabilities
        ):
            return tuple(fixtures)
        return tuple(
            fixture
            for fixture in fixtures
            if str(getattr(fixture.capability, "value", fixture.capability))
            != FIXTURE_TRADING_STATUS
        )

    def _fixture_issues(
        self,
        profile: PreflightProfile,
        fixtures: Sequence[InternalFixture],
        request: DataPreflightRequest,
        fixed_ids: Sequence[UUID],
    ) -> tuple[PreflightIssue, ...]:
        """Check exact references and complete request scope coverage."""

        issues: list[PreflightIssue] = []
        if fixtures and not _profile_allows_fixtures(profile):
            issues.append(
                _issue(
                    "internal_preflight_profile_mismatch",
                    "正式 profile 不接受 fixture-only 内部事实，已阻断回测。",
                    field="fixtures",
                    details={"preflight_profile": _profile_text(profile)},
                )
            )
            return tuple(issues)
        for fixture in fixtures:
            capability = str(fixture.capability)
            reference = f"{fixture.fixture_key}@{fixture.fixture_version}"
            registered = self.fixture_registry.get(reference)
            version_number = getattr(fixture, "version_number", fixture.fixture_version)
            try:
                version_number = int(version_number)
            except (TypeError, ValueError):
                version_number = -1
            if registered != (version_number, capability):
                issues.append(
                    _issue(
                        "internal_preflight_fixture_missing",
                        "内部 fixture 未按 profile 精确注册，已阻断回测。",
                        field="fixture_key",
                        details={
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                            "capability": capability,
                        },
                    )
                )
            if capability not in _profile_allowed_capabilities(profile):
                issues.append(
                    _issue(
                        "internal_preflight_fixture_out_of_scope",
                        "内部 fixture 能力不在当前 profile 范围内，已阻断回测。",
                        field="capability",
                        details={"fixture_key": fixture.fixture_key, "capability": capability},
                    )
                )
            allowed_refs = tuple(getattr(profile, "allowed_fixture_references", ()))
            if allowed_refs:
                try:
                    reference = ContractRef(fixture.fixture_key, version_number)
                except Exception:
                    reference = None
                if reference not in allowed_refs:
                    issues.append(
                        _issue(
                            "internal_preflight_fixture_out_of_scope",
                            "内部 fixture 引用不在当前 profile 注册范围内，已阻断回测。",
                            field="fixture_key",
                            details={
                                "fixture_key": fixture.fixture_key,
                                "fixture_version": fixture.fixture_version,
                            },
                        )
                    )
            scoped_ids = set(fixture.instrument_ids)
            # An empty ``instrument_ids`` set is an explicitly global scope
            # (the request-layer fixture contract permits this when a scope
            # mapping is supplied).  Only a non-empty set is checked against
            # fixed objects; absence is never inferred as proof elsewhere.
            if scoped_ids and not set(fixed_ids).issubset(scoped_ids):
                issues.append(
                    _issue(
                        "internal_preflight_fixture_out_of_scope",
                        "内部 fixture 的标的范围未完整覆盖固定对象，已阻断回测。",
                        field="fixture_scope",
                        details={
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                            "missing_instrument_ids": [
                                str(item)
                                for item in sorted(set(fixed_ids) - scoped_ids, key=str)
                            ],
                        },
                    )
                )
            if fixture.start_date > request.requested_window.start_date or fixture.end_date < request.requested_window.end_date:
                issues.append(
                    _issue(
                        "internal_preflight_fixture_out_of_scope",
                        "内部 fixture 的日期范围未完整覆盖请求，已阻断回测。",
                        field="fixture_scope",
                        details={
                            "fixture_key": fixture.fixture_key,
                            "fixture_version": fixture.fixture_version,
                            "requested_start_date": request.requested_window.start_date,
                            "requested_end_date": request.requested_window.end_date,
                            "fixture_start_date": fixture.start_date,
                            "fixture_end_date": fixture.end_date,
                        },
                    )
                )
        return tuple(issues)

    @staticmethod
    def _required_fixture_issues(
        profile: PreflightProfile,
        request: DataPreflightRequest,
        provider: object | None,
        fixtures: Sequence[InternalFixture],
    ) -> tuple[PreflightIssue, ...]:
        """Require named substitutes when a provider lacks an applicable gate."""

        if not profile.allow_fixture_only:
            return ()
        needs_capability_manifest = (
            DataCapability.ACTIONS in request.required_capabilities
            or DataCapability.STATUS in request.required_capabilities
            or request.consistency_mode is ConsistencyMode.TRANSITIONAL_REPEATABLE_READ
        )
        if not needs_capability_manifest:
            # A request that needs neither an optional fact family nor a
            # consistency-mode substitute has no reason to inspect the
            # provider manifest.  In particular, an ETF request whose frozen
            # rule segments are all ``not_applicable`` must not turn an
            # unavailable STATUS manifest into a preflight dependency.
            return ()
        declared: set[str] = set()
        modes: set[ConsistencyMode] = set()
        manifest_method = getattr(provider, "capability_manifest", None)
        if callable(manifest_method):
            try:
                manifest = manifest_method()
                declared = {
                    str(item.value if isinstance(item, DataCapability) else item)
                    for item in getattr(manifest, "capabilities", ())
                }
                modes = set(getattr(manifest, "consistency_modes", ()))
            except Exception:
                # The provider preflight below will produce the authoritative
                # contract error; this helper only identifies missing fixtures.
                pass
        fixture_caps = {str(item.capability) for item in fixtures}
        required: list[tuple[DataCapability | None, str, str]] = []
        if DataCapability.ACTIONS in request.required_capabilities:
            required.append((DataCapability.ACTIONS, FIXTURE_QUANTITY_ACTIONS, "actions"))
        if DataCapability.STATUS in request.required_capabilities:
            required.append((DataCapability.STATUS, FIXTURE_TRADING_STATUS, "status"))
        if request.consistency_mode is ConsistencyMode.TRANSITIONAL_REPEATABLE_READ and modes and request.consistency_mode not in modes:
            required.append((None, FIXTURE_REPEATABLE_READ, "transitional_repeatable_read"))
        issues: list[PreflightIssue] = []
        for capability, fixture_capability, label in required:
            if capability is not None and capability.value in declared:
                continue
            if fixture_capability not in fixture_caps:
                issues.append(
                    _issue(
                        "internal_preflight_fixture_missing",
                        f"Provider 未提供 {label} 能力，且缺少具名内部 fixture，已阻断回测。",
                        field="fixtures",
                        details={
                            "capability": capability.value if capability is not None else label,
                            "required_fixture_capability": fixture_capability,
                        },
                    )
                )
        return tuple(issues)

    def _resolve_scope(
        self,
        context: PreflightContext,
        provider: object | None,
    ) -> tuple[dict[str, object], tuple[PreflightIssue, ...], object | None]:
        """Consume task-15 scope resolution without enumerating candidates."""

        request = context.request
        if request.instrument_scope_mode is InstrumentScopeMode.FIXED:
            return {}, (), None
        try:
            if context.dynamic_scope_resolver is not None:
                resolver = context.dynamic_scope_resolver
                method = resolver if callable(resolver) else getattr(resolver, "resolve", None)
                if not callable(method):
                    raise ProviderContractViolationError("dynamic scope resolver is not callable")
                try:
                    parameters = inspect.signature(method).parameters
                except (TypeError, ValueError):
                    parameters = {}
                if "request" in parameters and parameters["request"].kind is inspect.Parameter.KEYWORD_ONLY:
                    result = method(request=request)
                else:
                    result = method(request)
            else:
                result = resolve_dynamic_universe_scope(
                    request,
                    provider,
                    fixed_calendar_ids=(),
                    initial_position_calendar_ids=(),
                    calendar_resolver=context.calendar_resolver,
                )
            payload = _scope_payload(result)
        except DataContractError as exc:
            return {}, (
                _issue(
                    getattr(exc, "code", "universe_provider_contract_violation"),
                    "动态范围解析能力读取失败，已阻断回测。",
                    field="universe_query_policy",
                    details={"error_type": type(exc).__name__, **dict(getattr(exc, "details", {}) or {})},
                ),
            ), None
        except Exception as exc:
            return {}, (
                _issue(
                    "universe_provider_contract_violation",
                    "动态范围解析能力发生未定义错误，已阻断回测。",
                    field="universe_query_policy",
                    details={"error_type": type(exc).__name__},
                ),
            ), None
        issues: list[PreflightIssue] = []
        raw_issues = payload.get("issues", ())
        if isinstance(raw_issues, (list, tuple)):
            issues.extend(_scope_issue(item) for item in raw_issues)
        status = str(payload.get("status", "ready")).lower()
        calendar_ids = payload.get("resolved_calendar_ids", payload.get("calendar_ids", ()))
        if isinstance(calendar_ids, str) or not isinstance(calendar_ids, Iterable):
            calendar_ids = ()
        try:
            from app.backtesting.calendar_axis import normalize_calendar_id

            normalized_ids = tuple(
                sorted(
                    {
                        normalize_calendar_id(str(item))
                        for item in calendar_ids
                        if str(item).strip()
                    }
                )
            )
        except Exception as exc:
            issues.append(
                _issue(
                    "universe_scope_unresolved",
                    "动态范围返回的日历标识无效，已阻断回测。",
                    field="resolved_calendar_ids",
                    details={"error_type": type(exc).__name__},
                )
            )
            normalized_ids = ()
        if status not in {"ready", "compatible", "ok"} and not issues:
            issues.append(
                _issue(
                    "universe_scope_unresolved",
                    "动态范围预检未就绪，已阻断回测。",
                    field="universe_query_policy",
                )
            )
        if status in {"ready", "compatible", "ok"} and not payload.get(
            "calendar_session_signature"
        ):
            issues.append(
                _issue(
                    "universe_scope_unresolved",
                    "动态范围缺少正式区间日历兼容性证明，已阻断回测。",
                    field="calendar_session_signature",
                )
            )
        if not normalized_ids:
            issues.append(
                _issue(
                    "universe_scope_unresolved",
                    "动态范围未返回有限具名日历集合，已阻断回测。",
                    field="resolved_calendar_ids",
                )
            )
        payload["resolved_calendar_ids"] = normalized_ids
        return payload, tuple(issues), result

    def resolve_dynamic_scope(
        self,
        value: PreflightContext | DataPreflightRequest | DataRequest | object,
        **overrides: object,
    ) -> Mapping[str, object]:
        """Expose task-15 scope evidence as a read-only service helper."""

        context = self._context(value, **overrides)
        payload, issues, _ = self._resolve_scope(context, context.provider or self.provider)
        if issues:
            payload = dict(payload)
            payload["issues"] = tuple(issue.as_dict() for issue in issues)
            payload["status"] = "blocked"
        return MappingProxyType(
            json.loads(canonical_json(payload))
        )

    resolve_universe_scope = resolve_dynamic_scope

    @staticmethod
    def _provider_report(
        provider: object | None,
        request: DataPreflightRequest,
        *,
        session: object | None,
        authoritative: bool,
        dynamic_calendar_ids: Sequence[str],
    ) -> DataPreflightReport:
        """Call one existing provider/session preflight method only."""

        target = session if authoritative and session is not None else provider
        if target is None:
            raise ProviderContractViolationError("preflight provider is not configured")
        method = getattr(target, "preflight", None)
        if not callable(method):
            raise ProviderContractViolationError("preflight provider has no preflight method")
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        # Signature inspection decides the call shape before invocation.  Do
        # not catch a provider's own TypeError and retry: doing so could issue
        # two reads and violate the one-authoritative-preflight boundary.
        if "resolved_calendar_ids" in parameters:
            report = method(request, resolved_calendar_ids=tuple(dynamic_calendar_ids))
        elif "frozen_calendar_ids" in parameters and authoritative:
            report = method(request, frozen_calendar_ids=tuple(dynamic_calendar_ids))
        else:
            report = method(request)
        if not isinstance(report, DataPreflightReport):
            report = getattr(report, "report", None)
        if not isinstance(report, DataPreflightReport):
            raise ProviderContractViolationError("provider preflight returned an invalid report")
        return report

    def _qualification_issues(
        self,
        context: PreflightContext,
        report: DataPreflightReport,
        profile: PreflightProfile,
        fixtures: Sequence[InternalFixture] = (),
    ) -> tuple[PreflightIssue, ...]:
        """Ask an injected task-16A qualification port for fixed objects."""

        qualifier = context.coverage_qualifier
        if qualifier is None:
            return ()
        method = qualifier if callable(qualifier) else getattr(qualifier, "qualify_instrument", None)
        method_name = "qualify_instrument" if callable(method) else "qualify"
        if not callable(method):
            method = getattr(qualifier, "qualify", None)
        if not callable(method):
            return (
                _issue(
                    "coverage_provider_contract_violation",
                    "覆盖资格能力不可调用，已阻断回测。",
                    field="coverage_qualifier",
                ),
            )
        from app.backtesting.data.requests import CoverageQualificationRequest

        issues: list[PreflightIssue] = []
        fixture_values = tuple(fixtures)
        for instrument_id in self._fixed_ids(context):
            request = context.request
            qualification_request = CoverageQualificationRequest(
                instrument_id=instrument_id,
                effective_date=request.requested_window.start_date,
                requested_window=request.requested_window,
                formal_envelope=request.requested_window,
                warmup_envelope=None,
                history_envelope=None,
                required_capabilities=request.required_capabilities,
                query_boundary=request.query_boundary,
                preflight_profile=profile.reference,
                resolved_calendar_ids=tuple(report.resolved_calendar_ids),
                rule_package=request.rule_package,
                rule_exception_set=request.rule_exception_set,
                market_scope=request.market_scope,
                universe_query_policy=request.universe_query_policy,
                qualification_policy_version=(
                    getattr(request, "qualification_policy_version", None)
                    if isinstance(
                        getattr(request, "qualification_policy_version", None),
                        ContractRef,
                    )
                    else None
                ),
                fixtures=tuple(fixture_values),
                frequency=request.frequency,
            )
            try:
                if method_name == "qualify_instrument":
                    result = method(
                        instrument_id=qualification_request.instrument_id,
                        effective_date=qualification_request.effective_date,
                        requested_window=qualification_request.requested_window,
                        required_capabilities=qualification_request.required_capabilities,
                        query_boundary=qualification_request.query_boundary,
                        resolved_calendar_ids=qualification_request.resolved_calendar_ids,
                        preflight_profile=qualification_request.preflight_profile,
                        formal_envelope=qualification_request.formal_envelope,
                        warmup_envelope=qualification_request.warmup_envelope,
                        history_envelope=qualification_request.history_envelope,
                    )
                else:
                    result = method(qualification_request)
            except TypeError:
                # Early adapters exposed ``qualify(instrument_id, request)``;
                # this compatibility call keeps the same frozen values and
                # does not broaden the qualification scope.
                result = method(instrument_id, qualification_request)
            except Exception as exc:
                issues.append(
                    _issue(
                        "coverage_provider_contract_violation",
                        "覆盖资格能力读取失败，已阻断回测。",
                        field="coverage_qualifier",
                        instrument_id=instrument_id,
                        details={"error_type": type(exc).__name__},
                    )
                )
                continue
            eligible = getattr(result, "eligible", getattr(result, "is_eligible", None))
            if eligible is False or str(getattr(result, "status", "")).lower() == "blocked":
                reason_codes = getattr(result, "reason_codes", ())
                issues.append(
                    _issue(
                        "coverage_incomplete",
                        "固定标的覆盖资格未通过，已阻断回测。",
                        field="coverage",
                        instrument_id=instrument_id,
                        details={"reason_codes": tuple(str(item) for item in reason_codes)},
                    )
                )
        return tuple(issues)

    # ------------------------------------------------------------------
    # Main APIs
    # ------------------------------------------------------------------

    def preflight(
        self,
        context: PreflightContext | DataPreflightRequest | DataRequest | object,
        *,
        authoritative: bool = False,
        **overrides: object,
    ) -> PreflightOutcome:
        """Run page or authoritative preflight with all pre-read gates first."""

        ctx = self._context(context, **overrides)
        profile = self._bound_profile(ctx)
        request = ctx.request
        fixed_ids = self._fixed_ids(ctx)
        try:
            fixtures = self._consumed_fixtures(
                profile,
                request,
                self._fixtures(ctx),
            )
        except DataContractError as exc:
            report = _minimal_blocked_report(
                request,
                (
                    _issue(
                        "internal_preflight_fixture_missing",
                        "内部 fixture 契约无效，已阻断回测。",
                        field="fixtures",
                        details={"error_type": type(exc).__name__},
                    ),
                ),
            )
            report = _attach_scope_evidence(
                report, request, dynamic_scope={}, fixed_ids=fixed_ids
            )
            return PreflightOutcome(report=report, profile=profile, fixed_instrument_ids=fixed_ids)

        pre_read_issues = list(self._fixture_issues(profile, fixtures, request, fixed_ids))
        allowed_modes = tuple(getattr(profile, "allowed_consistency_modes", ()))
        if allowed_modes and request.consistency_mode not in allowed_modes:
            pre_read_issues.append(
                _issue(
                    "internal_preflight_profile_mismatch",
                    "请求的一致性模式不在当前预检 profile 允许范围内，已阻断回测。",
                    field="consistency_mode",
                    details={
                        "preflight_profile": _profile_text(profile),
                        "actual": request.consistency_mode.value,
                        "allowed": [item.value for item in allowed_modes],
                    },
                )
            )
        non_zero_ids = tuple(
            getattr(request, "non_zero_initial_position_instrument_ids", ()) or ()
        )
        if non_zero_ids and ctx.spec is None and ctx.initial_position_gateway is None:
            # The request carries mandatory opening holdings, but no existing
            # initial-position preflight input was supplied.  Do not pretend
            # that a generic provider report proves valuation/accounting
            # facts for those positions.
            pre_read_issues.append(
                _issue(
                    "coverage_incomplete",
                    "非零初始持仓未绑定既有初始持仓预检，已阻断回测。",
                    field="initial_positions",
                    details={
                        "instrument_ids": [str(item) for item in non_zero_ids],
                    },
                )
            )
        maximum = getattr(request, "max_lookback_sessions", MAX_LOOKBACK_SESSIONS)
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum > MAX_LOOKBACK_SESSIONS:
            pre_read_issues.append(
                _issue(
                    "lookback_sessions_limit_exceeded",
                    f"历史会话数量超过首版上限 {MAX_LOOKBACK_SESSIONS}，读取前已阻断回测。",
                    field="max_lookback_sessions",
                    details={"requested": maximum, "maximum": MAX_LOOKBACK_SESSIONS},
                )
        )
        provider = ctx.provider or self.provider
        status_requirement_issue = _rule_status_requirement_issue(
            request,
            ctx.rule_preflight_report,
        )
        if status_requirement_issue is not None:
            pre_read_issues.append(status_requirement_issue)
        # The rule snapshot is the cheaper and earlier authority for STATUS
        # applicability.  Do not inspect a provider manifest after that
        # contract has already failed: the mismatch must remain a pure
        # request/snapshot decision and must not trigger provider I/O.
        if not pre_read_issues:
            pre_read_issues.extend(
                self._required_fixture_issues(profile, request, provider, fixtures)
            )
        if pre_read_issues:
            # Profile/fixture/lookback failures are all pre-read gates.  Do
            # not call the dynamic scope provider after one has already
            # proven that this request cannot proceed.
            dynamic_scope, dynamic_resolution = {}, None
        else:
            dynamic_scope, scope_issues, dynamic_resolution = self._resolve_scope(ctx, provider)
            pre_read_issues.extend(scope_issues)
        if pre_read_issues:
            report = _minimal_blocked_report(request, tuple(pre_read_issues))
            report = _attach_scope_evidence(
                report,
                request,
                dynamic_scope=dynamic_scope,
                fixed_ids=fixed_ids,
                resolution=dynamic_resolution,
            )
            return PreflightOutcome(
                report=report,
                profile=profile,
                fixed_instrument_ids=fixed_ids,
                dynamic_scope=dynamic_scope,
                fixtures=fixtures,
            )

        try:
            report = (
                ctx.base_report
                if ctx.base_report is not None
                else self._provider_report(
                    provider,
                    request,
                    session=ctx.session,
                    authoritative=authoritative,
                    dynamic_calendar_ids=tuple(dynamic_scope.get("resolved_calendar_ids", ())),
                )
            )
        except DataContractError as exc:
            report = _minimal_blocked_report(
                request,
                (
                    _issue(
                        getattr(exc, "code", "provider_contract_violation"),
                        "数据提供方预检失败，已阻断回测。",
                        field="provider",
                        details={"error_type": type(exc).__name__, **dict(getattr(exc, "details", {}) or {})},
                    ),
                ),
                provider_key=getattr(provider, "provider_key", None),
            )
        except Exception as exc:
            report = _minimal_blocked_report(
                request,
                (
                    _issue(
                        "provider_contract_violation",
                        "数据提供方预检发生未定义错误，已阻断回测。",
                        field="provider",
                        details={"error_type": type(exc).__name__},
                    ),
                ),
            )

        report = _attach_scope_evidence(
            report,
            request,
            dynamic_scope=dynamic_scope,
            fixed_ids=fixed_ids,
            resolution=dynamic_resolution,
        )
        report = _bind_quantity_action_integrity(report, request, fixtures)
        pit_gate_issues = _pit_gate_issues(report, request)
        if pit_gate_issues:
            report = _with_report_issues(report, pit_gate_issues)
        report_gate_issues = _post_report_gate_issues(report, request, fixtures)
        if profile.reference == FORMAL_PROFILE:
            report_gate_issues += _formal_capability_issues(report, request)
        report_gate_issues += _source_revision_audit_issues(report, request, profile, fixtures)
        if report_gate_issues:
            report = _with_report_issues(report, report_gate_issues)
        fixture_session_issues = _fixture_session_scope_issues(
            report, fixtures, fixed_ids
        )
        if fixture_session_issues:
            report = _with_report_issues(report, fixture_session_issues)
        removals = _fixture_substitution_removals(report, fixtures)
        if removals:
            report = _with_report_issues(report, (), remove_codes=removals)
        expected_dynamic_ids = set(dynamic_scope.get("resolved_calendar_ids", ()))
        if expected_dynamic_ids and not expected_dynamic_ids.issubset(set(report.resolved_calendar_ids)):
            report = _with_report_issues(
                report,
                (
                    _issue(
                        "universe_scope_unresolved",
                        "预检报告未覆盖动态范围解析出的全部日历，已阻断回测。",
                        field="resolved_calendar_ids",
                        details={
                            "expected": sorted(expected_dynamic_ids),
                            "actual": list(report.resolved_calendar_ids),
                        },
                    ),
                ),
            )

        initial_report: InitialPositionPreflightReport | None = None
        if ctx.spec is not None and ctx.spec.non_zero_initial_positions:
            if (
                ctx.spec.start_date != request.requested_window.start_date
                or ctx.spec.end_date != request.requested_window.end_date
            ):
                report = _with_report_issues(
                    report,
                    (
                        _issue(
                            "data_preflight_report_hash_mismatch",
                            "初始持仓预检范围与冻结请求不一致，已阻断回测。",
                            field="initial_positions",
                            details={
                                "request_window": {
                                    "start_date": request.requested_window.start_date,
                                    "end_date": request.requested_window.end_date,
                                },
                                "spec_window": {
                                    "start_date": ctx.spec.start_date,
                                    "end_date": ctx.spec.end_date,
                                },
                            },
                        ),
                    ),
                )
            gateway = ctx.initial_position_gateway
            if gateway is None and all(
                callable(getattr(provider, name, None))
                for name in (
                    "resolve_instrument",
                    "resolve_identity_mapping",
                    "resolve_instrument_rules",
                    "resolve_settlement_and_sell_rules",
                    "find_first_trading_session_on_or_after",
                    "get_raw_valuation_price",
                    "check_required_corporate_actions",
                    "check_required_trading_status",
                )
            ):
                gateway = provider  # type: ignore[assignment]
            if gateway is None:
                report = _with_report_issues(
                    report,
                    (
                        _issue(
                            "coverage_incomplete",
                            "非零初始持仓缺少既有初始持仓预检能力，已阻断回测。",
                            field="initial_positions",
                        ),
                    ),
                )
            else:
                initial_report = InitialPositionPreflightService(gateway).run(ctx.spec)
                fixture_capabilities = {str(item.capability) for item in fixtures}
                projected: list[PreflightIssue] = []
                remove_codes: list[str] = []
                for item in initial_report.issues:
                    code = str(item.code)
                    if code == "CORPORATE_ACTION_FACTS_MISSING" and FIXTURE_QUANTITY_ACTIONS in fixture_capabilities:
                        remove_codes.append(code)
                    elif code == "TRADING_STATUS_FACTS_MISSING" and FIXTURE_TRADING_STATUS in fixture_capabilities:
                        remove_codes.append(code)
                    else:
                        projected.append(_initial_issue(item))
                report = _with_report_issues(report, tuple(projected), remove_codes=remove_codes)

        qualification_issues = self._qualification_issues(ctx, report, profile, fixtures)
        if qualification_issues:
            report = _with_report_issues(report, qualification_issues)

        if profile.reference == INTERNAL_LINK_ACCEPTANCE_PROFILE and report.status is PreflightStatus.DEGRADED:
            report = _with_report_issues(
                report,
                (
                    _issue(
                        "internal_preflight_degraded_forbidden",
                        "内部 profile 不允许 degraded 状态，已阻断回测。",
                        field="status",
                    ),
                ),
            )
        return PreflightOutcome(
            report=report,
            profile=profile,
            fixed_instrument_ids=fixed_ids,
            dynamic_scope=dynamic_scope,
            fixtures=fixtures,
            initial_position_report=initial_report,
        )

    def admission(
        self,
        context: PreflightContext | DataPreflightRequest | object,
        **overrides: object,
    ) -> AdmissionDecision:
        """Run the page gate; this method has no run-creation side effect."""

        outcome = self.preflight(context, authoritative=False, **overrides)
        return AdmissionDecision(
            allowed=outcome.status is PreflightStatus.READY,
            outcome=outcome,
            reason_code=None if not outcome.blocked else outcome.report.primary_issue_code,
        )

    def admit(
        self,
        context: PreflightContext | DataPreflightRequest | object,
        *,
        confirmed_report_hash: str | None = None,
        **overrides: object,
    ) -> AdmissionDecision:
        """Apply admission and, when supplied, exact hash confirmation."""

        decision = self.admission(context, **overrides)
        if decision.outcome.status is PreflightStatus.BLOCKED:
            return decision
        if decision.outcome.status is PreflightStatus.DEGRADED:
            required_code = (
                "formal_degraded_confirmation_required"
                if decision.run_kind == "backtest_run"
                else "data_preflight_confirmation_mismatch"
            )
            mismatch_code = (
                "formal_degraded_confirmation_mismatch"
                if decision.run_kind == "backtest_run"
                else "data_preflight_confirmation_mismatch"
            )
            if confirmed_report_hash is None:
                return AdmissionDecision(
                    allowed=False,
                    outcome=decision.outcome,
                    reason_code=required_code,
                )
            if confirmed_report_hash != decision.report_hash:
                return AdmissionDecision(
                    allowed=False,
                    outcome=decision.outcome,
                    reason_code=mismatch_code,
                )
            return AdmissionDecision(allowed=True, outcome=decision.outcome)
        if confirmed_report_hash is not None and confirmed_report_hash != decision.report_hash:
            return AdmissionDecision(
                allowed=False,
                outcome=decision.outcome,
                reason_code="data_preflight_report_hash_mismatch",
            )
        return decision

    def validate_session(
        self,
        context: PreflightContext | DataPreflightRequest | object,
        *,
        admission: AdmissionDecision | PreflightOutcome | None = None,
        **overrides: object,
    ) -> SessionPreflightDecision:
        """Re-run authoritative preflight before any strategy load/call."""

        page_outcome = admission.outcome if isinstance(admission, AdmissionDecision) else admission
        outcome = self.preflight(context, authoritative=True, **overrides)
        admission_hash = page_outcome.report_hash if page_outcome is not None else None
        hash_match = None if admission_hash is None else admission_hash == outcome.report_hash
        report_diff: tuple[Mapping[str, object], ...] = ()
        if page_outcome is not None and page_outcome.blocked:
            # A blocked page gate cannot be revived by a later session read,
            # even if the underlying provider now happens to report ready.
            blocked_issue = _issue(
                "data_preflight_blocked",
                "页面准入预检未通过，会话不能重新放行该运行。",
                field="admission_report",
                details={"admission_report_hash": admission_hash},
            )
            outcome = PreflightOutcome(
                report=_with_report_issues(outcome.report, (blocked_issue,)),
                profile=outcome.profile,
                fixed_instrument_ids=outcome.fixed_instrument_ids,
                dynamic_scope=outcome.dynamic_scope,
                fixtures=outcome.fixtures,
                initial_position_report=outcome.initial_position_report,
                admission_report_hash=admission_hash,
                session_report_hash=outcome.report_hash,
                hash_match=False,
                failure_phase="data_preflight",
            )
            hash_match = False
        if admission_hash is not None and not hash_match:
            session_hash_before_block = outcome.report_hash
            report_diff = (
                MappingProxyType(
                    {
                        "section": "preflight",
                        "field": "report_hash",
                        "page_value": admission_hash,
                        "session_value": outcome.report_hash,
                        "reason_code": "data_preflight_report_hash_mismatch",
                    }
                ),
            )
            # A hash change is informational when the authoritative session
            # is ready (the session report is the final source of truth).
            # It is a hard failure only when the session remains degraded and
            # therefore requires the page's exact degraded confirmation.
            if outcome.status is PreflightStatus.DEGRADED:
                report = _with_report_issues(
                    outcome.report,
                    (_issue("data_preflight_report_hash_mismatch", "会话权威预检与页面准入报告不一致，已阻断回测。", field="report_hash", details=dict(report_diff[0])),),
                )
                outcome = PreflightOutcome(
                    report=report, profile=outcome.profile,
                    fixed_instrument_ids=outcome.fixed_instrument_ids,
                    dynamic_scope=outcome.dynamic_scope, fixtures=outcome.fixtures,
                    initial_position_report=outcome.initial_position_report,
                    admission_report_hash=admission_hash,
                    session_report_hash=session_hash_before_block,
                    hash_match=False, report_diff=report_diff,
                    failure_phase="data_preflight",
                )
        else:
            outcome = PreflightOutcome(
                report=outcome.report,
                profile=outcome.profile,
                fixed_instrument_ids=outcome.fixed_instrument_ids,
                dynamic_scope=outcome.dynamic_scope,
                fixtures=outcome.fixtures,
                initial_position_report=outcome.initial_position_report,
                admission_report_hash=admission_hash,
                session_report_hash=outcome.report_hash,
                hash_match=hash_match,
                failure_phase="data_preflight" if outcome.blocked else None,
            )
        # A degraded report is executable only when the page explicitly
        # confirmed the exact same hash; ready remains executable regardless
        # of a hash change because the session report is authoritative.
        page_degraded = page_outcome is not None and page_outcome.status is PreflightStatus.DEGRADED
        allowed = (
            outcome.status in (PreflightStatus.READY, PreflightStatus.DEGRADED)
            and (hash_match is not False or outcome.status is PreflightStatus.READY)
            and (outcome.status is PreflightStatus.READY or hash_match is True)
        )
        return SessionPreflightDecision(
            allowed=allowed,
            outcome=outcome,
            admission_report_hash=admission_hash,
            hash_match=hash_match,
            report_diff=report_diff,
            failure_phase=None if allowed else "data_preflight",
        )

    validate_authoritative_session = validate_session
    session_preflight = validate_session

    def before_strategy(
        self,
        context: PreflightContext | DataPreflightRequest | object,
        strategy_loader: Callable[[], Any],
        *,
        admission: AdmissionDecision | PreflightOutcome | None = None,
        **overrides: object,
    ) -> tuple[SessionPreflightDecision, Any | None]:
        """Load a strategy only after the authoritative gate succeeds."""

        decision = self.validate_session(context, admission=admission, **overrides)
        if decision.blocked:
            return decision, None
        return decision, strategy_loader()

    def open_session(
        self,
        request: DataRequest,
        *,
        admission: AdmissionDecision | PreflightOutcome | None = None,
    ) -> object:
        """Open a provider session after an optional page decision.

        Opening is delegated to the existing provider; this helper never
        creates a run or invokes strategy code.  Call ``validate_session`` on
        the returned object/context before loading a strategy.
        """

        if not isinstance(request, DataRequest):
            raise InvalidDataRequestError("open_session requires a frozen DataRequest")
        admitted = admission if isinstance(admission, AdmissionDecision) else None
        page = admitted.outcome if admitted is not None else admission
        if page is not None:
            if page.status is PreflightStatus.BLOCKED or (
                page.status is PreflightStatus.DEGRADED
                and (admitted is None or not admitted.allowed)
            ):
                raise InvalidDataRequestError(
                    "an unconfirmed data admission cannot open an authoritative data session"
                )
        provider = self.provider
        if provider is None or not callable(getattr(provider, "open_session", None)):
            raise ProviderContractViolationError("preflight provider has no open_session method")
        return provider.open_session(request)

    # ------------------------------------------------------------------
    # Existing result-table persistence
    # ------------------------------------------------------------------

    @staticmethod
    def persist_admission_report(
        repository: object,
        *,
        run_id: UUID,
        outcome: PreflightOutcome,
    ) -> int:
        """Persist one admission report via ``BacktestResultRepository``."""

        if isinstance(outcome, AdmissionDecision):
            outcome = outcome.outcome
        append = getattr(repository, "append", None)
        if not callable(append):
            raise InvalidDataRequestError("result repository must expose append")
        return int(append("data_preflight", outcome.to_result_record(run_id, "admission")))

    @staticmethod
    def persist_session_report(
        repository: object,
        *,
        run_id: UUID,
        outcome: PreflightOutcome,
        admission: PreflightOutcome | AdmissionDecision | None = None,
    ) -> int:
        """Persist a session report with page hash/diff association."""

        if isinstance(outcome, SessionPreflightDecision):
            outcome = outcome.outcome
        page = admission.outcome if isinstance(admission, AdmissionDecision) else admission
        if isinstance(page, SessionPreflightDecision):
            page = page.outcome
        append = getattr(repository, "append", None)
        if not callable(append):
            raise InvalidDataRequestError("result repository must expose append")
        return int(
            append(
                "data_preflight",
                outcome.to_result_record(run_id, "session", admission=page),
            )
        )

    save_admission_report = persist_admission_report
    save_session_report = persist_session_report


__all__ = [
    "AdmissionDecision",
    "DataPreflightService",
    "FORMAL_PROFILE_TEXT",
    "FIXTURE_QUANTITY_ACTIONS",
    "FIXTURE_REPEATABLE_READ",
    "FIXTURE_SOURCE_REVISIONS",
    "FIXTURE_TRADING_STATUS",
    "INTERNAL_FIXTURE_REGISTRY",
    "InternalFixture",
    "PreflightProfile",
    "PreflightProfileRegistry",
    "INTERNAL_LINK_ACCEPTANCE_PROFILE",
    "FORMAL_PROFILE",
    "INTERNAL_LINK_ACCEPTANCE_PROFILE_TEXT",
    "InternalFixtureContractError",
    "PreflightContext",
    "PreflightOutcome",
    "PreflightProfileMismatchError",
    "PreflightServiceError",
    "RUN_KIND_FORMAL",
    "RUN_KIND_INTERNAL_LINK_ACCEPTANCE",
    "SessionPreflightDecision",
]
