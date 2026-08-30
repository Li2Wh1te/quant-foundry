"""Pure coverage-fact aggregation for the generic backtesting data layer.

This module deliberately owns no provider, ORM, network, or calendar
resolution logic.  It consumes already-resolved instrument ids and session
dates and projects immutable :class:`DataCoverageFact` values into the
existing :class:`DataCoverageReport`.  Domain adapters remain responsible for
deciding whether a Bar, rule, or other source fact is valid; this layer only
normalizes those declared outcomes and accounts for missing keys.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.backtesting.data.errors import (
    CoverageFactInvalidError,
    CoverageProviderContractViolationError,
)
from app.backtesting.data.facts import (
    CoverageApplicability,
    DataCoverageFact,
    _coverage_rule_ref,
)
from app.backtesting.data.reports import (
    DataCoverageReport,
    PreflightIssue,
    canonical_hash,
    canonical_json,
)
from app.backtesting.data.requests import (
    DataCapability,
    DateRange,
    IssueSeverity,
    QualityStatus,
)

__all__ = [
    "aggregate_coverage",
    "aggregate_coverage_facts",
    "build_coverage_report",
    "coverage_report_hash",
    "evaluate_coverage",
]


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    """One required machine field and its optional exact rule reference."""

    field: str
    validation_rule: object


def _contract_error(
    message: str,
    *,
    field: str,
    actual: object,
    expected: object,
) -> CoverageFactInvalidError:
    """Construct one deterministic input-contract error."""

    safe_actual = actual if type(actual) in (str, int, float, bool) or actual is None else type(actual).__name__
    safe_expected = expected if type(expected) in (str, int, float, bool) or expected is None else str(expected)
    return CoverageFactInvalidError(
        message,
        details={"field": field, "actual": safe_actual, "expected": safe_expected},
    )


def _normalize_instrument_ids(value: Iterable[UUID] | UUID) -> tuple[UUID, ...]:
    """Validate and sort the stable instrument identity set."""

    if isinstance(value, UUID):
        values = (value,)
    elif isinstance(value, (str, bytes)):
        raise _contract_error(
            "expected instrument ids must be UUID values",
            field="expected_instrument_ids",
            actual=type(value).__name__,
            expected="UUID iterable",
        )
    else:
        try:
            values = tuple(value)
        except TypeError as exc:
            raise _contract_error(
                "expected instrument ids must be UUID values",
                field="expected_instrument_ids",
                actual=type(value).__name__,
                expected="UUID iterable",
            ) from exc
    if not values:
        raise _contract_error(
            "expected_instrument_ids must not be empty",
            field="expected_instrument_ids",
            actual="empty",
            expected="one or more UUIDs",
        )
    if any(not isinstance(item, UUID) for item in values):
        raise _contract_error(
            "expected_instrument_ids entries must be UUIDs",
            field="expected_instrument_ids",
            actual=next(type(item).__name__ for item in values if not isinstance(item, UUID)),
            expected="UUID",
        )
    return tuple(sorted(set(values), key=str))


def _session_date(value: object) -> date:
    """Extract a plain date from a resolved session point or date value."""

    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    point_date = getattr(value, "session_date", None)
    if isinstance(point_date, date) and not isinstance(point_date, datetime):
        return point_date
    raise _contract_error(
        "expected_sessions entries must be dates or resolved session points",
        field="expected_sessions",
        actual=type(value).__name__,
        expected="date/session point",
    )


def _normalize_sessions(value: Iterable[date] | Iterable[object]) -> tuple[date, ...]:
    """Normalize resolved sessions without inventing natural-calendar days."""

    if isinstance(value, (str, bytes)):
        raise _contract_error(
            "expected_sessions must be an iterable of resolved sessions",
            field="expected_sessions",
            actual=type(value).__name__,
            expected="date iterable",
        )
    try:
        values = tuple(_session_date(item) for item in value)
    except TypeError as exc:
        raise _contract_error(
            "expected_sessions must be an iterable of resolved sessions",
            field="expected_sessions",
            actual=type(value).__name__,
            expected="date iterable",
        ) from exc
    if not values:
        raise _contract_error(
            "expected_sessions must not be empty",
            field="expected_sessions",
            actual="empty",
            expected="one or more resolved sessions",
        )
    # A repeated date cannot increase the number of expected logical facts.
    # Sorting makes an intentionally unordered provider input harmless while
    # preserving the resolved session dates themselves.
    return tuple(sorted(set(values)))


def _normalize_field_specs(required_fields: object) -> tuple[_FieldSpec, ...]:
    """Normalize field names with optional exact validation references.

    A sequence of strings means that the field's rule is provider-declared
    (wildcard for matching); a mapping or ``(field, rule)`` pair pins the
    expected key/version.  The wildcard never weakens fact validation: more
    than one rule materialization for a wildcard slot is a provider conflict.
    """

    entries: list[tuple[object, object]] = []
    if isinstance(required_fields, Mapping):
        entries.extend(required_fields.items())
    elif isinstance(required_fields, (str, bytes)):
        raise _contract_error(
            "required_fields must be an iterable of machine fields",
            field="required_fields",
            actual=type(required_fields).__name__,
            expected="sequence[str] or mapping[str, ContractRef]",
        )
    else:
        try:
            raw_entries = tuple(required_fields)
        except TypeError as exc:
            raise _contract_error(
                "required_fields must be an iterable of machine fields",
                field="required_fields",
                actual=type(required_fields).__name__,
                expected="sequence[str] or mapping[str, ContractRef]",
            ) from exc
        for item in raw_entries:
            if isinstance(item, str):
                entries.append((item, None))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                entries.append((item[0], item[1]))
            else:
                raise _contract_error(
                    "required_fields entries must be field names or (field, rule)",
                    field="required_fields",
                    actual=type(item).__name__,
                    expected="str or pair",
                )
    if not entries:
        raise _contract_error(
            "required_fields must not be empty",
            field="required_fields",
            actual="empty",
            expected="one or more machine fields",
        )

    # Use the same field and version validators as DataCoverageFact so the
    # expected-key contract cannot diverge from the fact-key contract.
    normalized: dict[tuple[str, tuple[str, int] | None], _FieldSpec] = {}
    for field, rule in entries:
        if not isinstance(field, str):
            raise _contract_error(
                "required field names must be strings",
                field="required_fields",
                actual=type(field).__name__,
                expected="machine field name",
            )
        # Constructing a tiny invalid-free fact is unnecessary; the private
        # helper is the canonical field validator used by the fact envelope.
        from app.backtesting.data.facts import _coverage_field

        normalized_field = _coverage_field(field)
        normalized_rule = _coverage_rule_ref(rule, "required_fields.validation_rule")
        rule_key = None if normalized_rule is None else (normalized_rule.key, normalized_rule.version)
        normalized[(normalized_field, rule_key)] = _FieldSpec(
            normalized_field,
            normalized_rule,
        )
    return tuple(
        sorted(
            normalized.values(),
            key=lambda item: (
                item.field,
                "" if item.validation_rule is None else item.validation_rule.key,
                0 if item.validation_rule is None else item.validation_rule.version,
            ),
        )
    )


def _rule_payload(fact: DataCoverageFact) -> dict[str, object] | None:
    """Serialize one fact rule into safe issue details."""

    if fact.validation_rule is None:
        return None
    return {"key": fact.validation_rule.key, "version": fact.validation_rule.version}


def _fact_key_payload(fact: DataCoverageFact) -> dict[str, object]:
    """Serialize a fact logical key into JSON-safe issue context."""

    rule = _rule_payload(fact)
    return {
        "instrument_id": str(fact.instrument_id),
        "session_date": fact.session_date.isoformat(),
        "capability": fact.capability.value,
        "field": fact.field,
        "validation_rule": rule,
        # ``rule`` is the concise name used by the stable error contract;
        # ``validation_rule`` remains for callers mirroring the fact field.
        "rule": rule,
    }


def _issue(
    code: str,
    message: str,
    *,
    fact: DataCoverageFact | None = None,
    details: Mapping[str, object] | None = None,
    severity: IssueSeverity = IssueSeverity.ERROR,
    field: str | None = None,
    issue_date: date | None = None,
) -> PreflightIssue:
    """Build one deterministic structured coverage issue."""

    payload: dict[str, object] = {}
    if fact is not None:
        payload.update(_fact_key_payload(fact))
        if issue_date is None:
            issue_date = fact.session_date
        if field is None:
            field = fact.field
    if details:
        payload.update(details)
    # Existing PreflightIssue sorting recognizes ``fact_id`` as a stable tie
    # breaker.  The logical key is not a database id, so naming it fact_id in
    # this local issue detail is deliberate: it makes report hashes invariant
    # under input order without adding identity semantics to the fact itself.
    payload.setdefault(
        "fact_id",
        canonical_json(
            {
                "instrument_id": payload.get("instrument_id"),
                "session_date": payload.get("session_date"),
                "capability": payload.get("capability"),
                "field": payload.get("field"),
                "validation_rule": payload.get("validation_rule"),
            }
        ),
    )
    return PreflightIssue(
        code=code,
        severity=severity,
        scope="coverage",
        message=message,
        instrument_id=fact.instrument_id if fact is not None else _uuid_from_payload(payload),
        field=field,
        date=issue_date,
        details=payload,
    )


def _uuid_from_payload(payload: Mapping[str, object]) -> UUID | None:
    """Recover an optional UUID from issue details without guessing values."""

    value = payload.get("instrument_id")
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _base_key(
    instrument_id: UUID,
    session_date: date,
    capability: DataCapability,
    field: str,
) -> tuple[str, str, str, str]:
    """Return a logical key without the optional rule component."""

    return (str(instrument_id), session_date.isoformat(), capability.value, field)


def _missing_ranges(
    expected_sessions: Sequence[date], missing_dates: set[date]
) -> tuple[DateRange, ...]:
    """Merge missing dates by resolved-session adjacency, not natural days."""

    ranges: list[DateRange] = []
    start: date | None = None
    previous: date | None = None
    for session_date in expected_sessions:
        if session_date not in missing_dates:
            if start is not None and previous is not None:
                ranges.append(DateRange(start_date=start, end_date=previous))
            start = previous = None
            continue
        if start is None:
            start = previous = session_date
            continue
        # ``expected_sessions`` is already the resolved ordered sequence.  A
        # holiday gap between its dates is still one session-adjacent span;
        # no new natural date is inserted into the result.
        previous = session_date
    if start is not None and previous is not None:
        ranges.append(DateRange(start_date=start, end_date=previous))
    return tuple(ranges)


def _classify_quality(fact: DataCoverageFact) -> QualityStatus:
    """Use the source-declared fact quality without repairing it."""

    return fact.quality_status


def _materialization_content(fact: DataCoverageFact) -> dict[str, object]:
    """Return immutable source content for duplicate/conflict comparison.

    ``FactEvidence.observed_at`` records when a provider read a fact, not a
    different logical fact value.  It is intentionally excluded here so a
    repeated read is deterministic and does not become a false conflict or a
    hash-changing generated-time artifact.  Knowledge time, source revision,
    quality, rule, details, and issue codes remain part of the comparison.
    """

    content = fact.machine_content()
    evidence = content.get("evidence")
    if isinstance(evidence, Mapping):
        content["evidence"] = {
            key: value for key, value in evidence.items() if key != "observed_at"
        }
    return content


def evaluate_coverage(
    expected_instrument_ids: Iterable[UUID] | UUID,
    expected_sessions: Iterable[date] | Iterable[object],
    required_fields: object,
    facts: Iterable[DataCoverageFact],
    capability: DataCapability,
    profile: object | None = None,
) -> DataCoverageReport:
    """Aggregate immutable coverage facts into the existing report type.

    The function is pure: it performs no reads and mutates no input.  Facts
    are normalized by their stable logical key.  Equal duplicate
    materializations are counted once; conflicting materializations produce
    provider-contract issues and an invalid report.  ``profile`` is accepted
    as part of the frozen call boundary for callers that carry a preflight
    profile, but deliberately does not alter raw coverage semantics.
    """

    del profile  # Profile validation belongs to preflight, not this pure layer.
    if not isinstance(capability, DataCapability):
        raise _contract_error(
            "capability must be a DataCapability",
            field="capability",
            actual=type(capability).__name__,
            expected="DataCapability",
        )
    instrument_ids = _normalize_instrument_ids(expected_instrument_ids)
    sessions = _normalize_sessions(expected_sessions)
    field_specs = _normalize_field_specs(required_fields)
    try:
        raw_facts = tuple(facts)
    except TypeError as exc:
        raise _contract_error(
            "facts must be an iterable of DataCoverageFact",
            field="facts",
            actual=type(facts).__name__,
            expected="DataCoverageFact iterable",
        ) from exc
    for fact in raw_facts:
        if not isinstance(fact, DataCoverageFact):
            raise CoverageProviderContractViolationError(
                "coverage provider returned a non-DataCoverageFact value",
                details={
                    "field": "facts",
                    "actual": type(fact).__name__,
                    "expected": "DataCoverageFact",
                },
            )

    expected_exact: dict[tuple[str, str, str, str, tuple[str, int] | None], _FieldSpec] = {}
    expected_wildcards: dict[tuple[str, str, str, str], _FieldSpec] = {}
    for instrument_id in instrument_ids:
        for session_date in sessions:
            for field_spec in field_specs:
                base = _base_key(instrument_id, session_date, capability, field_spec.field)
                rule = field_spec.validation_rule
                if rule is None:
                    expected_wildcards[base] = field_spec
                    expected_exact[(base[0], base[1], base[2], base[3], None)] = field_spec
                else:
                    expected_exact[(base[0], base[1], base[2], base[3], (rule.key, rule.version))] = field_spec

    by_exact: dict[tuple[str, str, str, str, tuple[str, int] | None], list[DataCoverageFact]] = {}
    by_base: dict[tuple[str, str, str, str], list[DataCoverageFact]] = {}
    for fact in raw_facts:
        exact_key = fact.normalized_logical_key
        base_key = _base_key(fact.instrument_id, fact.session_date, fact.capability, fact.field)
        by_exact.setdefault(exact_key, []).append(fact)
        by_base.setdefault(base_key, []).append(fact)

    issues: list[PreflightIssue] = []
    provider_contract_violation = False
    selected: dict[tuple[str, str, str, str, tuple[str, int] | None], DataCoverageFact | None] = {}
    conflict_keys: set[tuple[str, str, str, str, tuple[str, int] | None]] = set()
    consumed_exact_keys: set[tuple[str, str, str, str, tuple[str, int] | None]] = set()

    def choose_materialized(
        candidates: Sequence[DataCoverageFact],
        *,
        expected_key: tuple[str, str, str, str, tuple[str, int] | None],
    ) -> DataCoverageFact | None:
        """Deduplicate equal facts or flag an ambiguous materialization."""

        if not candidates:
            return None
        by_material: dict[str, list[DataCoverageFact]] = {}
        for candidate in candidates:
            by_material.setdefault(canonical_json(_materialization_content(candidate)), []).append(candidate)
        if len(by_material) == 1:
            return min(candidates, key=lambda item: canonical_json(_materialization_content(item)))
        nonlocal provider_contract_violation
        provider_contract_violation = True
        conflict_keys.add(expected_key)
        first = min(candidates, key=lambda item: canonical_json(_materialization_content(item)))
        conflict_details = {
            "expected": "one materialization per logical key",
            "actual": len(by_material),
            "materialization_count": len(candidates),
            "logical_key": list(first.normalized_logical_key),
        }
        issues.append(
            _issue(
                "coverage_fact_conflict",
                "同一覆盖事实键返回了内容冲突的事实，覆盖聚合已阻断",
                fact=first,
                details=conflict_details,
            )
        )
        issues.append(
            _issue(
                "coverage_provider_contract_violation",
                "覆盖提供方违反事实唯一键契约，覆盖聚合已阻断",
                fact=first,
                details={"cause_code": "coverage_fact_conflict", **conflict_details},
            )
        )
        return first

    # Resolve each expected logical slot in stable key order.  A wildcard
    # field accepts exactly one provider-declared rule version; multiple
    # versions are an ambiguity rather than an arbitrary choice.
    for expected_key in sorted(expected_exact, key=str):
        base = expected_key[:4]
        expected_rule_key = expected_key[4]
        if expected_rule_key is None and base in expected_wildcards:
            candidates = by_base.get(base, ())
            exact_rules = sorted(
                {candidate.normalized_logical_key[4] for candidate in candidates},
                key=str,
            )
            if len(exact_rules) > 1:
                provider_contract_violation = True
                first = min(candidates, key=lambda item: canonical_json(_materialization_content(item)))
                conflict_keys.add(expected_key)
                details = {
                    "expected": "one validation rule materialization",
                    "actual": [list(rule) if rule is not None else None for rule in exact_rules],
                    "logical_key": list(first.normalized_logical_key),
                }
                issues.append(
                    _issue(
                        "coverage_fact_conflict",
                        "同一字段存在多个校验规则版本，覆盖聚合已阻断",
                        fact=first,
                        details=details,
                    )
                )
                issues.append(
                    _issue(
                        "coverage_provider_contract_violation",
                        "覆盖提供方返回了无法确定规则版本的事实，覆盖聚合已阻断",
                        fact=first,
                        details={"cause_code": "coverage_fact_conflict", **details},
                    )
                )
                # Keep one deterministic representative in the slot so the
                # report denominator remains the normalized expected-key
                # count.  ``account_fact`` treats this key as invalid below;
                # the representative is never counted as complete.
                selected[expected_key] = first
                consumed_exact_keys.update(
                    candidate.normalized_logical_key for candidate in candidates
                )
                continue
            selected_fact = choose_materialized(candidates, expected_key=expected_key)
            selected[expected_key] = selected_fact
            consumed_exact_keys.update(
                candidate.normalized_logical_key for candidate in candidates
            )
            continue
        candidates = by_exact.get(expected_key, ())
        selected[expected_key] = choose_materialized(candidates, expected_key=expected_key)
        consumed_exact_keys.add(expected_key)

    # Facts outside the resolved expected scope are never silently ignored.
    # This catches wrong instrument/date/capability/field/rule results while
    # keeping the report's expected count tied to the frozen request.
    for exact_key in sorted(by_exact, key=str):
        if exact_key in consumed_exact_keys:
            continue
        first = min(
            by_exact[exact_key],
            key=lambda item: canonical_json(_materialization_content(item)),
        )
        issues.append(
            _issue(
                "coverage_provider_contract_violation",
                "覆盖事实超出请求的标的、会话、能力或字段范围，已拒绝该事实",
                fact=first,
                details={
                    "expected": "fact within requested instrument/session/capability/field scope",
                    "actual": list(exact_key),
                    "scope": "coverage",
                },
            )
        )
        provider_contract_violation = True

    complete_count = partial_count = invalid_count = unavailable_count = 0
    missing_dates: set[date] = set()
    source_revision_values: dict[str, set[str]] = {}
    not_applicable_keys: set[tuple[str, str, str, str, tuple[str, int] | None]] = set()

    def account_fact(
        expected_key: tuple[str, str, str, str, tuple[str, int] | None],
        fact: DataCoverageFact | None,
    ) -> None:
        """Account one selected fact without repair or value substitution."""

        nonlocal complete_count, partial_count, invalid_count, unavailable_count
        if fact is None:
            unavailable_count += 1
            missing_dates.add(date.fromisoformat(expected_key[1]))
            issues.append(
                _issue(
                    "coverage_required_field_missing",
                    "请求所需覆盖字段没有可见事实，已标记为 unavailable",
                    details={
                        "instrument_id": expected_key[0],
                        "session_date": expected_key[1],
                        "capability": expected_key[2],
                        "field": expected_key[3],
                        "rule": (
                            None
                            if expected_key[4] is None
                            else {"key": expected_key[4][0], "version": expected_key[4][1]}
                        ),
                        "expected": "complete coverage fact",
                        "actual": None,
                    },
                    field=expected_key[3],
                    issue_date=date.fromisoformat(expected_key[1]),
                )
            )
            return
        if expected_key in conflict_keys:
            invalid_count += 1
            missing_dates.add(fact.session_date)
            return
        if fact.applicability is CoverageApplicability.NOT_APPLICABLE:
            not_applicable_keys.add(expected_key)
            return
        quality = _classify_quality(fact)
        if quality is QualityStatus.COMPLETE:
            complete_count += 1
        elif quality is QualityStatus.PARTIAL:
            partial_count += 1
            missing_dates.add(fact.session_date)
            issues.append(
                _issue(
                    "coverage_incomplete",
                    "覆盖事实仅部分可用，未将缺失字段修复或填充",
                    fact=fact,
                    details={"quality_status": quality.value},
                )
            )
        elif quality is QualityStatus.INVALID:
            invalid_count += 1
            missing_dates.add(fact.session_date)
            issues.append(
                _issue(
                    "coverage_fact_invalid",
                    "覆盖事实未通过来源校验，已保留原始失败上下文",
                    fact=fact,
                    details={
                        "quality_status": quality.value,
                        "fact_details": fact.details,
                "validation_rule": _rule_payload(fact),
                "rule": _rule_payload(fact),
                    },
                )
            )
        else:
            unavailable_count += 1
            missing_dates.add(fact.session_date)
            issues.append(
                _issue(
                    "coverage_required_field_missing",
                    "覆盖事实无法证明请求字段可用，已标记为 unavailable",
                    fact=fact,
                    details={"quality_status": quality.value},
                )
            )
        for issue_code in fact.issue_codes:
            # Preserve provider-declared machine findings in the report while
            # keeping their text display-safe and independent of source
            # wording.  The fact's JSON details remain the audit context.
            issues.append(
                _issue(
                    issue_code,
                    "覆盖事实携带来源问题码，详见结构化事实详情",
                    fact=fact,
                    details={
                        "quality_status": quality.value,
                        "fact_details": fact.details,
                    },
                )
            )
        if fact.evidence is not None and fact.evidence.source_revision is not None:
            source_revision_values.setdefault(fact.evidence.source, set()).add(fact.evidence.source_revision)

    for expected_key in sorted(selected, key=str):
        account_fact(expected_key, selected[expected_key])

    # Explicit not-applicable declarations remove a slot from the expected
    # denominator.  Absence never reaches this branch and remains unavailable.
    expected_count = len(selected) - len(not_applicable_keys)
    if not_applicable_keys:
        complete_count = sum(
            1
            for key, fact in selected.items()
            if key not in not_applicable_keys and fact is not None and fact.quality_status is QualityStatus.COMPLETE
        )
        # Re-accounting above already ignored N/A facts; the denominator is
        # simply reduced and the other counters remain unchanged.

    if provider_contract_violation or conflict_keys or invalid_count:
        quality_status = QualityStatus.INVALID
    elif partial_count or unavailable_count:
        quality_status = QualityStatus.PARTIAL if (complete_count or partial_count) else QualityStatus.UNAVAILABLE
    elif expected_count == 0 or complete_count == expected_count:
        quality_status = QualityStatus.COMPLETE
    else:  # Defensive branch for a future quality enum extension.
        quality_status = QualityStatus.UNAVAILABLE

    if expected_count and (
        partial_count
        or invalid_count
        or unavailable_count
        or conflict_keys
        or provider_contract_violation
    ):
        issues.append(
            _issue(
                "coverage_incomplete",
                "覆盖聚合未能证明全部请求字段完整，报告保持失败关闭",
                details={
                    "expected": expected_count,
                    "actual": {
                        "complete": complete_count,
                        "partial": partial_count,
                        "invalid": invalid_count,
                        "unavailable": unavailable_count,
                    },
                    "scope": "coverage",
                },
                field=None,
                issue_date=None,
            )
        )

    revisions = {
        source: ",".join(sorted(values))
        for source, values in sorted(source_revision_values.items())
    }
    report = DataCoverageReport(
        requested_window=DateRange(start_date=sessions[0], end_date=sessions[-1]),
        capability=capability,
        instrument_ids=instrument_ids,
        expected_count=expected_count,
        complete_count=complete_count,
        partial_count=partial_count,
        invalid_count=invalid_count,
        unavailable_count=unavailable_count,
        quality_status=quality_status,
        missing_ranges=_missing_ranges(sessions, missing_dates),
        source_revisions=revisions,
        issues=tuple(issues),
    )
    return report


def coverage_report_hash(report: DataCoverageReport) -> str:
    """Compute the canonical hash of an existing coverage report."""

    if not isinstance(report, DataCoverageReport):
        raise TypeError("report must be a DataCoverageReport")
    return canonical_hash(report.machine_content())


# Descriptive aliases keep callers from inventing a second aggregation model.
aggregate_coverage = evaluate_coverage
aggregate_coverage_facts = evaluate_coverage
build_coverage_report = evaluate_coverage
