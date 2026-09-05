"""Fixed-order resolver for instrument rule packages.

The resolver executes the contractually frozen parse order and never
guesses: missing facts, invalid values, conflicting candidates, and
mode violations all surface as one immutable :class:`RulePackageResolution`
with ``status=blocked`` and machine-readable issue codes.  Ordinary
exceptions are reserved for caller bugs (unknown package reference, bad
argument types), never for data problems.

This module is free of ORM, database session, FastAPI, Tushare, and any
concrete data-source client.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from uuid import UUID

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import (
    VersionedReference,
    _is_representable,
)
from app.instruments.rules.contracts import (
    CAPABILITY_DIMENSIONS,
    FactQualityStatus,
    ParseMode,
    ResolvedFactSummary,
    RuleExceptionSetDefinition,
    RuleFactCandidate,
    RuleFieldDefinition,
    RuleFieldType,
    RulePackageDefinition,
    RulePackageIssue,
    RulePackageIssueCode,
    RulePackageResolution,
    ResolutionStatus,
    StrategyRuleDeclaration,
    TradingStatusRequirement,
    canonical_payload,
    exception_set_content_hash,
    reference_display,
    stable_hash,
)
from app.instruments.rules.registry import RulePackageRegistry


#: Bumped whenever resolution semantics change; participates in the hash.
#: Revision 2 adds fact references, knowledge times, the rule-package
#: semantic hash, and the exception-set content hash to the payload.
PARSER_REVISION = "rule-package-resolver@2"


class RulePackageResolver:
    """Resolve one instrument's rules against a registered package."""

    def __init__(self, registry: RulePackageRegistry) -> None:
        if not isinstance(registry, RulePackageRegistry):
            raise DomainValidationError("registry must be a RulePackageRegistry")
        self._registry = registry

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def resolve(
        self,
        reference: VersionedReference,
        *,
        instrument_id: UUID,
        asset_class: str,
        effective_date: date,
        data_cutoff: datetime,
        facts: Sequence[RuleFactCandidate],
        exception_sets: Sequence[RuleExceptionSetDefinition] = (),
        mode: ParseMode = ParseMode.FORMAL,
    ) -> RulePackageResolution:
        """Run the fixed parse order for one instrument instant.

        ``effective_date`` selects the validity window and ``data_cutoff``
        bounds fact visibility by ``known_at``; the two are deliberately
        separate.  Any data problem returns a blocked resolution instead
        of raising.
        """

        if not isinstance(instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        if not isinstance(effective_date, date) or isinstance(effective_date, datetime):
            raise DomainValidationError("effective_date must be a calendar date")
        cutoff = _aware_datetime(data_cutoff, "data_cutoff")
        if not isinstance(mode, ParseMode):
            raise DomainValidationError("mode must be a ParseMode")
        for candidate in facts:
            if not isinstance(candidate, RuleFactCandidate):
                raise DomainValidationError(
                    "facts must contain RuleFactCandidate instances"
                )
        for exception_set in exception_sets:
            if not isinstance(exception_set, RuleExceptionSetDefinition):
                raise DomainValidationError(
                    "exception_sets must contain RuleExceptionSetDefinition "
                    "instances"
                )

        # Step 1: exact key/version load; unknown packages raise because a
        # resolver pointed at an unregistered package is a caller bug.
        definition = self._registry.require(reference)

        issues: list[RulePackageIssue] = []
        summaries: list[ResolvedFactSummary] = []

        # Step 2: asset-class gate.
        if asset_class not in definition.supported_asset_classes:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_PACKAGE_MISMATCH,
                    instrument_id=instrument_id,
                    field="asset_class",
                    message_zh=(
                        f"资产类别 {asset_class} 不在规则包 "
                        f"{reference_display(reference)} 支持范围内"
                    ),
                    details={
                        "asset_class": asset_class,
                        "supported_asset_classes": sorted(
                            definition.supported_asset_classes
                        ),
                    },
                )
            )
            return self._finalize(
                status=ResolutionStatus.BLOCKED,
                definition=definition,
                issues=issues,
                summaries=summaries,
                normalized={},
                capability={},
                exception_reference=None,
                exception_set_reference=None,
                exception_set_references=(),
            )

        relevant = [
            candidate for candidate in facts if candidate.instrument_id == instrument_id
        ]

        # Steps 3-4: ordinary-fact package consistency and selection.
        ordinary_pool = [
            candidate
            for candidate in relevant
            if candidate.exception_fact_ref is None
            and candidate.covers(effective_date)
            and candidate.known_at <= cutoff
        ]
        for candidate in ordinary_pool:
            if candidate.package_reference != reference:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_PACKAGE_MISMATCH,
                        instrument_id=instrument_id,
                        message_zh=(
                            "普通事实的规则包引用 "
                            f"{reference_display(candidate.package_reference)} "
                            f"与目标规则包 {reference_display(reference)} 不一致"
                        ),
                        details={
                            "source": candidate.source,
                            "fact_package_reference": reference_display(
                                candidate.package_reference
                            ),
                        },
                    )
                )
        ordinary = [
            candidate
            for candidate in ordinary_pool
            if candidate.package_reference == reference
        ]
        normal_fact: RuleFactCandidate | None = None
        if len(ordinary) > 1:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_FIELD_CONFLICT,
                    instrument_id=instrument_id,
                    message_zh=(
                        "多条同等适用的普通事实同时覆盖目标有效期，"
                        "无法在不引入插入顺序偏好的情况下选择"
                    ),
                    details={
                        "sources": [
                            candidate.source for candidate in ordinary
                        ],
                    },
                )
            )
        elif not ordinary:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_FACT_NOT_COMPLETE,
                    instrument_id=instrument_id,
                    message_zh=(
                        "没有覆盖目标有效期且在数据截止点之前已知的普通事实"
                    ),
                    details={
                        "effective_date": effective_date.isoformat(),
                    },
                )
            )
        else:
            normal_fact = ordinary[0]
            summaries.append(normal_fact.summary())

        # Steps 5-6: named-exception lookup and exception-fact selection.
        matched: list[tuple[RuleExceptionSetDefinition, Any]] = []
        # Every set with a covering entry participates in the audit trail,
        # including mismatched-package sets that only produce an issue:
        # blocked runs must show which exception-set versions took part.
        participating_sets: dict[tuple[str, int], VersionedReference] = {}
        for exception_set in exception_sets:
            covering = [
                entry
                for entry in exception_set.entries
                if entry.instrument_id == instrument_id
                and entry.covers(effective_date)
            ]
            if not covering:
                continue
            participating_sets[
                (exception_set.reference.key, exception_set.reference.version)
            ] = exception_set.reference
            if exception_set.package_reference != reference:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_EXCEPTION_TARGET_MISMATCH,
                        instrument_id=instrument_id,
                        message_zh=(
                            "例外清单 "
                            f"{reference_display(exception_set.reference)} "
                            "引用的规则包 "
                            f"{reference_display(exception_set.package_reference)} "
                            f"与目标规则包 {reference_display(reference)} 不一致"
                        ),
                        details={
                            "exception_set": reference_display(
                                exception_set.reference
                            ),
                            "package_reference": reference_display(
                                exception_set.package_reference
                            ),
                        },
                    )
                )
                continue
            matched.extend((exception_set, entry) for entry in covering)

        exception_reference: VersionedReference | None = None
        exception_set_reference: VersionedReference | None = None
        # The definition of the single selected exception set, kept so its
        # order-independent content hash can join the resolution hash.
        selected_exception_set: RuleExceptionSetDefinition | None = None
        # Stable order (key, version): the audit trail must not depend on
        # the caller's exception-set iteration order.
        exception_set_references = tuple(
            sorted(
                participating_sets.values(),
                key=lambda ref: (ref.key, ref.version),
            )
        )
        exception_fact: RuleFactCandidate | None = None
        if len(matched) > 1:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_EXCEPTION_INTERVAL_CONFLICT,
                    instrument_id=instrument_id,
                    message_zh="同一标的在目标日期命中多个重叠的例外区间",
                    details={
                        "exception_sets": sorted(
                            {
                                reference_display(exception_set.reference)
                                for exception_set, _ in matched
                            }
                        ),
                    },
                )
            )
        elif len(matched) == 1:
            matched_set, entry = matched[0]
            exception_reference = entry.exception_fact_ref
            # The versioned exception-set identity is recorded alongside
            # the fact reference so audits can tell which set version was
            # actually used even when two versions share a fact reference.
            exception_set_reference = matched_set.reference
            selected_exception_set = matched_set
            # Strict identity: the referenced fact row must carry exactly
            # this key/version as its own fact_reference (and be marked as
            # exception-sourced for it).  A different fact version — even
            # of the same key — must never stand in for the declared one.
            pool = [
                candidate
                for candidate in relevant
                if candidate.fact_reference == entry.exception_fact_ref
                and candidate.exception_fact_ref == entry.exception_fact_ref
                and candidate.covers(effective_date)
                and candidate.known_at <= cutoff
            ]
            for candidate in pool:
                if candidate.package_reference != reference:
                    issues.append(
                        self._issue(
                            RulePackageIssueCode.RULE_EXCEPTION_TARGET_MISMATCH,
                            instrument_id=instrument_id,
                            message_zh=(
                                "例外事实 "
                                f"{reference_display(entry.exception_fact_ref)} "
                                "的规则包引用与目标规则包不一致"
                            ),
                            details={
                                "source": candidate.source,
                                "fact_package_reference": reference_display(
                                    candidate.package_reference
                                ),
                            },
                        )
                    )
            usable = [
                candidate
                for candidate in pool
                if candidate.package_reference == reference
            ]
            if not usable:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_EXCEPTION_FACT_MISSING,
                        instrument_id=instrument_id,
                        message_zh=(
                            "例外声明存在，但其引用的例外事实 "
                            f"{reference_display(entry.exception_fact_ref)} "
                            "缺失或已过期"
                        ),
                        details={
                            "exception_fact_ref": reference_display(
                                entry.exception_fact_ref
                            ),
                        },
                    )
                )
            elif len(usable) > 1:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_FIELD_CONFLICT,
                        instrument_id=instrument_id,
                        message_zh="多条同等适用的例外事实同时命中同一例外引用",
                        details={
                            "sources": [
                                candidate.source for candidate in usable
                            ],
                        },
                    )
                )
            else:
                exception_fact = usable[0]
                summaries.append(
                    exception_fact.summary(
                        exception_set_reference=exception_set_reference
                    )
                )

        # Step 7: overlay exception fields onto ordinary fields.  Overlay
        # routing is retained from the frozen parse order, but the
        # exception fact must be a *complete* fact row on its own: any
        # required field it does not provide blocks the resolution instead
        # of being silently filled from the ordinary fact.
        merged: dict[str, Any] = {}
        if normal_fact is not None:
            merged.update(normal_fact.fields)
        if exception_fact is not None:
            missing_in_exception = [
                field_definition.name
                for field_definition in definition.field_definitions
                if field_definition.required
                and field_definition.name not in exception_fact.fields
            ]
            if missing_in_exception:
                for missing_name in sorted(missing_in_exception):
                    issues.append(
                        self._issue(
                            RulePackageIssueCode.RULE_REQUIRED_FIELD_MISSING,
                            instrument_id=instrument_id,
                            field=missing_name,
                            message_zh=(
                                "例外事实 "
                                f"{reference_display(exception_fact.fact_reference)} "
                                f"自身缺少必填字段 {missing_name}，"
                                "禁止从普通事实隐式补齐"
                            ),
                            details={
                                "exception_fact_reference": reference_display(
                                    exception_fact.fact_reference
                                ),
                            },
                        )
                    )
            merged.update(exception_fact.fields)

        # Steps 8-10: required-field presence, per-field validation, and
        # explicit capability declarations.
        normalized: dict[str, Any] = {}
        for field_definition in definition.field_definitions:
            name = field_definition.name
            if name not in merged:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_REQUIRED_FIELD_MISSING,
                        instrument_id=instrument_id,
                        field=name,
                        message_zh=f"必填字段 {name} 缺失，禁止使用默认值回退",
                    )
                )
                continue
            value, issue = _normalize_field_value(
                field_definition, merged[name]
            )
            if issue is not None:
                issue = RulePackageIssue(
                    code=issue.code,
                    message=issue.message,
                    field=name,
                    instrument_id=instrument_id,
                    details=issue.details,
                )
                issues.append(issue)
                continue
            normalized[name] = value
            if field_definition.value_type is (
                RuleFieldType.TRADING_STATUS_APPLICABILITY
            ):
                missing_dimensions = [
                    dimension
                    for dimension in definition.capability_schema
                    if dimension not in value
                ]
                if missing_dimensions:
                    issues.append(
                        self._issue(
                            RulePackageIssueCode
                            .RULE_CAPABILITY_DECLARATION_MISSING,
                            instrument_id=instrument_id,
                            field=name,
                            message_zh=(
                                "交易状态适用性缺少显式声明维度："
                                f"{', '.join(missing_dimensions)}"
                            ),
                            details={"missing_dimensions": missing_dimensions},
                        )
                    )

        # Step 9 (cross-field constraints).
        issues.extend(
            self._cross_field_issues(definition, normalized, instrument_id)
        )

        # Steps 11-12: settlement-class recognition and formal gating.
        settlement = normalized.get("settlement_rule_class")
        if isinstance(settlement, str):
            if settlement not in definition.known_settlement_rule_classes:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_SETTLEMENT_UNKNOWN,
                        instrument_id=instrument_id,
                        field="settlement_rule_class",
                        message_zh=(
                            f"结算类别 {settlement} 无法识别，"
                            "不得默认为首期支持类别"
                        ),
                        details={"settlement_rule_class": settlement},
                    )
                )
            elif settlement not in definition.formal_settlement_rule_classes:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_SETTLEMENT_UNSUPPORTED,
                        instrument_id=instrument_id,
                        field="settlement_rule_class",
                        message_zh=(
                            f"结算类别 {settlement} 可以识别，"
                            "但不在首期正式支持范围内"
                        ),
                        details={
                            "settlement_rule_class": settlement,
                            "formal_settlement_rule_classes": sorted(
                                definition.formal_settlement_rule_classes
                            ),
                        },
                    )
                )

        # Step 13: source quality and mode gating on contributing facts.
        for candidate in (normal_fact, exception_fact):
            if candidate is None:
                continue
            if candidate.fixture_only and mode is ParseMode.FORMAL:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_FIXTURE_SOURCE_FORBIDDEN,
                        instrument_id=instrument_id,
                        message_zh=(
                            f"正式模式拒绝测试 fixture 事实（来源 {candidate.source}）"
                        ),
                        details={"source": candidate.source},
                    )
                )
            if candidate.quality_status is FactQualityStatus.INCOMPLETE:
                issues.append(
                    self._issue(
                        RulePackageIssueCode.RULE_FACT_NOT_COMPLETE,
                        instrument_id=instrument_id,
                        message_zh=(
                            f"事实来源 {candidate.source} 的质量标记为不完整"
                        ),
                        details={
                            "source": candidate.source,
                            "source_revision": candidate.source_revision,
                        },
                    )
                )

        # Step 14: immutable resolution, provenance summary, stable hash.
        if issues:
            return self._finalize(
                status=ResolutionStatus.BLOCKED,
                definition=definition,
                issues=issues,
                summaries=summaries,
                normalized={},
                capability={},
                exception_reference=exception_reference,
                exception_set_reference=exception_set_reference,
                exception_set_references=exception_set_references,
                selected_exception_set=selected_exception_set,
            )
        capability = dict(normalized["trading_status_applicability"])
        return self._finalize(
            status=ResolutionStatus.READY,
            definition=definition,
            issues=(),
            summaries=summaries,
            normalized=normalized,
            capability=capability,
            exception_reference=exception_reference,
            exception_set_reference=exception_set_reference,
            exception_set_references=exception_set_references,
            selected_exception_set=selected_exception_set,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _finalize(
        self,
        *,
        status: ResolutionStatus,
        definition: RulePackageDefinition,
        issues: Sequence[RulePackageIssue],
        summaries: Sequence[ResolvedFactSummary],
        normalized: Mapping[str, Any],
        capability: Mapping[str, str],
        exception_reference: VersionedReference | None,
        exception_set_reference: VersionedReference | None,
        exception_set_references: tuple[VersionedReference, ...],
        selected_exception_set: RuleExceptionSetDefinition | None = None,
    ) -> RulePackageResolution:
        """Build the immutable resolution and its stable semantic hash.

        Both statuses hash the same provenance core (package identity and
        semantic hash, exception fact and set references plus the set
        content hash, parse order, and per-fact provenance including the
        exact ``fact_reference`` and ``known_at``).  Issue messages never
        participate in the hash; ``observed_at`` is audit-only.
        """

        # Order-independent content hash of the selected exception set;
        # ``None`` when no single set was selected (no match or conflict).
        exception_set_hash = (
            exception_set_content_hash(selected_exception_set)
            if selected_exception_set is not None
            else None
        )
        normal_summaries = [
            summary for summary in summaries if summary.exception_set_reference is None
        ]
        exception_summaries = [
            summary for summary in summaries if summary.exception_set_reference is not None
        ]
        fact_provenance = [
            {
                "fact_reference": summary.fact_reference,
                "source": summary.source,
                "source_revision": summary.source_revision,
                "known_at": summary.known_at,
                "exception_fact_ref": summary.exception_fact_ref,
                "exception_set_reference": summary.exception_set_reference,
                "valid_from": summary.valid_from,
                "valid_to": summary.valid_to,
            }
            for summary in summaries
        ]
        def _unique_reference_payloads(
            summaries_: Sequence[ResolvedFactSummary],
        ) -> list[dict[str, Any]]:
            """Deduplicate fact references as canonical payloads, stably."""

            unique: dict[tuple[str, int], dict[str, Any]] = {}
            for summary_ in summaries_:
                payload = canonical_payload(summary_.fact_reference)
                unique[(payload["key"], payload["version"])] = payload
            return [unique[identity] for identity in sorted(unique)]

        provenance_core = {
            "kind": "rule_package_resolution",
            "status": status,
            "parser_revision": PARSER_REVISION,
            "package_reference": definition.reference,
            "package_semantic_hash": definition.semantic_hash,
            "exception_reference": exception_reference,
            "exception_set_reference": exception_set_reference,
            "exception_set_hash": exception_set_hash,
            "exception_set_references": exception_set_references,
            "normal_fact_references": _unique_reference_payloads(
                normal_summaries
            ),
            "exception_fact_references": _unique_reference_payloads(
                exception_summaries
            ),
            "parse_order": definition.parse_order,
            "selected_facts": fact_provenance,
        }
        if status is ResolutionStatus.READY:
            payload = {
                **provenance_core,
                "normalized_values": dict(normalized),
                "capability_declarations": dict(capability),
            }
        else:
            # Blocked resolutions keep their full fact provenance in the
            # hash: two fact revisions triggering the same issue codes
            # must stay distinguishable for preflight/run audits.
            payload = {
                **provenance_core,
                "issue_identities": sorted(
                    (issue.code, issue.field) for issue in issues
                ),
            }
        return RulePackageResolution(
            status=status,
            package_reference=definition.reference,
            exception_reference=exception_reference,
            exception_set_reference=exception_set_reference,
            exception_set_references=exception_set_references,
            selected_facts=tuple(summaries),
            normalized_values=dict(normalized),
            capability_declarations={
                str(key): str(value) for key, value in capability.items()
            },
            parse_order=definition.parse_order,
            parser_revision=PARSER_REVISION,
            semantic_hash=stable_hash(canonical_payload(payload)),
            issues=tuple(issues),
        )

    def _cross_field_issues(
        self,
        definition: RulePackageDefinition,
        normalized: Mapping[str, Any],
        instrument_id: UUID,
    ) -> list[RulePackageIssue]:
        """Validate cross-field constraints among already-valid values."""

        issues: list[RulePackageIssue] = []

        def _invalid(field: str, reason_zh: str, details: dict[str, Any]) -> None:
            issues.append(
                self._issue(
                    RulePackageIssueCode.RULE_FIELD_INVALID,
                    instrument_id=instrument_id,
                    field=field,
                    message_zh=reason_zh,
                    details=details,
                )
            )

        quantity_precision = normalized.get("quantity_precision")
        price_precision = normalized.get("price_precision")
        lot_size = normalized.get("lot_size")
        price_tick = normalized.get("price_tick")
        minimum_order_quantity = normalized.get("minimum_order_quantity")
        order_types = normalized.get("order_types")

        if isinstance(quantity_precision, int) and isinstance(lot_size, Decimal):
            if not _is_representable(lot_size, quantity_precision):
                _invalid(
                    "lot_size",
                    "lot_size 无法按 quantity_precision 精确表示",
                    {"quantity_precision": quantity_precision},
                )
        if isinstance(price_precision, int) and isinstance(price_tick, Decimal):
            if not _is_representable(price_tick, price_precision):
                _invalid(
                    "price_tick",
                    "price_tick 无法按 price_precision 精确表示",
                    {"price_precision": price_precision},
                )
        if isinstance(minimum_order_quantity, Decimal):
            if isinstance(quantity_precision, int) and not _is_representable(
                minimum_order_quantity, quantity_precision
            ):
                _invalid(
                    "minimum_order_quantity",
                    "minimum_order_quantity 无法按 quantity_precision 精确表示",
                    {"quantity_precision": quantity_precision},
                )
            if isinstance(lot_size, Decimal) and (
                minimum_order_quantity % lot_size != 0
            ):
                _invalid(
                    "minimum_order_quantity",
                    "minimum_order_quantity 必须是 lot_size 的整数倍",
                    {
                        "minimum_order_quantity": str(minimum_order_quantity),
                        "lot_size": str(lot_size),
                    },
                )
        if isinstance(order_types, tuple) and "market" not in order_types:
            _invalid(
                "order_types",
                "首期 order_types 必须包含 market",
                {"order_types": list(order_types)},
            )
        return issues

    @staticmethod
    def _issue(
        code: RulePackageIssueCode,
        *,
        message_zh: str,
        field: str | None = None,
        instrument_id: UUID | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> RulePackageIssue:
        return RulePackageIssue(
            code=code,
            message=message_zh,
            field=field,
            instrument_id=instrument_id,
            details=details,
        )


# ---------------------------------------------------------------------------
# Per-field normalization
# ---------------------------------------------------------------------------


def _normalize_field_value(
    field_definition: RuleFieldDefinition,
    raw: Any,
) -> tuple[Any, RulePackageIssue | None]:
    """Normalize one raw fact value per its contracted type.

    Returns ``(value, None)`` on success or ``(None, issue)`` with a
    ``RULE_FIELD_INVALID`` (or settlement-specific) issue on failure.
    Binary floats, booleans, NaN, infinities, and unparsable strings are
    always rejected.
    """

    name = field_definition.name
    value_type = field_definition.value_type
    prefix = f"字段 {name} "

    if value_type is RuleFieldType.POSITIVE_DECIMAL:
        if isinstance(raw, bool) or isinstance(raw, float):
            return None, _invalid_issue(prefix + "必须为精确十进制数，不接受布尔值或浮点数")
        if isinstance(raw, Decimal):
            value = raw
        elif isinstance(raw, (int, str)):
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                return None, _invalid_issue(prefix + "不是可解析的十进制数")
        else:
            return None, _invalid_issue(prefix + "的值类型不受支持")
        if not value.is_finite():
            return None, _invalid_issue(prefix + "必须为有限数值")
        if value <= 0:
            return None, _invalid_issue(prefix + "必须为正数")
        return value, None

    if value_type is RuleFieldType.NON_NEGATIVE_INT:
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, _invalid_issue(prefix + "必须为非负整数，不接受布尔值或浮点数")
        if raw < 0:
            return None, _invalid_issue(prefix + "不能为负数")
        return raw, None

    if value_type is RuleFieldType.VERSIONED_REFERENCE:
        return _normalize_versioned_reference(prefix, raw)

    if value_type is RuleFieldType.STRATEGY_RULE:
        return _normalize_strategy_rule(prefix, raw)

    if value_type is RuleFieldType.SETTLEMENT_CLASS:
        if not isinstance(raw, str) or not raw.strip():
            return None, _invalid_issue(prefix + "必须为非空字符串")
        return raw.strip(), None

    if value_type is RuleFieldType.STRING_SET:
        if isinstance(raw, (str, bytes)) or not hasattr(raw, "__iter__"):
            return None, _invalid_issue(prefix + "必须为字符串集合")
        members: list[str] = []
        for member in raw:
            if not isinstance(member, str) or not member.strip():
                return None, _invalid_issue(prefix + "的每个成员必须为非空字符串")
            members.append(member.strip())
        unique = tuple(sorted(set(members)))
        if not unique:
            return None, _invalid_issue(prefix + "不能为空集合")
        return unique, None

    if value_type is RuleFieldType.CURRENCY_CODE:
        if not isinstance(raw, str) or not raw.strip():
            return None, _invalid_issue(prefix + "必须为非空字符串")
        return raw.strip().upper(), None

    if value_type is RuleFieldType.TRADING_STATUS_APPLICABILITY:
        if not isinstance(raw, Mapping):
            return None, _invalid_issue(prefix + "必须为维度到枚举的映射")
        normalized: dict[str, str] = {}
        for key, value in raw.items():
            if key not in CAPABILITY_DIMENSIONS:
                return None, _invalid_issue(
                    prefix + f"包含未知维度 {key!r}", {"unknown_dimension": str(key)}
                )
            try:
                requirement = TradingStatusRequirement(value)
            except ValueError:
                return None, _invalid_issue(
                    prefix + f"维度 {key} 的取值必须为 required 或 not_applicable",
                    {"dimension": str(key), "value": str(value)},
                )
            normalized[key] = requirement.value
        return normalized, None

    raise DomainValidationError(
        f"unhandled rule field type {value_type} for field {name}"
    )


def _normalize_versioned_reference(
    prefix: str, raw: Any
) -> tuple[VersionedReference, RulePackageIssue | None]:
    """Accept a ``VersionedReference`` or an equivalent ``{key, version}`` map."""

    if isinstance(raw, VersionedReference):
        return raw, None
    if isinstance(raw, Mapping):
        key = raw.get("key")
        version = raw.get("version")
        try:
            return VersionedReference(key=key, version=version), None
        except DomainValidationError:
            return None, _invalid_issue(prefix + "必须是合法的版本化引用")
    return None, _invalid_issue(prefix + "必须是版本化引用或 {key, version} 映射")


def _normalize_strategy_rule(
    prefix: str, raw: Any
) -> tuple[Any, RulePackageIssue | None]:
    """Accept a versioned rule reference or a strong-typed declaration."""

    if isinstance(raw, StrategyRuleDeclaration):
        return raw, None
    if isinstance(raw, Mapping) and "statements" in raw:
        statements = raw["statements"]
        try:
            return StrategyRuleDeclaration(statements=tuple(statements)), None
        except DomainValidationError:
            return None, _invalid_issue(prefix + "的策略声明语句不能为空")
    return _normalize_versioned_reference(prefix, raw)


def restore_normalized_values(
    definition: RulePackageDefinition,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Restore canonical-JSON snapshot values into domain-normalized types.

    Run snapshots persist ``normalized_values`` in canonical JSON form
    (decimal strings, ``{key, version}`` maps, lists).  Execution must not
    consume those raw JSON types: this function re-runs the resolver's
    per-field normalization so a restored segment carries exactly the same
    ``Decimal``/``VersionedReference``/tuple values the original ready
    resolution produced.  Unknown or invalid entries are rejected instead
    of being passed through.
    """

    if not isinstance(payload, Mapping):
        raise DomainValidationError("payload must be a mapping")
    known = {field.name for field in definition.field_definitions}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise DomainValidationError(
            f"stored normalized values contain fields unknown to rule "
            f"package {reference_display(definition.reference)}: {unknown}"
        )
    restored: dict[str, Any] = {}
    for field_definition in definition.field_definitions:
        name = field_definition.name
        if name not in payload:
            continue
        value, issue = _normalize_field_value(field_definition, payload[name])
        if issue is not None:
            raise DomainValidationError(
                f"stored normalized value for field {name} is invalid: "
                f"{issue.message}"
            )
        restored[name] = value
    return restored


def _invalid_issue(
    message_zh: str, details: Mapping[str, Any] | None = None
) -> RulePackageIssue:
    return RulePackageIssue(
        code=RulePackageIssueCode.RULE_FIELD_INVALID,
        message=message_zh,
        details=details,
    )
