"""Fixed-instrument rule preflight: the formal hard admission gate.

The service checks every fixed instrument of a run — ``static_instrument_ids``
unioned with ``mandatory_instrument_ids`` and non-zero initial positions —
across the whole backtest window, splitting the window into validity
segments whenever facts change.  Any missing, expired, conflicting,
incomplete-quality, fixture-sourced fact, unsupported settlement class,
or missing capability fact blocks the entire report; there is no degraded
admission path.

A ready report carries a complete :class:`RunRuleSnapshotBundle`; a
blocked report never creates one.  Machine judgment uses stable issue
codes; Chinese messages are operator-facing only.  Existing
initial-position preflight stays responsible for positions and valuation;
this module only adds rule-fact admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol, Sequence
from uuid import UUID

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import VersionedReference
from app.instruments.rule_exceptions_repository import PersistedExceptionSet
from app.instruments.rule_snapshots import (
    FactProvenance,
    InstrumentRuleSnapshotSegment,
    RunRuleSnapshotBundle,
)
from app.instruments.rules.contracts import (
    FactQualityStatus,
    ParseMode,
    RuleExceptionSetDefinition,
    RuleFactCandidate,
    RulePackageDefinition,
    RulePackageIssue,
    RulePackageIssueCode,
    RulePackageResolution,
    ResolutionStatus,
    canonical_payload,
    reference_display,
    stable_hash,
)
from app.instruments.rules.registry import RulePackageRegistry
from app.instruments.rules.resolver import PARSER_REVISION, RulePackageResolver


class RuleCheckStatus(StrEnum):
    """Outcome of one sub-check for one instrument."""

    OK = "ok"
    BLOCKED = "blocked"


#: Issue-code classification used for the per-instrument sub-check
#: statuses; capability codes are reported separately from rules codes.
_RULES_CHECK_CODES: frozenset[RulePackageIssueCode] = frozenset(
    {
        RulePackageIssueCode.RULE_PACKAGE_MISMATCH,
        RulePackageIssueCode.RULE_FACT_MISSING,
        RulePackageIssueCode.RULE_FACT_EXPIRED,
        RulePackageIssueCode.RULE_FACT_CONFLICT,
        RulePackageIssueCode.RULE_FACT_NOT_COMPLETE,
        RulePackageIssueCode.RULE_FIXTURE_SOURCE_FORBIDDEN,
        RulePackageIssueCode.RULE_EXCEPTION_SET_MISSING,
        RulePackageIssueCode.RULE_EXCEPTION_TARGET_MISMATCH,
        RulePackageIssueCode.RULE_EXCEPTION_INTERVAL_CONFLICT,
        RulePackageIssueCode.RULE_EXCEPTION_FACT_MISSING,
        RulePackageIssueCode.RULE_REQUIRED_FIELD_MISSING,
        RulePackageIssueCode.RULE_FIELD_INVALID,
        RulePackageIssueCode.RULE_FIELD_CONFLICT,
        RulePackageIssueCode.RULE_SETTLEMENT_UNKNOWN,
        RulePackageIssueCode.RULE_SETTLEMENT_UNSUPPORTED,
        RulePackageIssueCode.RULE_SNAPSHOT_INCOMPLETE,
    }
)
_CAPABILITY_CHECK_CODES: frozenset[RulePackageIssueCode] = frozenset(
    {
        RulePackageIssueCode.RULE_CAPABILITY_DECLARATION_MISSING,
        RulePackageIssueCode.RULE_CAPABILITY_FACT_MISSING,
    }
)


class InstrumentRulePreflightGateway(Protocol):
    """Read boundary between the preflight service and persistence."""

    def list_rule_facts(
        self,
        instrument_id: UUID,
        package_reference: VersionedReference,
        *,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> Sequence[RuleFactCandidate]:
        """Return visible candidates intersecting the window."""
        ...

    def resolve_exception_set(
        self,
        set_reference: VersionedReference,
        *,
        data_cutoff: datetime,
    ) -> PersistedExceptionSet | None:
        """Return the exact set version with provenance, or ``None``."""
        ...

    def check_required_trading_status_facts(
        self,
        instrument_id: UUID,
        dimensions: Sequence[str],
        *,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> tuple[str, ...]:
        """Return which required trading-status dimensions lack facts."""
        ...


@dataclass(frozen=True, slots=True)
class FixedInstrumentRulePreflightRequest:
    """One formal fixed-instrument rule preflight request.

    ``instrument_ids`` must already be the stable deduplicated union of
    static ids, mandatory ids, and non-zero initial-position ids; the
    constructor re-sorts defensively so ordering can never affect hashes.
    """

    instrument_ids: tuple[UUID, ...]
    start_date: date
    end_date: date
    data_cutoff: datetime
    rule_package_reference: VersionedReference
    exception_set_reference: VersionedReference | None = None
    mode: ParseMode = ParseMode.FORMAL

    def __post_init__(self) -> None:
        if isinstance(self.instrument_ids, (str, bytes)) or not hasattr(
            self.instrument_ids, "__iter__"
        ):
            raise DomainValidationError(
                "instrument_ids must be a collection of UUIDs"
            )
        try:
            candidates = tuple(self.instrument_ids)
        except TypeError as exc:
            raise DomainValidationError(
                "instrument_ids must be a collection of UUIDs"
            ) from exc
        seen: set[UUID] = set()
        for instrument_id in candidates:
            if not isinstance(instrument_id, UUID):
                raise DomainValidationError(
                    "instrument_ids must contain UUID instances"
                )
            seen.add(instrument_id)
        if not seen:
            raise DomainValidationError("instrument_ids must not be empty")
        object.__setattr__(self, "instrument_ids", tuple(sorted(seen, key=str)))
        if not isinstance(self.start_date, date) or isinstance(
            self.start_date, datetime
        ):
            raise DomainValidationError("start_date must be a calendar date")
        if not isinstance(self.end_date, date) or isinstance(
            self.end_date, datetime
        ):
            raise DomainValidationError("end_date must be a calendar date")
        if self.start_date > self.end_date:
            raise DomainValidationError("start_date cannot be after end_date")
        object.__setattr__(
            self, "data_cutoff", _aware_datetime(self.data_cutoff, "data_cutoff")
        )
        if not isinstance(self.rule_package_reference, VersionedReference):
            raise DomainValidationError(
                "rule_package_reference must be a VersionedReference"
            )
        if self.exception_set_reference is not None and not isinstance(
            self.exception_set_reference, VersionedReference
        ):
            raise DomainValidationError(
                "exception_set_reference must be a VersionedReference when "
                "provided"
            )
        # Formal admission is the only production mode; keeping the field
        # explicit makes the gate auditable instead of implied.
        if self.mode is not ParseMode.FORMAL:
            raise DomainValidationError(
                "fixed-instrument rule preflight runs in formal mode only"
            )


@dataclass(frozen=True, slots=True)
class InstrumentRulePreflightResult:
    """Per-instrument outcome across all validity segments."""

    instrument_id: UUID
    status: ResolutionStatus
    rules_check_status: RuleCheckStatus
    capability_check_status: RuleCheckStatus
    resolved_segments: tuple[InstrumentRuleSnapshotSegment, ...]
    selected_fact_references: tuple[VersionedReference, ...]
    issues: tuple[RulePackageIssue, ...]


@dataclass(frozen=True, slots=True)
class RulePreflightReport:
    """Immutable aggregate result of the fixed-instrument rule gate.

    ``snapshot_bundle`` is present only for ready reports; blocked
    reports never freeze a snapshot.  ``report_hash`` covers stable
    content only — Chinese messages are excluded by construction.  The
    checked window and cutoff are recorded so an admission path can bind
    the report to exactly one run intent.
    """

    status: ResolutionStatus
    rule_package_reference: VersionedReference
    rule_package_semantic_hash: str
    exception_set_reference: VersionedReference | None
    exception_set_hash: str | None
    data_cutoff: datetime
    start_date: date
    end_date: date
    checked_instruments: tuple[InstrumentRulePreflightResult, ...]
    issues: tuple[RulePackageIssue, ...]
    snapshot_bundle: RunRuleSnapshotBundle | None = None
    snapshot_hash: str = ""
    report_hash: str = ""

    def __post_init__(self) -> None:
        # Machine judgment relies on the exact READY/BLOCKED vocabulary;
        # a free-form string must never masquerade as a terminal status.
        if not isinstance(self.status, ResolutionStatus):
            raise DomainValidationError("status must be a ResolutionStatus")
        if not isinstance(self.start_date, date) or isinstance(
            self.start_date, datetime
        ):
            raise DomainValidationError("start_date must be a calendar date")
        if not isinstance(self.end_date, date) or isinstance(self.end_date, datetime):
            raise DomainValidationError("end_date must be a calendar date")
        # The report is the admission credential for the rule gate, so a
        # ready report is bound to its verified bundle at construction:
        # neither field can be forged or detached afterwards (a
        # dataclasses.replace that breaks the binding fails right here).
        if self.status is ResolutionStatus.READY:
            self._validate_ready_binding()
        elif self.status is ResolutionStatus.BLOCKED:
            if self.snapshot_bundle is not None or self.snapshot_hash:
                raise DomainValidationError(
                    "a blocked rule preflight report must not carry a "
                    "snapshot bundle or snapshot hash"
                )
        else:  # pragma: no cover - guarded by the isinstance check above
            raise DomainValidationError(
                f"unsupported rule preflight status {self.status}"
            )
        # Defensive recomputation so a caller cannot forge a mismatched hash.
        object.__setattr__(self, "report_hash", _hash_report(self))

    def _validate_ready_binding(self) -> None:
        """Enforce full consistency between a READY report and its bundle."""

        bundle = self.snapshot_bundle
        if not isinstance(bundle, RunRuleSnapshotBundle):
            raise DomainValidationError(
                "a ready rule preflight report must carry its frozen "
                "snapshot bundle"
            )
        if self.snapshot_hash != bundle.snapshot_hash:
            raise DomainValidationError(
                "snapshot_hash must equal the snapshot bundle's verified "
                "hash; a hand-written hash cannot stand in for one"
            )
        mismatches = []
        if bundle.rule_package_reference != self.rule_package_reference:
            mismatches.append("rule_package_reference")
        if (
            bundle.rule_package_semantic_hash
            != self.rule_package_semantic_hash
        ):
            mismatches.append("rule_package_semantic_hash")
        if bundle.exception_set_reference != self.exception_set_reference:
            mismatches.append("exception_set_reference")
        if bundle.exception_set_hash != self.exception_set_hash:
            mismatches.append("exception_set_hash")
        if bundle.data_cutoff != self.data_cutoff:
            mismatches.append("data_cutoff")
        if mismatches:
            raise DomainValidationError(
                "snapshot bundle metadata does not match the report: "
                f"{sorted(mismatches)}"
            )
        # A ready report is clean by definition: no issues anywhere, and
        # every checked instrument froze at least one segment.
        if self.issues:
            raise DomainValidationError(
                "a ready rule preflight report must not carry issues"
            )
        checked_ids = {
            result.instrument_id for result in self.checked_instruments
        }
        for result in self.checked_instruments:
            if result.status is not ResolutionStatus.READY:
                raise DomainValidationError(
                    "a ready rule preflight report must not contain "
                    "blocked instrument results"
                )
            if result.issues:
                raise DomainValidationError(
                    "a ready rule preflight report must not contain "
                    "instrument results with issues"
                )
        segment_ids = {
            segment.instrument_id
            for segment in bundle.instrument_segments
        }
        missing = sorted(str(iid) for iid in checked_ids - segment_ids)
        if missing:
            raise DomainValidationError(
                "a ready rule preflight report requires at least one "
                f"snapshot segment per checked instrument; missing {missing}"
            )
        orphaned = sorted(str(iid) for iid in segment_ids - checked_ids)
        if orphaned:
            raise DomainValidationError(
                "the snapshot bundle contains segments for instruments "
                f"that were not checked: {orphaned}"
            )


def _hash_report(report: RulePreflightReport) -> str:
    payload = {
        "kind": "rule_preflight_report",
        "status": report.status,
        "rule_package_reference": canonical_payload(
            report.rule_package_reference
        ),
        "rule_package_semantic_hash": report.rule_package_semantic_hash,
        "exception_set_reference": (
            None
            if report.exception_set_reference is None
            else canonical_payload(report.exception_set_reference)
        ),
        "exception_set_hash": report.exception_set_hash,
        "data_cutoff": report.data_cutoff.isoformat(),
        "start_date": report.start_date.isoformat(),
        "end_date": report.end_date.isoformat(),
        "snapshot_hash": report.snapshot_hash,
        "checked_instruments": [
            {
                "instrument_id": str(result.instrument_id),
                "status": result.status,
                "rules_check_status": result.rules_check_status,
                "capability_check_status": result.capability_check_status,
                "fact_references": [
                    canonical_payload(reference)
                    for reference in sorted(
                        result.selected_fact_references,
                        key=lambda ref: (ref.key, ref.version),
                    )
                ],
                "issues": sorted(
                    (issue.code, issue.field) for issue in result.issues
                ),
            }
            for result in report.checked_instruments
        ],
        "issues": sorted((issue.code, issue.field) for issue in report.issues),
    }
    return stable_hash(payload)


class FixedInstrumentRulePreflightService:
    """Run the formal hard rule gate over every fixed instrument."""

    def __init__(
        self,
        registry: RulePackageRegistry,
        gateway: InstrumentRulePreflightGateway,
    ) -> None:
        if not isinstance(registry, RulePackageRegistry):
            raise DomainValidationError("registry must be a RulePackageRegistry")
        self._registry = registry
        self._gateway = gateway

    def run(
        self, request: FixedInstrumentRulePreflightRequest
    ) -> RulePreflightReport:
        """Check every instrument over every validity segment of the window.

        The exact rule-package key/version must be registered (a missing
        registration is a caller bug); every data problem becomes part of
        a structured blocked report instead of an exception.  A single
        blocking segment blocks the whole report.
        """

        definition = self._registry.require(request.rule_package_reference)

        exception_persisted: PersistedExceptionSet | None = None
        top_issues: list[RulePackageIssue] = []
        if request.exception_set_reference is not None:
            exception_persisted = self._gateway.resolve_exception_set(
                request.exception_set_reference,
                data_cutoff=request.data_cutoff,
            )
            if exception_persisted is None:
                top_issues.append(
                    _issue(
                        RulePackageIssueCode.RULE_EXCEPTION_SET_MISSING,
                        message=(
                            "例外集合 "
                            f"{reference_display(request.exception_set_reference)}"
                            " 不存在或在数据截止点之前不可见，整个规则预检阻断"
                        ),
                        details={
                            "exception_set": reference_display(
                                request.exception_set_reference
                            ),
                            "data_cutoff": request.data_cutoff.isoformat(),
                        },
                    )
                )
            else:
                # The set itself is subject to the same formal quality and
                # fixture gates as any fact row.  Anything that is not
                # explicitly COMPLETE blocks: unknown future statuses must
                # fail closed, not open.
                if exception_persisted.quality_status is not (
                    FactQualityStatus.COMPLETE
                ):
                    top_issues.append(
                        _issue(
                            RulePackageIssueCode.RULE_FACT_NOT_COMPLETE,
                            message=(
                                "例外集合 "
                                f"{reference_display(request.exception_set_reference)}"
                                " 的质量标记为不完整，正式模式拒绝使用"
                            ),
                            details={
                                "exception_set": reference_display(
                                    request.exception_set_reference
                                ),
                            },
                        )
                    )
                if exception_persisted.fixture_only:
                    top_issues.append(
                        _issue(
                            RulePackageIssueCode.RULE_FIXTURE_SOURCE_FORBIDDEN,
                            message=(
                                "例外集合 "
                                f"{reference_display(request.exception_set_reference)}"
                                " 为测试 fixture 来源，正式模式拒绝使用"
                            ),
                            details={
                                "exception_set": reference_display(
                                    request.exception_set_reference
                                ),
                            },
                        )
                    )

        results = tuple(
            self._check_instrument(
                request,
                definition,
                exception_persisted.definition if exception_persisted else None,
                instrument_id,
            )
            for instrument_id in request.instrument_ids
        )

        blocked = bool(top_issues) or any(
            result.status is ResolutionStatus.BLOCKED for result in results
        )
        aggregated_issues = tuple(
            sorted(
                [*top_issues, *(issue for r in results for issue in r.issues)],
                key=lambda issue: (
                    issue.code,
                    str(issue.instrument_id),
                    issue.field or "",
                ),
            )
        )
        common = dict(
            rule_package_reference=request.rule_package_reference,
            rule_package_semantic_hash=definition.semantic_hash,
            exception_set_reference=request.exception_set_reference,
            exception_set_hash=(
                None if exception_persisted is None
                else exception_persisted.content_hash
            ),
            data_cutoff=request.data_cutoff,
            start_date=request.start_date,
            end_date=request.end_date,
            checked_instruments=results,
        )
        if blocked:
            return RulePreflightReport(
                status=ResolutionStatus.BLOCKED,
                issues=aggregated_issues,
                snapshot_bundle=None,
                snapshot_hash="",
                **common,
            )

        segments = [
            segment for result in results for segment in result.resolved_segments
        ]
        if not segments or any(
            not result.resolved_segments for result in results
        ):
            # Defensive: a ready path that failed to freeze at least one
            # segment per instrument must never emit a partial snapshot.
            return RulePreflightReport(
                status=ResolutionStatus.BLOCKED,
                issues=(
                    _issue(
                        RulePackageIssueCode.RULE_SNAPSHOT_INCOMPLETE,
                        message="规则预检通过但未能生成完整的运行规则快照",
                        details={"reason": "missing instrument segments"},
                    ),
                ),
                snapshot_bundle=None,
                snapshot_hash="",
                **common,
            )
        bundle = RunRuleSnapshotBundle(
            rule_package_reference=request.rule_package_reference,
            rule_package_semantic_hash=definition.semantic_hash,
            parser_revision=PARSER_REVISION,
            exception_set_reference=request.exception_set_reference,
            exception_set_hash=(
                None if exception_persisted is None
                else exception_persisted.content_hash
            ),
            data_cutoff=request.data_cutoff,
            instrument_segments=tuple(segments),
        )
        return RulePreflightReport(
            status=ResolutionStatus.READY,
            issues=(),
            snapshot_bundle=bundle,
            snapshot_hash=bundle.snapshot_hash,
            **common,
        )

    # ------------------------------------------------------------------
    # Per-instrument checks
    # ------------------------------------------------------------------

    def _check_instrument(
        self,
        request: FixedInstrumentRulePreflightRequest,
        definition: RulePackageDefinition,
        exception_definition: RuleExceptionSetDefinition | None,
        instrument_id: UUID,
    ) -> InstrumentRulePreflightResult:
        """Check one instrument across the whole segmented window."""

        facts = list(
            self._gateway.list_rule_facts(
                instrument_id,
                request.rule_package_reference,
                start_date=request.start_date,
                end_date=request.end_date,
                data_cutoff=request.data_cutoff,
            )
        )
        ordinary = [fact for fact in facts if fact.exception_fact_ref is None]
        exceptional = [fact for fact in facts if fact.exception_fact_ref is not None]

        issues: list[RulePackageIssue] = []
        segments: list[InstrumentRuleSnapshotSegment] = []
        selected_refs: dict[tuple[str, int], VersionedReference] = {}

        boundary_edges = list(ordinary)
        exception_entry_edges: list[date] = []
        if exception_definition is not None:
            boundary_edges = [
                *boundary_edges,
                *exceptional,
            ]
            # Both edges of every matching entry participate in the
            # segmentation, so an exception ending mid-window stops being
            # applied after its exclusive valid_to.
            for entry in exception_definition.entries:
                if entry.instrument_id != instrument_id:
                    continue
                exception_entry_edges.append(entry.valid_from)
                if entry.valid_to is not None:
                    exception_entry_edges.append(entry.valid_to)
        for seg_from, seg_to in _segment_boundaries(
            boundary_edges,
            request.start_date,
            request.end_date,
            extra_edges=exception_entry_edges,
        ):
            covering = [
                fact
                for fact in ordinary
                if fact.valid_from <= seg_from
                and (fact.valid_to is None or fact.valid_to > seg_from)
            ]
            normal_fact: RuleFactCandidate | None = None
            if len(covering) > 1:
                issues.append(
                    _issue(
                        RulePackageIssueCode.RULE_FACT_CONFLICT,
                        instrument_id=instrument_id,
                        message=(
                            "同一有效区间内存在多条同等适用的普通事实，"
                            "无法唯一选择，禁止按插入顺序回退"
                        ),
                        details={
                            "segment_start": seg_from.isoformat(),
                            "segment_end": (
                                None if seg_to is None else seg_to.isoformat()
                            ),
                            "sources": sorted(
                                fact.source for fact in covering
                            ),
                        },
                    )
                )
            elif not covering:
                issues.append(
                    _issue(
                        RulePackageIssueCode.RULE_FACT_MISSING,
                        instrument_id=instrument_id,
                        message=(
                            f"区间 {seg_from.isoformat()} 起缺少覆盖该有效区间的"
                            "普通规则事实，且无生产默认值可用"
                        ),
                        details={
                            "segment_start": seg_from.isoformat(),
                            "segment_end": (
                                None if seg_to is None else seg_to.isoformat()
                            ),
                        },
                    )
                )
            else:
                normal_fact = covering[0]
                selected_refs[
                    (normal_fact.fact_reference.key, normal_fact.fact_reference.version)
                ] = normal_fact.fact_reference

            exception_fact = self._match_exception_fact(
                request,
                instrument_id,
                exception_definition,
                exceptional,
                seg_from,
                issues,
                selected_refs,
            )

            if len(covering) != 1:
                # Selection itself failed; the resolver needs exactly one
                # ordinary candidate to validate fields meaningfully.
                continue

            resolution = self._resolver.resolve(
                request.rule_package_reference,
                instrument_id=instrument_id,
                asset_class=_asset_class_of(definition),
                effective_date=seg_from,
                data_cutoff=request.data_cutoff,
                facts=[normal_fact, *exceptional],
                exception_sets=(
                    [exception_definition] if exception_definition else ()
                ),
                mode=request.mode,
            )
            issues.extend(resolution.issues)
            if resolution.status is ResolutionStatus.READY:
                segment_issues = self._capability_fact_issues(
                    request,
                    instrument_id,
                    resolution,
                    seg_from,
                    seg_to,
                )
                issues.extend(segment_issues)
                if not segment_issues:
                    segments.append(
                        _build_segment(
                            instrument_id,
                            seg_from,
                            seg_to,
                            resolution,
                            {fact.fact_reference: fact for fact in facts},
                        )
                    )

        status = (
            ResolutionStatus.BLOCKED
            if issues
            else ResolutionStatus.READY
        )
        rules_status = (
            RuleCheckStatus.BLOCKED
            if any(issue.code in _RULES_CHECK_CODES for issue in issues)
            else RuleCheckStatus.OK
        )
        capability_status = (
            RuleCheckStatus.BLOCKED
            if any(issue.code in _CAPABILITY_CHECK_CODES for issue in issues)
            else RuleCheckStatus.OK
        )
        return InstrumentRulePreflightResult(
            instrument_id=instrument_id,
            status=status,
            rules_check_status=rules_status,
            capability_check_status=capability_status,
            resolved_segments=tuple(segments),
            selected_fact_references=tuple(
                sorted(
                    selected_refs.values(),
                    key=lambda ref: (ref.key, ref.version),
                )
            ),
            issues=tuple(issues),
        )

    def _match_exception_fact(
        self,
        request: FixedInstrumentRulePreflightRequest,
        instrument_id: UUID,
        exception_definition: RuleExceptionSetDefinition | None,
        exceptional: Sequence[RuleFactCandidate],
        seg_from: date,
        issues: list[RulePackageIssue],
        selected_refs: dict[tuple[str, int], VersionedReference],
    ) -> RuleFactCandidate | None:
        """Record interval-level exception issues for the segment.

        Field-level validation of the matched exception fact is left to
        the resolver; this method adds the routing-level checks the task
        contract requires before resolution runs.
        """

        if exception_definition is None:
            return None
        covering_entries = [
            entry
            for entry in exception_definition.entries
            if entry.instrument_id == instrument_id and entry.covers(seg_from)
        ]
        if len(covering_entries) > 1:
            issues.append(
                _issue(
                    RulePackageIssueCode.RULE_EXCEPTION_INTERVAL_CONFLICT,
                    instrument_id=instrument_id,
                    message=(
                        "同一标的在同一有效时点命中多个重叠例外区间，"
                        "优先级不得由插入顺序决定"
                    ),
                    details={
                        "segment_start": seg_from.isoformat(),
                        "exception_sets": [
                            reference_display(exception_definition.reference)
                        ],
                    },
                )
            )
            return None
        if not covering_entries:
            return None
        entry = covering_entries[0]
        # Strict identity: the candidate's own fact_reference must equal
        # the entry's declared exception fact — a different version (even
        # of the same key) must never stand in for it.
        matching = [
            fact
            for fact in exceptional
            if fact.fact_reference == entry.exception_fact_ref
            and fact.exception_fact_ref == entry.exception_fact_ref
            and fact.valid_from <= seg_from
            and (fact.valid_to is None or fact.valid_to > seg_from)
        ]
        if len(matching) == 1:
            selected_refs[
                (
                    matching[0].fact_reference.key,
                    matching[0].fact_reference.version,
                )
            ] = matching[0].fact_reference
            return matching[0]
        if not matching:
            issues.append(
                _issue(
                    RulePackageIssueCode.RULE_EXCEPTION_FACT_MISSING,
                    instrument_id=instrument_id,
                    message=(
                        "例外条目命中，但其引用的例外事实 "
                        f"{reference_display(entry.exception_fact_ref)} 在该区间"
                        "缺失、过期或不可见"
                    ),
                    details={
                        "exception_fact_ref": reference_display(
                            entry.exception_fact_ref
                        ),
                        "segment_start": seg_from.isoformat(),
                    },
                )
            )
        else:
            issues.append(
                _issue(
                    RulePackageIssueCode.RULE_FACT_CONFLICT,
                    instrument_id=instrument_id,
                    message="多条同等适用的例外事实同时命中同一例外引用",
                    details={
                        "exception_fact_ref": reference_display(
                            entry.exception_fact_ref
                        ),
                        "sources": sorted(fact.source for fact in matching),
                    },
                )
            )
        return None

    def _capability_fact_issues(
        self,
        request: FixedInstrumentRulePreflightRequest,
        instrument_id: UUID,
        resolution: RulePackageResolution,
        seg_from: date,
        seg_to: date | None,
    ) -> list[RulePackageIssue]:
        """Check required trading-status dimensions against status facts.

        ``not_applicable`` declarations need no facts and are preserved
        as-is inside the resolution; only explicitly ``required``
        dimensions demand point-in-time trading-status facts in the
        segment.
        """

        required_dimensions = sorted(
            dimension
            for dimension, requirement in resolution.capability_declarations.items()
            if requirement == "required"
        )
        if not required_dimensions:
            return []
        # The segment is half-open [seg_from, seg_to); an open-ended final
        # segment runs to the inclusive backtest end date, so the status
        # facts must cover the whole remaining window — not just one day.
        status_window_end = (
            request.end_date if seg_to is None else seg_to - timedelta(days=1)
        )
        missing = self._gateway.check_required_trading_status_facts(
            instrument_id,
            required_dimensions,
            start_date=seg_from,
            end_date=status_window_end,
            data_cutoff=request.data_cutoff,
        )
        if not missing:
            return []
        return [
            _issue(
                RulePackageIssueCode.RULE_CAPABILITY_FACT_MISSING,
                instrument_id=instrument_id,
                field=dimension,
                message=(
                    f"能力维度 {dimension} 声明为 required，但该区间内"
                    "缺少对应的交易状态事实"
                ),
                details={
                    "dimension": dimension,
                    "segment_start": seg_from.isoformat(),
                },
            )
            for dimension in missing
        ]

    @property
    def _resolver(self) -> RulePackageResolver:
        """Fresh resolver over the frozen registry (cheap, immutable)."""

        return RulePackageResolver(self._registry)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _asset_class_of(definition: RulePackageDefinition) -> str:
    """The package's single supported asset class.

    v1 packages declare exactly one class; a future multi-class package
    would require callers to pass the class explicitly.
    """

    classes = sorted(definition.supported_asset_classes)
    if len(classes) != 1:
        raise DomainValidationError(
            "fixed-instrument preflight expects a single-asset-class "
            f"package; got {classes}"
        )
    return classes[0]


def _segment_boundaries(
    facts: Sequence[RuleFactCandidate],
    start_date: date,
    end_date: date,
    *,
    extra_edges: Sequence[date] = (),
) -> list[tuple[date, date | None]]:
    """Split the window into elementary half-open validity intervals.

    Boundaries come from the facts' ``valid_from``/``valid_to`` edges and
    any explicit exception-interval edges, clipped to the requested
    window, so a mid-run fact change produces multiple segments instead
    of one resolution covering everything.  The last segment stays
    open-ended within the run window.
    """

    edges = {start_date}
    for fact in facts:
        if start_date < fact.valid_from <= end_date:
            edges.add(fact.valid_from)
        if fact.valid_to is not None and start_date < fact.valid_to <= end_date:
            edges.add(fact.valid_to)
    for edge in extra_edges:
        if start_date < edge <= end_date:
            edges.add(edge)
    ordered = sorted(edges)
    segments: list[tuple[date, date | None]] = []
    for index, edge in enumerate(ordered):
        next_edge = (
            ordered[index + 1] if index + 1 < len(ordered) else None
        )
        segments.append((edge, next_edge))
    return segments


def _build_segment(
    instrument_id: UUID,
    effective_from: date,
    effective_to: date | None,
    resolution: RulePackageResolution,
    facts_by_reference: dict[VersionedReference, RuleFactCandidate],
) -> InstrumentRuleSnapshotSegment:
    """Freeze one ready resolution into an immutable snapshot segment."""

    normal_summary = next(
        (
            summary
            for summary in resolution.selected_facts
            if summary.exception_set_reference is None
        ),
        None,
    )
    exception_summary = next(
        (
            summary
            for summary in resolution.selected_facts
            if summary.exception_set_reference is not None
        ),
        None,
    )
    if normal_summary is None:
        raise DomainValidationError(
            "a ready resolution must select exactly one ordinary fact"
        )

    def _provenance(summary) -> dict:
        candidate = facts_by_reference.get(summary.fact_reference)
        quality = (
            candidate.quality_status.value
            if candidate is not None
            else summary.quality_status.value
        )
        fixture_only = (
            candidate.fixture_only if candidate is not None else summary.fixture_only
        )
        known_at = summary.known_at
        valid_from = summary.valid_from
        valid_to = summary.valid_to
        return FactProvenance(
            fact_reference=summary.fact_reference,
            source=summary.source,
            source_revision=summary.source_revision,
            valid_from=valid_from,
            valid_to=valid_to,
            known_at=known_at,
            quality_status=quality,
            fixture_only=fixture_only,
        ).to_payload()

    provenance: dict = {"normal_fact": _provenance(normal_summary)}
    if exception_summary is not None:
        provenance["exception_fact"] = _provenance(exception_summary)
    return InstrumentRuleSnapshotSegment(
        instrument_id=instrument_id,
        effective_from=effective_from,
        effective_to=effective_to,
        normal_fact_reference=normal_summary.fact_reference,
        exception_fact_reference=(
            exception_summary.fact_reference
            if exception_summary is not None
            else None
        ),
        normalized_values=dict(resolution.normalized_values),
        capability_declarations=dict(resolution.capability_declarations),
        provenance=provenance,
        resolution_hash=resolution.semantic_hash,
    )


def _issue(
    code: RulePackageIssueCode,
    *,
    message: str,
    instrument_id: UUID | None = None,
    field: str | None = None,
    details: dict | None = None,
) -> RulePackageIssue:
    return RulePackageIssue(
        code=code,
        message=message,
        field=field,
        instrument_id=instrument_id,
        details=details,
    )


__all__ = [
    "FixedInstrumentRulePreflightRequest",
    "FixedInstrumentRulePreflightService",
    "InstrumentRulePreflightGateway",
    "InstrumentRulePreflightResult",
    "RuleCheckStatus",
    "RulePreflightReport",
]
