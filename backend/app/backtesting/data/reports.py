"""Coverage reports, admission preflight reports, and the canonical hash.

The hash is produced by exactly one normalization in this module
(:func:`canonical_json` plus SHA-256); providers never define their own
hash semantics.  The canonical form sorts mapping keys, renders decimals
as fixed-point strings, and orders collections stably, so equivalent
reports hash identically even when constructed in different input order,
with different Chinese display messages, or generated at different times.

Excluded from every hash: human-readable ``message`` text, generation
timestamps, elapsed timings, database primary keys, raw tokens, and any
credential material.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import (
    CalendarAxisDifference,
    CalendarAxisStatus,
    CalendarDefinition,
    SessionPoint,
    normalize_calendar_id,
)
from app.backtesting.data.errors import (
    CalendarPreflightResourceLimitExceededError,
    InvalidDataRequestError,
    UniversePreflightHashMismatchError,
    freeze_json,
)
from app.backtesting.data.requests import (
    CALENDAR_AXIS_POLICY,
    CHUNK_POLICY,
    DATA_CONTRACT_VERSION,
    MAX_LOOKBACK_SESSIONS,
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DateRange,
    InstrumentScopeMode,
    IssueSeverity,
    MarketScope,
    PreflightStatus,
    PriceBasis,
    QualityMode,
    QualityStatus,
    UniverseQueryPolicy,
    _aware_datetime,
    _non_blank_text,
    _sorted_unique_enum,
    _sorted_unique_text,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "DataCoverageReport",
    "DataPreflightReport",
    "PreflightIssue",
    "canonical_hash",
    "canonical_json",
    "calendar_semantic_signature",
    "calendar_revision_digest",
    "calendar_session_signature",
    "warmup_session_signature",
    "calendar_snapshot_fingerprint",
    "data_preflight_hash_v2",
]


# ---------------------------------------------------------------------------
# Canonical serialization and hashing
# ---------------------------------------------------------------------------

_HEX_ALPHABET = frozenset("0123456789abcdef")


def _canonical_value(value: object, where: str) -> object:
    """Convert one value into its canonical JSON-serializable form."""

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        # Finite floats serialize directly; NaN/inf are rejected because
        # they have no deterministic JSON representation.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{where} float values must be finite")
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        # Also covers datetime.time via its own branch below.
        return value.isoformat()
    try:
        from datetime import time as _time

        if isinstance(value, _time):
            return value.isoformat()
    except ImportError:  # pragma: no cover - stdlib import cannot fail
        pass
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        canonical: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{where} mapping keys must be plain strings")
            canonical[key] = _canonical_value(item, f"{where}[{key!r}]")
        return canonical
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, where) for item in value]
    raise ValueError(
        f"{where} contains a value that has no canonical JSON form: "
        f"{type(value).__name__}"
    )


def canonical_json(value: object) -> str:
    """Serialize ``value`` into deterministic canonical JSON text.

    Mapping keys are sorted, separators are compact, and non-ASCII text is
    preserved so identical logical content always yields identical bytes.
    """

    canonical = _canonical_value(value, "value")
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: object) -> str:
    """SHA-256 hex digest of :func:`canonical_json` output."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _freeze_domain_json(value: object, field_name: str) -> object:
    """Normalize domain scalars before applying the strict JSON freezer.

    Candidate audit evidence naturally contains UUID/date/time/Decimal and
    enum values.  ``freeze_json`` intentionally accepts only JSON scalars, so
    convert those domain values with the same canonical serializer used by
    report hashes rather than leaking Python objects into persisted details.
    """

    _reject_sensitive_audit_keys(value, field_name)
    normalized = _canonical_value(value, field_name)
    return freeze_json(normalized, field_name)


_SENSITIVE_AUDIT_KEY_MARKERS = frozenset(
    {
        "credential",
        "credentials",
        "token",
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "password",
        "secret",
        "private_key",
        "access_key",
    }
)


def _reject_sensitive_audit_keys(value: object, where: str) -> None:
    """Reject credential-shaped keys before they enter report JSON.

    Audit projections may preserve hashes and non-sensitive provider context,
    but a nested credential key must fail closed rather than be redacted after
    the fact.  Values are never included in the exception details.
    """

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized = key.strip().lower().replace("-", "_").replace(".", "_")
            if any(marker in normalized for marker in _SENSITIVE_AUDIT_KEY_MARKERS):
                raise InvalidDataRequestError(
                    f"{where} contains a sensitive audit key",
                    details={"field": where, "key": key},
                )
            _reject_sensitive_audit_keys(item, f"{where}[{key!r}]")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_sensitive_audit_keys(item, f"{where}[{index}]")


def _require_hash_digest(value: str, field_name: str) -> str:
    """Require a lowercase SHA-256 hex digest."""

    text = _non_blank_text(value, field_name)
    if len(text) != 64 or any(character not in _HEX_ALPHABET for character in text):
        raise InvalidDataRequestError(
            f"{field_name} must be a lowercase SHA-256 hex digest"
        )
    return text


def _strict_version(value: object) -> int:
    """Require exactly the data-contract version this package implements."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataRequestError("data_contract_version must be an integer")
    if value != DATA_CONTRACT_VERSION:
        raise InvalidDataRequestError(
            f"unsupported data_contract_version {value}; this package "
            f"implements version {DATA_CONTRACT_VERSION} only"
        )
    return value


# ---------------------------------------------------------------------------
# Structured preflight issues
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightIssue:
    """One structured preflight finding with complete calendar evidence.

    ``title`` and ``message`` are display fields, while ``date_range``,
    ``calendar_id`` and ``values_by_calendar`` make a blocked calendar issue
    self-contained.  The machine evidence participates in report hashing;
    wording remains intentionally excluded.
    """

    code: str
    severity: IssueSeverity
    scope: str
    message: str
    instrument_id: UUID | None = None
    field: str | None = None
    details: Mapping[str, object] | None = None
    title: str = "预检问题"
    date: date | None = None
    date_range: tuple[date, date] | None = None
    calendar_id: str | None = None
    values_by_calendar: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code.strip():
            raise InvalidDataRequestError("issue code must be non-blank text")
        if not isinstance(self.severity, IssueSeverity):
            raise InvalidDataRequestError("issue severity must be an IssueSeverity")
        if type(self.scope) is not str or not self.scope.strip():
            raise InvalidDataRequestError("issue scope must be non-blank text")
        if type(self.message) is not str or not self.message.strip():
            raise InvalidDataRequestError("issue message must be non-blank text")
        if type(self.title) is not str or not self.title.strip():
            raise InvalidDataRequestError("issue title must be non-blank text")
        title = self.title.strip()
        if title == "预检问题":
            title = {
                "CALENDAR_FACT_MISSING": "交易日事实缺失",
                "CALENDAR_DEFINITION_MISSING": "日历定义缺失",
                "CALENDAR_SESSION_INCOMPATIBLE": "日历会话不兼容",
                "DATA_CUTOFF_REQUIRED": "缺少数据截止时间",
                "DATA_CUTOFF_EXCEEDED": "超过数据截止时间",
                "WARMUP_COVERAGE_INSUFFICIENT": "warmup 覆盖不足",
                "CALENDAR_PREFLIGHT_RESOURCE_LIMIT_EXCEEDED": "预检资源超限",
                "UNSUPPORTED_CAPABILITY": "能力声明不受支持",
                "CALENDAR_DEFINITION_AMBIGUOUS": "日历定义有歧义",
                "CALENDAR_DEFINITION_INVALID": "日历定义无效",
                "CALENDAR_FACT_AMBIGUOUS": "交易日事实有歧义",
                "CALENDAR_FACT_INVALID": "交易日事实无效",
                "CALENDAR_SESSION_UNRESOLVED": "会话时段未解析",
                "CALENDAR_SESSION_INVALID": "日历窗口无效",
                "CALENDAR_TIMEZONE_INCONSISTENT": "日历时区跨日不一致",
                "CALENDAR_TIMEZONE_MISMATCH": "参与日历时区不一致",
                "CALENDAR_TIMEZONE_UNSUPPORTED": "首版时区不支持",
                "CALENDAR_REGISTRY_FACT_MISSING": "日历注册事实缺失",
                "CALENDAR_REGISTRY_REFERENCE_INVALID": "日历注册引用无效",
                "CALENDAR_REGISTRY_AMBIGUOUS": "日历注册版本有歧义",
                "CALENDAR_BINDING_UNKNOWN": "交易所绑定缺失",
                "CALENDAR_BINDING_AMBIGUOUS": "交易所绑定有歧义",
                "CALENDAR_PIT_METADATA_MISSING": "缺少历史认知证据",
                "CALENDAR_SNAPSHOT_COVERAGE_UNKNOWN": "日历覆盖未知",
                "CALENDAR_SOURCE_PRIORITY_MISSING": "来源优先级缺失",
                "CALENDAR_SOURCE_PRIORITY_INVALID": "来源优先级无效",
                "CALENDAR_SOURCE_PRIORITY_AMBIGUOUS": "来源优先级有歧义",
                "CALENDAR_SOURCE_PRIORITY_CHAIN_BROKEN": "来源优先级版本链断裂",
                "CALENDAR_SOURCE_REVISION_CONFLICT": "来源修订有冲突",
                "CALENDAR_SNAPSHOT_REVISION_CHANGED": "日历快照版本发生变化",
                "CALENDAR_SNAPSHOT_RETRY_EXHAUSTED": "日历快照重试失败",
                "CALENDAR_SNAPSHOT_COVERAGE_UNKNOWN": "日历覆盖范围未知",
                "CALENDAR_DATE_SPAN_LIMIT_EXCEEDED": "日历日期跨度超限",
                "LOOKBACK_SESSIONS_LIMIT_EXCEEDED": "历史会话数量超限",
                "CALENDAR_PREFLIGHT_RESOURCE_LIMIT_EXCEEDED": "预检资源超限",
                "INSTRUMENT_CALENDAR_UNRESOLVED": "标的交易日历未解析",
                "CALENDAR_ID_SET_EMPTY": "未解析到交易日历",
                "CALENDAR_ID_UNKNOWN": "未知交易日历",
                "UNIVERSE_CALENDAR_NOT_PREFLIGHTED": "动态日历未预检",
                "UNIVERSE_SCOPE_UNRESOLVED": "动态候选范围未解析",
                "UNIVERSE_CAPABILITY_MISSING": "动态候选能力缺失",
                "UNIVERSE_PIT_BOUNDARY_VIOLATION": "候选超出 PIT 边界",
                "UNIVERSE_TARGET_OUTSIDE_SCOPE": "候选目标越出范围",
                "UNIVERSE_SELECTED_INELIGIBLE": "选中候选资格不完整",
                "UNIVERSE_PREFLIGHT_HASH_MISMATCH": "候选范围预检哈希不一致",
                "UNIVERSE_PROVIDER_CONTRACT_VIOLATION": "候选 Provider 契约错误",
                "CANDIDATE_IDENTITY_INCOMPLETE": "候选身份事实不完整",
                "CANDIDATE_MAPPING_INCOMPLETE": "候选代码映射不完整",
                "CANDIDATE_RULE_INCOMPLETE": "候选规则资格不完整",
                "CANDIDATE_MARKET_DATA_INCOMPLETE": "候选行情覆盖不完整",
                "CANDIDATE_CORPORATE_ACTION_INCOMPLETE": "候选公司行动资格不完整",
                "CANDIDATE_QUANTITY_ACTION_COVERAGE_INCOMPLETE": "候选数量类覆盖不完整",
                "CANDIDATE_STATUS_INCOMPLETE": "候选交易状态资格不完整",
                "UNSUPPORTED_CAPABILITY": "数据能力不支持",
                "CAPABILITY_DECLARATION_AMBIGUOUS": "能力声明有歧义",
                "CAPABILITY_DECLARATION_INVALID": "能力声明无效",
                "DATA_PREFLIGHT_BLOCKED": "数据预检未通过",
                "PROVIDER_CONTRACT_VIOLATION": "数据提供方契约错误",
            }.get(self.code.upper(), title)
        object.__setattr__(self, "title", title)
        if self.date is not None and (
            not isinstance(self.date, date) or isinstance(self.date, datetime)
        ):
            raise InvalidDataRequestError("issue date must be a calendar date")
        if self.date_range is not None:
            if not isinstance(self.date_range, (tuple, list)) or len(self.date_range) != 2:
                raise InvalidDataRequestError("issue date_range must contain [start, end)")
            start, end = self.date_range
            if (
                not isinstance(start, date) or isinstance(start, datetime)
                or not isinstance(end, date) or isinstance(end, datetime)
                or start >= end
            ):
                raise InvalidDataRequestError("issue date_range must be an ordered half-open date range")
            object.__setattr__(self, "date_range", (start, end))
        if self.calendar_id is not None:
            try:
                object.__setattr__(self, "calendar_id", normalize_calendar_id(self.calendar_id))
            except Exception as exc:
                raise InvalidDataRequestError("issue calendar_id must be canonical") from exc
        if self.values_by_calendar is not None:
            if not isinstance(self.values_by_calendar, Mapping):
                raise InvalidDataRequestError("issue values_by_calendar must be a mapping")
            frozen_values = freeze_json(dict(self.values_by_calendar), "issue values_by_calendar")
            assert isinstance(frozen_values, MappingProxyType)
            object.__setattr__(self, "values_by_calendar", frozen_values)
        if self.instrument_id is not None and not isinstance(
            self.instrument_id, UUID
        ):
            raise InvalidDataRequestError("issue instrument_id must be a UUID")
        if self.field is not None and (
            type(self.field) is not str or not self.field.strip()
        ):
            raise InvalidDataRequestError("issue field must be non-blank text")
        if self.details is not None:
            if not isinstance(self.details, Mapping):
                raise InvalidDataRequestError(
                    "issue details must be a mapping of JSON values"
                )
            frozen = freeze_json(dict(self.details), "issue details")
            assert isinstance(frozen, MappingProxyType)
            object.__setattr__(self, "details", frozen)

    @property
    def sort_key(self) -> tuple[int, int, str, str, int, str, str]:
        """Stable ordering key built from all machine fields.

        ``details`` participates through its canonical JSON form so that
        two issues differing only in details never tie: ties would keep
        input order under stable sorting and make hashes depend on it.
        """

        fact_id = ""
        if isinstance(self.details, Mapping):
            for key in ("fact_id", "selected_fact_id", "definition_fact_id"):
                value = self.details.get(key)
                if value is not None:
                    fact_id = str(value)
                    break
        return (
            _issue_stage_rank(self.code),
            _issue_scope_rank(self.scope),
            (self.date.isoformat() if self.date is not None else (self.date_range[0].isoformat() if self.date_range else "")),
            self.calendar_id or "",
            _issue_field_rank(self.field),
            self.code,
            fact_id or (str(self.instrument_id) if self.instrument_id else ""),
        )

    def as_dict(self) -> dict[str, object]:
        """Return the complete wire projection for synchronous blocked APIs."""

        payload: dict[str, object] = {
            "code": self.code,
            "title": self.title,
            "message": self.message,
            "scope": self.scope,
            "field": self.field,
            "calendar_id": self.calendar_id,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "date": self.date,
            "date_range": list(self.date_range) if self.date_range else None,
            "values_by_calendar": self.values_by_calendar,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload

    def machine_fields(self) -> dict[str, object]:
        """Hash-relevant content of this issue (message/title excluded)."""

        return {
            "code": self.code,
            "severity": self.severity.value,
            "scope": self.scope,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "field": self.field,
            "date": self.date,
            "date_range": list(self.date_range) if self.date_range else None,
            "calendar_id": self.calendar_id,
            "values_by_calendar": self.values_by_calendar,
            "details": self.details,
        }


def _issue_stage_rank(code: str) -> int:
    """Map issue codes to the fixed task-11 admission stage order."""

    upper = code.upper()
    if upper.startswith((
        "DATA_",
        "INVALID_",
        "REQUEST_",
        "CALENDAR_PIT_PROFILE",
        "CALENDAR_DATE_SPAN",
        "CALENDAR_PREFLIGHT_RESOURCE",
        "LOOKBACK_SESSIONS",
    )):
        return 1
    if upper.startswith(("IDENTITY_", "INSTRUMENT_", "CALENDAR_ID", "CALENDAR_BINDING", "CALENDAR_REGISTRY")):
        return 2
    if upper.startswith(("CALENDAR_SOURCE_PRIORITY", "SOURCE_PRIORITY")):
        return 3
    if upper.startswith(("CALENDAR_SESSION_INCOMPATIBLE", "CALENDAR_TIMEZONE", "NO_FORMAL_SESSIONS")):
        return 5
    if upper.startswith("WARMUP_"):
        return 6
    if upper.startswith((
        "CALENDAR_COVERAGE",
        "CALENDAR_SNAPSHOT_COVERAGE",
        "CALENDAR_DEFINITION",
        "CALENDAR_FACT",
        "CALENDAR_SESSION_WINDOW",
        "CALENDAR_SESSION_INVALID",
        "CALENDAR_SESSION_UNRESOLVED",
        "CALENDAR_JSON",
        "CALENDAR_PIT_METADATA",
    )):
        return 4
    if upper.startswith(("CAPABILITY", "UNSUPPORTED_CAPABILITY", "RULE_CAPABILITY")):
        return 7
    if upper.startswith(("PROVIDER_", "SNAPSHOT_", "CALENDAR_SNAPSHOT")):
        return 8
    return 9


def _issue_scope_rank(scope: str) -> int:
    """Return the canonical formal/warmup/global scope rank."""

    normalized = scope.strip().lower()
    # Warmup/formal scopes are exported with their explicit ``*_sessions``
    # names by the request and warmup layers.  Keep the short aliases for
    # callers constructing issues directly, while preserving the documented
    # formal -> warmup -> global ordering for both spellings.
    return {
        "formal": 0,
        "formal_sessions": 0,
        "warmup": 1,
        "warmup_sessions": 1,
        "global": 2,
    }.get(normalized, 2)


def _issue_field_rank(field: str | None) -> int:
    """Keep common issue fields deterministic without lexical semantics."""

    return {
        "data_cutoff": 1,
        "calendar_id": 2,
        "registry": 3,
        "definition": 4,
        "fact": 5,
        "sessions": 6,
        "timezone": 7,
    }.get((field or "").strip().lower(), 99)


def _sorted_issues(issues: Sequence[PreflightIssue]) -> tuple[PreflightIssue, ...]:
    """Validate element types and sort issues by machine fields."""

    validated = tuple(issues)
    for issue in validated:
        if not isinstance(issue, PreflightIssue):
            raise InvalidDataRequestError(
                "issues entries must be PreflightIssue instances"
            )
    return tuple(sorted(validated, key=lambda issue: issue.sort_key))


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataCoverageReport:
    """Per-capability coverage accounting for one requested slice.

    Counts are non-negative and must sum up to ``expected_count``.  An
    empty result is reported as ``unavailable`` evidence, never silently
    interpreted as "no events existed in the period".
    """

    requested_window: DateRange
    capability: DataCapability
    instrument_ids: tuple[UUID, ...]
    expected_count: int
    complete_count: int
    partial_count: int
    invalid_count: int
    unavailable_count: int
    quality_status: QualityStatus
    missing_ranges: tuple[DateRange, ...] = ()
    source_revisions: Mapping[str, str] | None = None
    issues: tuple[PreflightIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.requested_window, DateRange):
            raise InvalidDataRequestError(
                "requested_window must be a DateRange"
            )
        if not isinstance(self.capability, DataCapability):
            raise InvalidDataRequestError("capability must be a DataCapability")
        if not self.instrument_ids:
            raise InvalidDataRequestError("instrument_ids must not be empty")
        for instrument_id in self.instrument_ids:
            if not isinstance(instrument_id, UUID):
                raise InvalidDataRequestError(
                    "instrument_ids entries must be UUIDs"
                )
        object.__setattr__(
            self,
            "instrument_ids",
            tuple(sorted(set(self.instrument_ids), key=str)),
        )
        counts = {}
        total = 0
        for name in (
            "expected_count",
            "complete_count",
            "partial_count",
            "invalid_count",
            "unavailable_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidDataRequestError(f"{name} must be a non-negative int")
            counts[name] = value
            if name != "expected_count":
                total += value
        if total != counts["expected_count"]:
            raise InvalidDataRequestError(
                "coverage counts must sum up to expected_count"
            )
        if not isinstance(self.quality_status, QualityStatus):
            raise InvalidDataRequestError(
                "quality_status must be a QualityStatus"
            )
        ranges = tuple(self.missing_ranges)
        for missing_range in ranges:
            if not isinstance(missing_range, DateRange):
                raise InvalidDataRequestError(
                    "missing_ranges entries must be DateRange values"
                )
        ranges = tuple(sorted(set(ranges), key=lambda item: item.start_date))
        for earlier, later in zip(ranges, ranges[1:]):
            if later.start_date <= earlier.end_date:
                raise InvalidDataRequestError(
                    "missing_ranges must not contain overlapping ranges"
                )
        object.__setattr__(self, "missing_ranges", ranges)
        revisions = self.source_revisions or {}
        if not isinstance(revisions, Mapping):
            raise InvalidDataRequestError(
                "source_revisions must be a mapping"
            )
        normalized_revisions: dict[str, str] = {}
        for source, revision in revisions.items():
            if type(source) is not str or not source.strip():
                raise InvalidDataRequestError(
                    "source_revisions keys must be non-blank strings"
                )
            if type(revision) is not str or not revision.strip():
                raise InvalidDataRequestError(
                    "source_revisions values must be non-blank strings"
                )
            normalized_revisions[source] = revision
        object.__setattr__(
            self, "source_revisions", MappingProxyType(normalized_revisions)
        )
        object.__setattr__(self, "issues", _sorted_issues(self.issues))

    def machine_content(self) -> dict[str, object]:
        """Hash-relevant canonical content of this coverage report."""

        return {
            "requested_window": {
                "start_date": self.requested_window.start_date,
                "end_date": self.requested_window.end_date,
            },
            "capability": self.capability,
            "instrument_ids": [str(item) for item in self.instrument_ids],
            "counts": {
                "expected": self.expected_count,
                "complete": self.complete_count,
                "partial": self.partial_count,
                "invalid": self.invalid_count,
                "unavailable": self.unavailable_count,
            },
            "quality_status": self.quality_status,
            "missing_ranges": [
                {"start": item.start_date, "end": item.end_date}
                for item in self.missing_ranges
            ],
            "source_revisions": self.source_revisions,
            "issues": [issue.machine_fields() for issue in self.issues],
        }

    def __lt__(self, other: "DataCoverageReport") -> bool:
        return self.sort_key < other.sort_key

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """Stable ordering key independent of construction order."""

        return (
            self.capability.value,
            self.requested_window.start_date.isoformat(),
            self.requested_window.end_date.isoformat(),
        )


# ---------------------------------------------------------------------------
# Admission preflight report
# ---------------------------------------------------------------------------


def _validated_session_tuple(
    value: Sequence[SessionPoint], field_name: str
) -> tuple[SessionPoint, ...]:
    """Require SessionPoint tuples in ascending unique date order."""

    points = tuple(value)
    for point in points:
        if not isinstance(point, SessionPoint):
            raise InvalidDataRequestError(
                f"{field_name} entries must be SessionPoint instances"
            )
    dates = [point.session_date for point in points]
    if dates != sorted(dates):
        raise InvalidDataRequestError(
            f"{field_name} must be ordered by session_date"
        )
    if len(dates) != len(set(dates)):
        raise InvalidDataRequestError(
            f"{field_name} must not repeat session dates"
        )
    return points


@dataclass(frozen=True, slots=True)
class DataPreflightReport:
    """Authoritative admission-preflight outcome for one run intent.

    ``report_hash`` is recomputed defensively on construction from the
    canonical machine content, so callers cannot forge a mismatched hash.
    Display-only content (Chinese issue messages, generation time) and
    audit-only content (calendar definition sources) never affect it.
    """

    status: PreflightStatus
    generated_at: datetime
    provider_key: str
    capability_manifest_version: int
    requested_window: DateRange
    scope_mode: InstrumentScopeMode
    resolved_calendar_ids: tuple[str, ...]
    resolved_calendar_definitions: tuple[CalendarDefinition, ...]
    resolved_timezone: str | None
    calendar_axis_policy: ContractRef
    calendar_compatibility_status: CalendarAxisStatus
    calendar_session_signature: str
    resolved_sessions: tuple[SessionPoint, ...]
    warmup_sessions: tuple[SessionPoint, ...]
    max_lookback_sessions: int
    knowledge_as_of: datetime | None
    non_strict_pit_capabilities: tuple[DataCapability, ...]
    consistency_mode: ConsistencyMode
    consistency_token_capability: bool
    consistency_token_contract: ContractRef | None
    data_chunk_policy: ContractRef
    data_chunk_size_sessions: int
    required_capabilities: tuple[DataCapability, ...]
    rule_package: ContractRef
    rule_exception_set: ContractRef | None
    static_instrument_ids: tuple[UUID, ...]
    mandatory_instrument_ids: tuple[UUID, ...]
    strategy_price_bases: tuple[PriceBasis, ...]
    engine_price_basis: PriceBasis
    # Full request semantics bound into the report (and therefore the hash).
    # Defaults exist purely for dataclass field ordering; they are validated.
    data_contract_version: int = DATA_CONTRACT_VERSION
    frequency: str = ""
    warmup_sessions_count: int = 0
    market_scope: MarketScope | None = None
    universe_query_policy: UniverseQueryPolicy | None = None
    allowed_settlement_rule_class: str | None = None
    adjustment_series_policy: ContractRef | None = None
    # Adjustment-contract evidence is kept beside the generic policy
    # reference so admission/run records can prove exactly which verified
    # implementation and factor coverage they consumed.  All fields are
    # optional for legacy reports that never requested qfq/hfq data.
    adjustment_policy_status: str | None = None
    adjustment_adapter_version: str | None = None
    adjustment_formula_version: str | None = None
    adjustment_qfq_anchor: str | None = None
    adjustment_hfq_anchor: str | None = None
    adjustment_factor_cutoff_rule: str | None = None
    adjustment_verification_input_hash: str | None = None
    adjustment_verification_output_hash: str | None = None
    adjustment_verification_evidence_hash: str | None = None
    adjustment_factor_coverage: Mapping[str, object] | None = None
    quality_mode: QualityMode = QualityMode.STRICT
    coverage_reports: tuple[DataCoverageReport, ...] = ()
    source_revisions: Mapping[str, str] | None = None
    issues: tuple[PreflightIssue, ...] = ()
    # Task-16A report-contract fields.  They live on the authoritative report
    # itself so persistence, hashing, and API projections consume one frozen
    # source of truth instead of reconstructing profile/coverage metadata in
    # an orchestration wrapper.
    run_kind: str | None = None
    preflight_profile_key: str | None = None
    preflight_profile_version: int | None = None
    resolved_instruments: tuple[UUID, ...] = ()
    instrument_mapping_coverage: Mapping[str, object] | None = None
    instrument_rule_fact_summary: Mapping[str, object] | None = None
    lookback_session_bar_coverage: Mapping[str, object] | None = None
    bar_validity_summary: Mapping[str, object] | None = None
    missing_bars: tuple[Mapping[str, object], ...] = ()
    missing_fields: tuple[str, ...] = ()
    invalid_bars: int | None = None
    incomplete_rules: int | None = None
    non_pit_sources: tuple[DataCapability, ...] = ()
    fixture_sources: tuple[Mapping[str, object], ...] = ()
    # PIT candidate-universe audit fields.  They are optional so legacy
    # fixed-scope reports remain source-compatible; dynamic/hybrid providers
    # populate them from ``UniverseScopeResolution`` without introducing a
    # candidate-specific persistence model.
    non_zero_initial_position_instrument_ids: tuple[UUID, ...] = ()
    # Canonical qualification-policy reference for PIT universe checks.  The
    # longer ``universe_eligibility_policy_version`` name below is retained as
    # the report-facing spelling used by the architecture documents.
    qualification_policy_version: ContractRef | str | None = None
    qualification_policy: ContractRef | str | None = None
    universe_eligibility_policy_version: ContractRef | str | None = None
    universe_eligibility_summary: Mapping[str, object] | None = None
    universe_scope_snapshot_hash: str | None = None
    universe_candidate_count: int | None = None
    universe_filtered_reason_counts: Mapping[str, int] | None = None
    universe_target_ids: tuple[UUID, ...] = ()
    universe_final_rechecks: tuple[Mapping[str, object], ...] = ()
    universe_scope_resolution: object | None = None
    # Warmup-resolution audit fields (task 02-02); ``warmup_sessions_count``
    # above is the requested warmup count.  Defaults are deterministic so
    # reports without a warmup attempt keep one canonical form.
    warmup_resolution: "WarmupResolution | None" = None
    warmup_resolution_signature: str | None = None
    calendar_axis_differences: tuple[CalendarAxisDifference, ...] = ()
    warmup_axis_differences: tuple[CalendarAxisDifference, ...] = ()
    # Canonical task-11 PIT input.  Legacy reports may omit it during the
    # migration window; strict calendar providers reject that omission before
    # reading facts.
    query_boundary: object | None = None
    # Task-11 report evidence.  Version 1 remains the legacy payload; version
    # 2 is used only when calendar PIT evidence is present.
    hash_schema_version: int = 1
    pit_context: Mapping[str, object] | object | None = None
    calendar_revision_digest: str | None = None
    snapshot_fingerprint: str | None = None
    non_strict_pit: bool | None = None
    calendar_semantic_signature: str | None = None
    warmup_session_signature: str | None = None
    definition_usage_by_date: tuple[Mapping[str, object], ...] = ()
    calendar_summary: Mapping[str, object] | None = None
    session_summary: Mapping[str, object] | None = None
    # Recomputed in __post_init__; the placeholder keeps the field defaulted.
    report_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, PreflightStatus):
            raise InvalidDataRequestError("status must be a PreflightStatus")
        object.__setattr__(
            self, "generated_at", _aware_datetime(self.generated_at, "generated_at")
        )
        object.__setattr__(
            self, "provider_key", _non_blank_text(self.provider_key, "provider_key")
        )
        version = self.capability_manifest_version
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise InvalidDataRequestError(
                "capability_manifest_version must be a positive integer"
            )
        if not isinstance(self.requested_window, DateRange):
            raise InvalidDataRequestError("requested_window must be a DateRange")
        if not isinstance(self.scope_mode, InstrumentScopeMode):
            raise InvalidDataRequestError("scope_mode must be an InstrumentScopeMode")
        profile_values = (
            self.run_kind,
            self.preflight_profile_key,
            self.preflight_profile_version,
        )
        if any(value is not None for value in profile_values) and any(
            value is None for value in profile_values
        ):
            raise InvalidDataRequestError(
                "run_kind and preflight profile key/version must be supplied together"
            )
        if self.run_kind is not None:
            object.__setattr__(
                self, "run_kind", _non_blank_text(self.run_kind, "run_kind")
            )
            object.__setattr__(
                self,
                "preflight_profile_key",
                _non_blank_text(
                    self.preflight_profile_key, "preflight_profile_key"
                ),
            )
            profile_version = self.preflight_profile_version
            if (
                isinstance(profile_version, bool)
                or not isinstance(profile_version, int)
                or profile_version < 1
            ):
                raise InvalidDataRequestError(
                    "preflight_profile_version must be a positive integer"
                )
        resolved_instruments = tuple(self.resolved_instruments)
        if any(not isinstance(item, UUID) for item in resolved_instruments):
            raise InvalidDataRequestError(
                "resolved_instruments must contain UUID values"
            )
        object.__setattr__(
            self,
            "resolved_instruments",
            tuple(sorted(set(resolved_instruments), key=str)),
        )
        fixture_sources: list[Mapping[str, object]] = []
        for item in self.fixture_sources:
            if not isinstance(item, Mapping):
                raise InvalidDataRequestError(
                    "fixture_sources entries must be mappings"
                )
            frozen = _freeze_domain_json(dict(item), "fixture_sources")
            if not isinstance(frozen, MappingProxyType):
                raise InvalidDataRequestError(
                    "fixture_sources entries must be JSON mappings"
                )
            fixture_sources.append(frozen)
        object.__setattr__(
            self,
            "fixture_sources",
            tuple(sorted(fixture_sources, key=canonical_json)),
        )
        # Candidate-universe fields are normalized here rather than left to
        # individual providers.  This keeps report hashes independent of
        # input ordering and keeps every persisted value JSON-safe.
        for field_name in (
            "non_zero_initial_position_instrument_ids",
            "universe_target_ids",
        ):
            raw_ids = tuple(getattr(self, field_name))
            if any(not isinstance(item, UUID) for item in raw_ids):
                raise InvalidDataRequestError(
                    f"{field_name} must contain UUID values"
                )
            object.__setattr__(self, field_name, tuple(sorted(set(raw_ids), key=str)))
        policy = self.qualification_policy_version or self.qualification_policy
        if (
            self.qualification_policy_version is not None
            and self.qualification_policy is not None
            and self.qualification_policy_version != self.qualification_policy
        ):
            raise InvalidDataRequestError(
                "qualification_policy and qualification_policy_version disagree"
            )
        display_policy = self.universe_eligibility_policy_version
        if policy is not None and display_policy is not None and policy != display_policy:
            raise InvalidDataRequestError(
                "qualification policy fields disagree"
            )
        if policy is None:
            policy = display_policy
        object.__setattr__(self, "qualification_policy_version", policy)
        object.__setattr__(self, "qualification_policy", policy)
        object.__setattr__(self, "universe_eligibility_policy_version", policy)
        if policy is not None and not isinstance(policy, (ContractRef, str)):
            raise InvalidDataRequestError(
                "universe_eligibility_policy_version must be a ContractRef or text"
            )
        if isinstance(policy, str):
            if not policy.strip():
                raise InvalidDataRequestError(
                    "universe_eligibility_policy_version must be non-blank"
                )
            policy = policy.strip()
            object.__setattr__(self, "qualification_policy_version", policy)
            object.__setattr__(self, "qualification_policy", policy)
            object.__setattr__(self, "universe_eligibility_policy_version", policy)
        summary = self.universe_eligibility_summary
        if summary is not None:
            if not isinstance(summary, Mapping):
                raise InvalidDataRequestError(
                    "universe_eligibility_summary must be a mapping"
                )
            frozen_summary = _freeze_domain_json(
                dict(summary), "universe_eligibility_summary"
            )
            if not isinstance(frozen_summary, MappingProxyType):
                raise InvalidDataRequestError(
                    "universe_eligibility_summary must be a JSON mapping"
                )
            object.__setattr__(self, "universe_eligibility_summary", frozen_summary)
        scope_hash = self.universe_scope_snapshot_hash
        if scope_hash is not None:
            scope_hash = _require_hash_digest(scope_hash, "universe_scope_snapshot_hash")
            object.__setattr__(self, "universe_scope_snapshot_hash", scope_hash)
        candidate_count = self.universe_candidate_count
        if candidate_count is not None and (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < 0
        ):
            raise InvalidDataRequestError(
                "universe_candidate_count must be a non-negative integer"
            )
        counts = self.universe_filtered_reason_counts
        if counts is not None:
            if not isinstance(counts, Mapping):
                raise InvalidDataRequestError(
                    "universe_filtered_reason_counts must be a mapping"
                )
            normalized_counts: dict[str, int] = {}
            for code, value in counts.items():
                if (
                    type(code) is not str
                    or not code.strip()
                    or isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                ):
                    raise InvalidDataRequestError(
                        "universe_filtered_reason_counts must map codes to non-negative integers"
                    )
                normalized_counts[code.strip()] = value
            object.__setattr__(
                self,
                "universe_filtered_reason_counts",
                MappingProxyType(dict(sorted(normalized_counts.items()))),
            )
        rechecks = tuple(self.universe_final_rechecks)
        normalized_rechecks: list[Mapping[str, object]] = []
        for item in rechecks:
            if not isinstance(item, Mapping):
                raise InvalidDataRequestError(
                    "universe_final_rechecks entries must be mappings"
                )
            frozen = _freeze_domain_json(dict(item), "universe_final_rechecks")
            if not isinstance(frozen, MappingProxyType):
                raise InvalidDataRequestError(
                    "universe_final_rechecks entries must be JSON mappings"
                )
            normalized_rechecks.append(frozen)
        object.__setattr__(self, "universe_final_rechecks", tuple(normalized_rechecks))
        resolution = self.universe_scope_resolution
        if resolution is not None:
            if isinstance(resolution, Mapping):
                normalized_resolution = _freeze_domain_json(
                    dict(resolution), "universe_scope_resolution"
                )
                if not isinstance(normalized_resolution, MappingProxyType):
                    raise InvalidDataRequestError(
                        "universe_scope_resolution must be a JSON mapping"
                    )
                object.__setattr__(self, "universe_scope_resolution", normalized_resolution)
                resolution = normalized_resolution
            elif not callable(getattr(resolution, "canonical_content", None)):
                raise InvalidDataRequestError(
                    "universe_scope_resolution must expose canonical_content()"
                )
            if scope_hash is None:
                resolved_hash = (
                    resolution.get("snapshot_hash")
                    if isinstance(resolution, Mapping)
                    else getattr(resolution, "snapshot_hash", None)
                )
                if isinstance(resolved_hash, str) and resolved_hash:
                    object.__setattr__(
                        self, "universe_scope_snapshot_hash", _require_hash_digest(
                            resolved_hash, "universe_scope_snapshot_hash"
                        )
                    )
            else:
                resolved_hash = (
                    resolution.get("snapshot_hash")
                    if isinstance(resolution, Mapping)
                    else getattr(resolution, "snapshot_hash", None)
                )
                if isinstance(resolved_hash, str) and resolved_hash and resolved_hash != scope_hash:
                    raise UniversePreflightHashMismatchError(
                        "universe_scope_resolution hash does not match report hash"
                    )
        if self.scope_mode in (InstrumentScopeMode.DYNAMIC, InstrumentScopeMode.HYBRID):
            # A ready dynamic/hybrid report is consumable only when it carries
            # the concrete task-15 scope resolution.  A naked digest or
            # provider signature is not enough to prove the frozen calendar
            # axis and capability gate.
            try:
                from app.backtesting.data.universe import (
                    UniverseScopeResolution,
                    UniverseScopeStatus,
                )
            except ImportError:  # pragma: no cover - import-cycle guard
                UniverseScopeResolution = ()  # type: ignore[assignment]
                UniverseScopeStatus = ()  # type: ignore[assignment]
            if self.status is PreflightStatus.READY:
                if not isinstance(resolution, UniverseScopeResolution):
                    raise InvalidDataRequestError(
                        "a ready dynamic report requires universe scope resolution"
                    )
                if resolution.status is not UniverseScopeStatus.READY:
                    raise InvalidDataRequestError(
                        "a ready dynamic report cannot carry a blocked scope resolution"
                    )
                from app.backtesting.calendar_axis import (
                    CalendarAxisResolution,
                    CalendarSnapshot,
                )
                axis = resolution.calendar_axis_resolution
                if isinstance(axis, CalendarSnapshot):
                    axis = axis.resolution
                if not isinstance(axis, CalendarAxisResolution) or (
                    axis.policy_key != "strict_compatible"
                    or str(axis.policy_version) != "1"
                    or axis.status is not CalendarAxisStatus.COMPATIBLE
                    or tuple(axis.calendar_ids) != tuple(
                        resolution.resolved_calendar_ids
                    )
                    or not axis.session_signature
                    or tuple(axis.differences)
                    or resolution.calendar_session_signature
                    != axis.session_signature
                ):
                    raise InvalidDataRequestError(
                        "a ready dynamic report requires a compatible strict calendar-axis result"
                    )
                if any(
                    getattr(getattr(issue, "severity", "error"), "value", getattr(issue, "severity", "error"))
                    == "error"
                    for issue in resolution.issues
                ):
                    raise InvalidDataRequestError(
                        "a ready dynamic report cannot carry blocking scope issues"
                    )
                # Keep the report constructor fail-closed even when a caller
                # bypasses ``resolve_dynamic_universe_scope`` and constructs
                # a resolution directly.  Presence of a calendar and hash
                # alone does not prove the required qualification contracts.
                capability_aliases = {
                    "universe": {"universe", "pit_universe", "candidate_universe"},
                    "identity": {"identity", "pit_identity", "instrument_identity"},
                    "mapping": {"mapping", "mappings", "pit_mapping", "display_mapping"},
                    "rules": {"rule", "rules", "rule_package", "rule_qualification", "qualification"},
                    "market_data": {"bar", "bars", "market_data", "raw_bars", "coverage", "coverage_qualification", "history"},
                }
                declared = {
                    str(key).strip().lower().replace("-", "_").replace(".", "_")
                    for key in resolution.capability_summary
                }
                missing_capabilities = sorted(
                    bucket
                    for bucket, aliases in capability_aliases.items()
                    if not declared.intersection(aliases)
                )
                if missing_capabilities:
                    raise InvalidDataRequestError(
                        "a ready dynamic report requires complete capability evidence",
                        details={"missing_capabilities": missing_capabilities},
                    )
                unavailable_capabilities: list[str] = []
                for bucket, aliases in capability_aliases.items():
                    matching_keys = declared.intersection(aliases)
                    for key in matching_keys:
                        value = resolution.capability_summary.get(key)
                        if isinstance(value, Mapping):
                            value = value.get(
                                "status",
                                value.get(
                                    "availability",
                                    value.get("supported", value.get("complete")),
                                ),
                            )
                        value = getattr(value, "value", value)
                        if isinstance(value, str):
                            value = value.strip().lower()
                        if value is False or value in {
                            None,
                            "missing",
                            "unavailable",
                            "unsupported",
                            "blocked",
                            "unknown",
                            "incomplete",
                            "invalid",
                        }:
                            unavailable_capabilities.append(bucket)
                if unavailable_capabilities:
                    raise InvalidDataRequestError(
                        "a ready dynamic report cannot carry unavailable capability evidence",
                        details={
                            "unavailable_capabilities": sorted(
                                set(unavailable_capabilities)
                            )
                        },
                    )
                profile = resolution.source_evidence.get(
                    "preflight_profile", "formal@1"
                )
                if str(profile) != "internal_link_acceptance@1":
                    fixture_capabilities: list[str] = []
                    for capability_key, capability_value in resolution.capability_summary.items():
                        source = None
                        status_value = capability_value
                        if isinstance(capability_value, Mapping):
                            source = capability_value.get("source")
                            status_value = capability_value.get(
                                "status",
                                capability_value.get(
                                    "availability",
                                    capability_value.get(
                                        "supported", capability_value.get("complete")
                                    ),
                                ),
                            )
                        source = getattr(source, "value", source)
                        status_value = getattr(status_value, "value", status_value)
                        if str(source).lower() in {
                            "fixture",
                            "internal_fixture",
                            "transitional",
                        } or str(status_value).lower() in {
                            "fixture",
                            "internal_fixture",
                            "transitional",
                        }:
                            fixture_capabilities.append(str(capability_key))
                    if fixture_capabilities:
                        raise InvalidDataRequestError(
                            "a ready formal dynamic report cannot use fixture-backed capability evidence",
                            details={"fixture_capabilities": sorted(fixture_capabilities)},
                        )
                    formal_gate_aliases = {
                        "formal_preflight": {
                            "formal_preflight",
                            "preflight_16b",
                            "formal_admission",
                            "formal_qualification",
                        },
                        "formal_runtime": {
                            "formal_runtime",
                            "runtime_boundary",
                            "runner",
                            "strategy_runtime",
                        },
                        "formal_corporate_actions": {
                            "formal_corporate_actions",
                            "corporate_action_qualification",
                            "actions_18",
                            "task18",
                        },
                        "formal_trading_status": {
                            "formal_trading_status",
                            "trading_status_qualification",
                            "status_19",
                            "task19",
                        },
                    }
                    missing_formal_gates = sorted(
                        gate
                        for gate, aliases in formal_gate_aliases.items()
                        if not declared.intersection(aliases)
                    )
                    if missing_formal_gates:
                        raise InvalidDataRequestError(
                            "a ready formal dynamic report requires all formal dependency gates",
                            details={"missing_formal_gates": missing_formal_gates},
                        )
                if not self.universe_scope_snapshot_hash:
                    raise InvalidDataRequestError(
                        "a ready dynamic report requires universe scope snapshot hash"
                    )
                if (
                    resolution.current_snapshot_hash is not None
                    and resolution.current_snapshot_hash
                    != resolution.snapshot_hash
                ):
                    raise UniversePreflightHashMismatchError(
                        "a ready dynamic report cannot carry a changed session scope hash",
                        details={
                            "expected": resolution.snapshot_hash,
                            "actual": resolution.current_snapshot_hash,
                        },
                    )
        object.__setattr__(
            self,
            "resolved_calendar_ids",
            # An empty tuple is legitimate for reports that never resolved a
            # calendar axis (blocked runs); DataRequest still requires a
            # non-empty resolution at admission time.
            _sorted_unique_text(
                self.resolved_calendar_ids, "resolved_calendar_ids", allow_empty=True
            ),
        )
        if not self.resolved_calendar_ids:
            # An unresolved calendar axis is only expressible in a fully
            # blocked, incompatible report: no timezone, no signature, and
            # (via the incompatible branch below) no formal sessions.
            if self.status is not PreflightStatus.BLOCKED:
                raise InvalidDataRequestError(
                    "only blocked reports may leave the calendar axis "
                    "unresolved"
                )
            if (
                self.calendar_compatibility_status
                is not CalendarAxisStatus.INCOMPATIBLE
            ):
                raise InvalidDataRequestError(
                    "an unresolved calendar axis must be reported as "
                    "incompatible"
                )
            if self.resolved_timezone is not None:
                raise InvalidDataRequestError(
                    "a report with an unresolved calendar axis must not "
                    "declare a resolved timezone"
                )
        definitions = tuple(self.resolved_calendar_definitions)
        for definition in definitions:
            if not isinstance(definition, CalendarDefinition):
                raise InvalidDataRequestError(
                    "resolved_calendar_definitions entries must be "
                    "CalendarDefinition instances"
                )
        object.__setattr__(
            self,
            "resolved_calendar_definitions",
            tuple(
                sorted(
                    definitions,
                    key=lambda d: (d.calendar_id, d.definition_version, d.fact_version, str(d.fact_id)),
                )
            ),
        )
        if self.resolved_timezone is not None:
            timezone = _non_blank_text(self.resolved_timezone, "resolved_timezone")
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise InvalidDataRequestError(
                    "resolved_timezone must be a resolvable IANA time-zone name"
                ) from exc
            object.__setattr__(self, "resolved_timezone", timezone)
        if self.calendar_axis_policy != CALENDAR_AXIS_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the strict_compatible@1 "
                "calendar axis policy"
            )
        if not isinstance(
            self.calendar_compatibility_status, CalendarAxisStatus
        ):
            raise InvalidDataRequestError(
                "calendar_compatibility_status must be a CalendarAxisStatus"
            )
        compatible = (
            self.calendar_compatibility_status is CalendarAxisStatus.COMPATIBLE
        )
        if compatible:
            # A fully compatible calendar is the precondition for publishing
            # a session signature and formal sessions at all.
            object.__setattr__(
                self,
                "calendar_session_signature",
                _non_blank_text(
                    self.calendar_session_signature,
                    "calendar_session_signature",
                ),
            )
        else:
            # Calendar incompatibility blocks the run outright.
            if self.status is not PreflightStatus.BLOCKED:
                raise InvalidDataRequestError(
                    "an incompatible calendar axis forces status=blocked"
                )
            if self.calendar_session_signature:
                raise InvalidDataRequestError(
                    "an incompatible calendar axis must not carry a "
                    "session signature"
                )
        sessions = _validated_session_tuple(self.resolved_sessions, "resolved_sessions")
        warmup = _validated_session_tuple(self.warmup_sessions, "warmup_sessions")
        if self.status is PreflightStatus.BLOCKED and sessions:
            raise InvalidDataRequestError(
                "a blocked report must not carry resolved_sessions"
            )
        if not compatible and warmup:
            raise InvalidDataRequestError(
                "warmup sessions require a compatible calendar axis"
            )
        official_dates = {point.session_date for point in sessions}
        for point in warmup:
            if point.session_date in official_dates:
                raise InvalidDataRequestError(
                    "warmup sessions must be kept separate from official "
                    "sessions"
                )
        object.__setattr__(self, "resolved_sessions", sessions)
        object.__setattr__(self, "warmup_sessions", warmup)
        # Warmup audit fields: the mounted resolution object, its mirrored
        # signature, the difference evidence, and the session tuples must
        # stay mutually consistent with the overall status.
        if self.status is PreflightStatus.BLOCKED and warmup:
            raise InvalidDataRequestError(
                "a blocked report must not carry warmup_sessions"
            )
        if self.warmup_sessions_count == 0 and warmup:
            raise InvalidDataRequestError(
                "warmup_sessions must be empty when no warmup was requested"
            )
        # Copy, type-check, sort, and freeze the warmup difference evidence
        # before anything compares it against the mounted resolution.
        warmup_differences = tuple(self.warmup_axis_differences)
        for difference in warmup_differences:
            if not isinstance(difference, CalendarAxisDifference):
                raise InvalidDataRequestError(
                    "warmup_axis_differences entries must be "
                    "CalendarAxisDifference instances"
                )
        warmup_differences = tuple(
            sorted(warmup_differences, key=lambda item: item.sort_key)
        )
        object.__setattr__(
            self, "warmup_axis_differences", warmup_differences
        )
        if self.warmup_resolution is not None:
            from app.backtesting.data.warmup import WarmupResolution, WarmupStatus

            if not isinstance(self.warmup_resolution, WarmupResolution):
                raise InvalidDataRequestError(
                    "warmup_resolution must be a WarmupResolution instance"
                )
            resolution = self.warmup_resolution
            if self.warmup_resolution_signature != resolution.resolution_signature:
                raise InvalidDataRequestError(
                    "warmup_resolution_signature must equal the mounted "
                    "warmup resolution signature"
                )
            if resolution.requested_sessions != self.warmup_sessions_count:
                raise InvalidDataRequestError(
                    "warmup_resolution requested count must equal "
                    "warmup_sessions_count"
                )
            if resolution.status is WarmupStatus.READY and (
                len(warmup) != len(resolution.resolved_sessions)
                or warmup != resolution.resolved_sessions
            ):
                raise InvalidDataRequestError(
                    "a ready warmup resolution requires the report to carry "
                    "exactly the resolved warmup sessions"
                )
            if (
                resolution.status is WarmupStatus.READY
                and sessions
                and resolution.first_formal_session != sessions[0].session_date
            ):
                raise InvalidDataRequestError(
                    "the mounted warmup anchor must equal the first formal "
                    "session of this report"
                )
            if warmup_differences != tuple(resolution.axis_differences):
                raise InvalidDataRequestError(
                    "warmup_axis_differences must equal the axis "
                    "differences of the mounted warmup resolution"
                )
            if (
                self.status is PreflightStatus.BLOCKED
                and resolution.status is WarmupStatus.READY
            ):
                raise InvalidDataRequestError(
                    "a blocked report cannot mount a ready warmup resolution"
                )
            if (
                self.status is PreflightStatus.READY
                and resolution.status is not WarmupStatus.READY
            ):
                raise InvalidDataRequestError(
                    "a ready report cannot mount a blocked warmup resolution"
                )
        elif self.warmup_resolution_signature is not None:
            raise InvalidDataRequestError(
                "warmup_resolution_signature requires a mounted "
                "warmup_resolution"
            )
        elif warmup_differences:
            # Difference evidence only exists as part of a mounted warmup
            # resolution; it can never be fabricated standalone.
            raise InvalidDataRequestError(
                "warmup_axis_differences require a mounted warmup_resolution"
            )
        if (
            self.status is PreflightStatus.READY
            and self.warmup_sessions_count > 0
            and (self.warmup_resolution is None or len(warmup) == 0)
        ):
            raise InvalidDataRequestError(
                "a ready report with requested warmup sessions requires a "
                "ready warmup resolution and the resolved warmup sessions"
            )
        differences = tuple(self.calendar_axis_differences)
        for difference in differences:
            if not isinstance(difference, CalendarAxisDifference):
                raise InvalidDataRequestError(
                    "calendar_axis_differences entries must be "
                    "CalendarAxisDifference instances"
                )
        if differences and compatible:
            raise InvalidDataRequestError(
                "a compatible calendar axis cannot carry differences"
            )
        object.__setattr__(
            self,
            "calendar_axis_differences",
            tuple(sorted(differences, key=lambda item: item.sort_key)),
        )
        maximum = self.max_lookback_sessions
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise InvalidDataRequestError(
                "max_lookback_sessions must be an integer"
            )
        if maximum != MAX_LOOKBACK_SESSIONS:
            raise InvalidDataRequestError(
                f"data-contract version 1 fixes max_lookback_sessions to "
                f"{MAX_LOOKBACK_SESSIONS}"
            )
        if self.knowledge_as_of is not None:
            object.__setattr__(
                self,
                "knowledge_as_of",
                _aware_datetime(self.knowledge_as_of, "knowledge_as_of"),
            )
        object.__setattr__(
            self,
            "non_strict_pit_capabilities",
            _sorted_unique_enum(
                self.non_strict_pit_capabilities,
                DataCapability,
                "non_strict_pit_capabilities",
                allow_empty=True,
            ),
        )
        if not isinstance(self.consistency_mode, ConsistencyMode):
            raise InvalidDataRequestError(
                "consistency_mode must be a ConsistencyMode"
            )
        if not isinstance(self.consistency_token_capability, bool):
            raise InvalidDataRequestError(
                "consistency_token_capability must be a boolean"
            )
        if self.consistency_token_contract is not None and not isinstance(
            self.consistency_token_contract, ContractRef
        ):
            raise InvalidDataRequestError(
                "consistency_token_contract must be a ContractRef when provided"
            )
        if (
            self.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
            and self.consistency_token_contract is None
        ):
            raise InvalidDataRequestError(
                "chunked_logical_token consistency requires a "
                "consistency_token_contract"
            )
        # The declared token capability must not contradict the mode: the
        # logical-token mode always advertises token support, while the
        # transitional repeatable-read mode has no logical token at all.
        if self.consistency_mode is ConsistencyMode.CHUNKED_LOGICAL_TOKEN:
            if not self.consistency_token_capability:
                raise InvalidDataRequestError(
                    "chunked_logical_token consistency requires "
                    "consistency_token_capability=True"
                )
        elif self.consistency_token_capability:
            raise InvalidDataRequestError(
                "transitional_repeatable_read has no logical token and must "
                "not advertise consistency_token_capability"
            )
        # Validate the request-semantics fields bound into this report.
        contract_version = _strict_version(self.data_contract_version)
        object.__setattr__(self, "data_contract_version", contract_version)
        object.__setattr__(
            self, "frequency", _non_blank_text(self.frequency, "frequency")
        )
        warmup_count = self.warmup_sessions_count
        if isinstance(warmup_count, bool) or not isinstance(warmup_count, int) or (
            warmup_count < 0
        ):
            raise InvalidDataRequestError(
                "warmup_sessions_count must be a non-negative integer"
            )
        if not isinstance(self.market_scope, MarketScope):
            raise InvalidDataRequestError("market_scope must be a MarketScope")
        if not isinstance(self.universe_query_policy, UniverseQueryPolicy):
            raise InvalidDataRequestError(
                "universe_query_policy must be a UniverseQueryPolicy"
            )
        if self.allowed_settlement_rule_class is not None:
            object.__setattr__(
                self,
                "allowed_settlement_rule_class",
                _non_blank_text(
                    self.allowed_settlement_rule_class,
                    "allowed_settlement_rule_class",
                ),
            )
        if self.adjustment_series_policy is not None and not isinstance(
            self.adjustment_series_policy, ContractRef
        ):
            # Accept the immutable ETF policy descriptor as a convenience at
            # this boundary and flatten its audit fields into this report.
            # The stored request reference remains the generic ContractRef;
            # no provider-specific policy object leaks into the report DTO.
            candidate = self.adjustment_series_policy
            as_dict = getattr(candidate, "as_dict", None)
            reference = getattr(candidate, "reference", None)
            payload = as_dict() if callable(as_dict) else None
            if not isinstance(reference, ContractRef) or not isinstance(payload, Mapping):
                raise InvalidDataRequestError(
                    "adjustment_series_policy must be a ContractRef when provided"
                )
            object.__setattr__(self, "adjustment_series_policy", reference)
            field_aliases = {
                "status": "adjustment_policy_status",
                "adapter_version": "adjustment_adapter_version",
                "formula_version": "adjustment_formula_version",
                "qfq_anchor": "adjustment_qfq_anchor",
                "hfq_anchor": "adjustment_hfq_anchor",
                "cutoff_rule": "adjustment_factor_cutoff_rule",
                "verification_input_hash": "adjustment_verification_input_hash",
                "verification_output_hash": "adjustment_verification_output_hash",
                "verification_evidence_hash": "adjustment_verification_evidence_hash",
                "factor_coverage": "adjustment_factor_coverage",
            }
            for source_name, target_name in field_aliases.items():
                if getattr(self, target_name) is None and payload.get(source_name) is not None:
                    object.__setattr__(self, target_name, payload[source_name])
        # Adjustment evidence is optional for legacy/raw-only reports, but
        # every supplied value must be deterministic and JSON-safe.  The
        # policy gate itself validates the stronger active-policy invariant;
        # this report merely preserves its immutable audit fields.
        for name in (
            "adjustment_policy_status",
            "adjustment_adapter_version",
            "adjustment_formula_version",
            "adjustment_qfq_anchor",
            "adjustment_hfq_anchor",
            "adjustment_factor_cutoff_rule",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _non_blank_text(value, name))
        for name in (
            "adjustment_verification_input_hash",
            "adjustment_verification_output_hash",
            "adjustment_verification_evidence_hash",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_hash_digest(value, name))
        if self.adjustment_policy_status is not None and self.adjustment_policy_status not in {
            "inactive",
            "active",
        }:
            raise InvalidDataRequestError(
                "adjustment_policy_status must be inactive or active"
            )
        if self.adjustment_factor_coverage is not None:
            frozen_coverage = freeze_json(
                dict(self.adjustment_factor_coverage),
                "adjustment_factor_coverage",
            )
            if not isinstance(frozen_coverage, MappingProxyType):
                raise InvalidDataRequestError(
                    "adjustment_factor_coverage must be a JSON mapping"
                )
            object.__setattr__(self, "adjustment_factor_coverage", frozen_coverage)
        if not isinstance(self.quality_mode, QualityMode):
            raise InvalidDataRequestError("quality_mode must be a QualityMode")
        if self.data_chunk_policy != CHUNK_POLICY:
            raise InvalidDataRequestError(
                "data-contract version 1 requires the fixed_trading_sessions@1 "
                "chunk policy"
            )
        chunk_size = self.data_chunk_size_sessions
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise InvalidDataRequestError(
                "data_chunk_size_sessions must be an integer"
            )
        if chunk_size != 20:
            raise InvalidDataRequestError(
                "data-contract version 1 fixes data_chunk_size_sessions to 20"
            )
        object.__setattr__(
            self,
            "required_capabilities",
            _sorted_unique_enum(
                self.required_capabilities, DataCapability, "required_capabilities"
            ),
        )
        if not isinstance(self.rule_package, ContractRef):
            raise InvalidDataRequestError("rule_package must be a ContractRef")
        if self.rule_exception_set is not None and not isinstance(
            self.rule_exception_set, ContractRef
        ):
            raise InvalidDataRequestError(
                "rule_exception_set must be a ContractRef when provided"
            )
        for field_name in ("static_instrument_ids", "mandatory_instrument_ids"):
            ids = getattr(self, field_name)
            for instrument_id in ids:
                if not isinstance(instrument_id, UUID):
                    raise InvalidDataRequestError(
                        f"{field_name} entries must be UUIDs"
                    )
            object.__setattr__(
                self, field_name, tuple(sorted(set(ids), key=str))
            )
        object.__setattr__(
            self,
            "strategy_price_bases",
            _sorted_unique_enum(
                self.strategy_price_bases, PriceBasis, "strategy_price_bases"
            ),
        )
        if not isinstance(self.engine_price_basis, PriceBasis):
            raise InvalidDataRequestError(
                "engine_price_basis must be a PriceBasis"
            )
        if self.engine_price_basis is not PriceBasis.RAW:
            raise InvalidDataRequestError(
                "data-contract version 1 requires engine_price_basis=raw"
            )
        coverages = tuple(self.coverage_reports)
        for report in coverages:
            if not isinstance(report, DataCoverageReport):
                raise InvalidDataRequestError(
                    "coverage_reports entries must be DataCoverageReport"
                )
        object.__setattr__(
            self, "coverage_reports", tuple(sorted(coverages))
        )
        coverage_by_capability = {
            item.capability: item.machine_content() for item in coverages
        }
        coverage_fields = {
            "instrument_mapping_coverage": DataCapability.MAPPINGS,
            "instrument_rule_fact_summary": DataCapability.RULES,
            "lookback_session_bar_coverage": DataCapability.BARS,
            "bar_validity_summary": DataCapability.BARS,
        }
        for field_name, capability in coverage_fields.items():
            value = getattr(self, field_name)
            if value is None:
                value = coverage_by_capability.get(capability)
            if value is not None:
                if not isinstance(value, Mapping):
                    raise InvalidDataRequestError(
                        f"{field_name} must be a mapping"
                    )
                frozen = _freeze_domain_json(dict(value), field_name)
                if not isinstance(frozen, MappingProxyType):
                    raise InvalidDataRequestError(
                        f"{field_name} must be a JSON mapping"
                    )
                object.__setattr__(self, field_name, frozen)
        bars = self.lookback_session_bar_coverage
        rules = self.instrument_rule_fact_summary
        missing_bars = tuple(self.missing_bars)
        if not missing_bars and isinstance(bars, Mapping):
            raw_missing = bars.get("missing_ranges", ())
            if isinstance(raw_missing, Sequence) and not isinstance(
                raw_missing, (str, bytes)
            ):
                missing_bars = tuple(raw_missing)
        normalized_missing_bars: list[Mapping[str, object]] = []
        for item in missing_bars:
            if not isinstance(item, Mapping):
                raise InvalidDataRequestError(
                    "missing_bars entries must be mappings"
                )
            frozen = _freeze_domain_json(dict(item), "missing_bars")
            if not isinstance(frozen, MappingProxyType):
                raise InvalidDataRequestError(
                    "missing_bars entries must be JSON mappings"
                )
            normalized_missing_bars.append(frozen)
        object.__setattr__(
            self,
            "missing_bars",
            tuple(sorted(normalized_missing_bars, key=canonical_json)),
        )
        object.__setattr__(
            self,
            "missing_fields",
            _sorted_unique_text(
                self.missing_fields, "missing_fields", allow_empty=True
            ),
        )
        for field_name, summary, count_name in (
            ("invalid_bars", bars, "invalid"),
            ("incomplete_rules", rules, "unavailable"),
        ):
            value = getattr(self, field_name)
            counts = summary.get("counts") if isinstance(summary, Mapping) else None
            if value is None and isinstance(counts, Mapping):
                value = counts.get(count_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise InvalidDataRequestError(
                    f"{field_name} must be a non-negative integer"
                )
            object.__setattr__(self, field_name, value)
        non_pit_sources = self.non_pit_sources or self.non_strict_pit_capabilities
        object.__setattr__(
            self,
            "non_pit_sources",
            _sorted_unique_enum(
                non_pit_sources,
                DataCapability,
                "non_pit_sources",
                allow_empty=True,
            ),
        )
        revisions = self.source_revisions or {}
        if not isinstance(revisions, Mapping):
            raise InvalidDataRequestError("source_revisions must be a mapping")
        normalized_revisions: dict[str, str] = {}
        for source, revision in revisions.items():
            if type(source) is not str or not source.strip():
                raise InvalidDataRequestError(
                    "source_revisions keys must be non-blank strings"
                )
            if type(revision) is not str or not revision.strip():
                raise InvalidDataRequestError(
                    "source_revisions values must be non-blank strings"
                )
            normalized_revisions[source] = revision
        object.__setattr__(
            self, "source_revisions", MappingProxyType(normalized_revisions)
        )
        issues = _sorted_issues(self.issues)
        if not self.missing_fields:
            object.__setattr__(
                self,
                "missing_fields",
                tuple(sorted({issue.field for issue in issues if issue.field})),
            )
        errors = [
            issue for issue in issues
            if issue.severity is IssueSeverity.ERROR
        ]
        warnings = [
            issue for issue in issues
            if issue.severity is IssueSeverity.WARNING
        ]
        if self.status is PreflightStatus.READY and errors:
            raise InvalidDataRequestError("ready reports must not carry error issues")
        if self.status is PreflightStatus.DEGRADED:
            if errors:
                raise InvalidDataRequestError(
                    "degraded reports must not carry error issues"
                )
            if not warnings:
                raise InvalidDataRequestError(
                    "degraded reports must carry at least one warning"
                )
        if self.status is PreflightStatus.BLOCKED and not errors:
            raise InvalidDataRequestError(
                "blocked reports must carry at least one error issue"
            )
        if len(issues) > 4096:
            raise CalendarPreflightResourceLimitExceededError(
                "preflight issue groups exceed the 4096 response limit",
                details={"issue_groups": len(issues), "maximum": 4096},
            )
        object.__setattr__(self, "issues", issues)
        if self.query_boundary is not None:
            from app.backtesting.data.requests import QueryBoundary
            if not isinstance(self.query_boundary, QueryBoundary):
                raise InvalidDataRequestError("query_boundary must be a QueryBoundary")
            if self.knowledge_as_of is not None and self.knowledge_as_of != self.query_boundary.knowledge_as_of:
                raise InvalidDataRequestError("report knowledge_as_of must match query_boundary")
        hash_version = self.hash_schema_version
        if isinstance(hash_version, bool) or not isinstance(hash_version, int) or hash_version not in (1, 2):
            raise InvalidDataRequestError("hash_schema_version must be 1 or 2")
        object.__setattr__(self, "hash_schema_version", hash_version)
        if self.pit_context is not None and hasattr(self.pit_context, "as_dict"):
            object.__setattr__(self, "pit_context", dict(self.pit_context.as_dict))
        if hash_version == 2:
            # @2 is never a cosmetic opt-in: it is the canonical calendar
            # evidence payload and therefore requires the complete immutable
            # PIT/snapshot envelope.  Pre-read failures use @1 with no fake
            # snapshot hash.
            if self.query_boundary is None or self.pit_context is None:
                raise InvalidDataRequestError(
                    "hash_schema_version=2 requires query_boundary and pit_context"
                )
            required_context = {
                "data_cutoff", "cutoff_local_date", "include_cutoff_day",
                "knowledge_as_of", "pit_profile", "profile_version",
            }
            if not required_context <= set(self.pit_context):
                raise InvalidDataRequestError(
                    "hash_schema_version=2 pit_context is incomplete"
                )
            expected_context = self.query_boundary
            context_data_cutoff = self.pit_context.get("data_cutoff")
            context_knowledge = self.pit_context.get("knowledge_as_of")
            context_include = self.pit_context.get("include_cutoff_day")
            expected_cutoff_text = expected_context.data_cutoff.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")
            expected_knowledge_text = (
                expected_context.knowledge_as_of.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")
                if expected_context.knowledge_as_of is not None else None
            )
            if context_data_cutoff != expected_cutoff_text or context_knowledge != expected_knowledge_text or context_include != expected_context.include_cutoff_day:
                raise InvalidDataRequestError("pit_context must be copied from query_boundary")
            expected_profile = "strict_historical_cognition" if expected_context.knowledge_as_of is not None else "strict_calendar_cutoff"
            if self.pit_context.get("pit_profile") != expected_profile or self.pit_context.get("profile_version") != "calendar_pit_profile@1:H":
                raise InvalidDataRequestError("pit_context profile is not the fixed H profile")
            for digest_name in (
                "calendar_revision_digest",
                "snapshot_fingerprint",
                "calendar_semantic_signature",
                "warmup_session_signature",
            ):
                digest = getattr(self, digest_name)
                if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                    raise InvalidDataRequestError(
                        f"hash_schema_version=2 requires {digest_name}"
                    )
        capabilities = tuple(self.non_strict_pit_capabilities)
        if self.non_strict_pit is None:
            object.__setattr__(self, "non_strict_pit", bool(capabilities))
        elif not isinstance(self.non_strict_pit, bool):
            raise InvalidDataRequestError("non_strict_pit must be a boolean")
        elif self.non_strict_pit != bool(capabilities):
            raise InvalidDataRequestError("non_strict_pit must equal the non-strict capability tuple")
        for name in ("calendar_revision_digest", "snapshot_fingerprint", "calendar_semantic_signature", "warmup_session_signature"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)):
                raise InvalidDataRequestError(f"{name} must be a lowercase SHA-256 digest")
        context = self.pit_context
        if context is not None:
            if hasattr(context, "as_dict"):
                context = dict(context.as_dict)
            elif not isinstance(context, Mapping):
                raise InvalidDataRequestError("pit_context must be a JSON mapping")
            frozen_context = freeze_json(dict(context), "pit_context")
            assert isinstance(frozen_context, MappingProxyType)
            object.__setattr__(self, "pit_context", frozen_context)
        for name in ("calendar_summary", "session_summary"):
            value = getattr(self, name)
            if value is not None:
                frozen = freeze_json(dict(value), name) if isinstance(value, Mapping) else None
                if frozen is None or not isinstance(frozen, MappingProxyType):
                    raise InvalidDataRequestError(f"{name} must be a JSON mapping")
                object.__setattr__(self, name, frozen)
        usage = tuple(self.definition_usage_by_date)
        frozen_usage: list[Mapping[str, object]] = []
        for item in usage:
            if not isinstance(item, Mapping):
                raise InvalidDataRequestError("definition_usage_by_date entries must be mappings")
            frozen = freeze_json(dict(item), "definition_usage_by_date entry")
            if not isinstance(frozen, MappingProxyType):
                raise InvalidDataRequestError("definition_usage_by_date entries must be JSON mappings")
            frozen_usage.append(frozen)
        object.__setattr__(self, "definition_usage_by_date", tuple(frozen_usage))
        if hash_version == 2:
            expected_ids = set(self.resolved_calendar_ids)
            seen_usage: set[tuple[str, str]] = set()
            for item in frozen_usage:
                scope = item.get("scope")
                usage_date = item.get("date")
                values = item.get("values_by_calendar")
                if scope not in {"formal", "warmup"} or not isinstance(usage_date, (str, date)):
                    raise InvalidDataRequestError("calendar usage must contain scope and date")
                if isinstance(usage_date, date):
                    usage_date = usage_date.isoformat()
                try:
                    parsed_date = date.fromisoformat(usage_date)
                except (TypeError, ValueError) as exc:
                    raise InvalidDataRequestError("calendar usage date must be ISO YYYY-MM-DD") from exc
                if not isinstance(values, Mapping) or set(values) != expected_ids:
                    raise InvalidDataRequestError(
                        "calendar usage must cover every resolved calendar"
                    )
                key = (str(scope), usage_date)
                if key in seen_usage:
                    raise InvalidDataRequestError("calendar usage must not repeat scope/date")
                seen_usage.add(key)
            if expected_ids and not frozen_usage:
                raise InvalidDataRequestError("hash_schema_version=2 requires calendar usage evidence")
            # Every natural day in the immutable snapshot envelope is an
            # auditable usage row.  The envelope metadata is intentionally
            # carried in calendar_summary rather than inferred from the first
            # or last usage item.
            envelope = self.calendar_summary.get("envelope") if isinstance(self.calendar_summary, Mapping) else None
            if not isinstance(envelope, Mapping):
                raise InvalidDataRequestError("hash_schema_version=2 requires calendar envelope evidence")
            envelope_start = envelope.get("start_date")
            envelope_end = envelope.get("end_date_exclusive")
            try:
                envelope_start_date = date.fromisoformat(str(envelope_start))
                envelope_end_date = date.fromisoformat(str(envelope_end))
            except (TypeError, ValueError) as exc:
                raise InvalidDataRequestError("calendar envelope dates are invalid") from exc
            if envelope_start_date >= envelope_end_date:
                raise InvalidDataRequestError("calendar envelope must be non-empty")
            expected_keys = {
                ("formal" if self.requested_window.start_date <= day <= self.requested_window.end_date else "warmup", day.isoformat())
                for offset in range((envelope_end_date - envelope_start_date).days)
                for day in (envelope_start_date + timedelta(days=offset),)
            }
            if seen_usage != expected_keys:
                raise InvalidDataRequestError("calendar usage must cover every natural day in the snapshot envelope")
        # Recompute defensively so a caller cannot forge a mismatched hash.
        # The response budget covers the complete canonical wire payload, not
        # only hash-relevant machine fields: issue titles/messages are part of
        # the synchronous blocked response and must count toward the 4 MiB
        # UTF-8 limit as well.
        report_hash = self._compute_hash()
        object.__setattr__(self, "report_hash", report_hash)
        if hash_version == 2:
            encoded_size = len(canonical_json(self.as_dict()).encode("utf-8"))
            if encoded_size > 4 * 1024 * 1024:
                raise CalendarPreflightResourceLimitExceededError(
                    "canonical preflight JSON exceeds the 4 MiB response limit",
                    details={"utf8_bytes": encoded_size, "maximum": 4 * 1024 * 1024},
                )

    @property
    def blocked(self) -> bool:
        """Whether this report is a hard gate for run creation."""

        return self.status is PreflightStatus.BLOCKED

    @property
    def fixed_instrument_ids(self) -> tuple[UUID, ...]:
        """Canonical fixed union including every non-zero opening holding."""

        return tuple(
            sorted(
                set(self.static_instrument_ids)
                | set(self.mandatory_instrument_ids)
                | set(self.non_zero_initial_position_instrument_ids),
                key=str,
            )
        )

    @property
    def primary_issue_code(self) -> str | None:
        """Stable first issue code after canonical issue ordering."""

        return self.issues[0].code if self.issues else None

    def as_dict(self) -> dict[str, object]:
        """Serialize the report for API/blocked-admission projection."""

        context = self.pit_context
        if hasattr(context, "as_dict"):
            context = dict(context.as_dict)
        details: dict[str, object] = {
            "schema_version": 2 if self.hash_schema_version == 2 else 1,
            "primary_issue_code": self.primary_issue_code,
            "run_kind": self.run_kind,
            "preflight_profile_key": self.preflight_profile_key,
            "preflight_profile_version": self.preflight_profile_version,
            "preflight_profile": (
                f"{self.preflight_profile_key}@{self.preflight_profile_version}"
                if self.preflight_profile_key is not None
                else None
            ),
            "capability_manifest_version": self.capability_manifest_version,
            "status": self.status,
            "scope_mode": self.scope_mode,
            "resolved_instruments": [
                str(item) for item in self.resolved_instruments
            ],
            "coverage_reports": [
                report.machine_content() for report in self.coverage_reports
            ],
            "instrument_mapping_coverage": self.instrument_mapping_coverage,
            "instrument_rule_fact_summary": self.instrument_rule_fact_summary,
            "lookback_session_bar_coverage": self.lookback_session_bar_coverage,
            "bar_validity_summary": self.bar_validity_summary,
            "missing_bars": self.missing_bars,
            "missing_fields": self.missing_fields,
            "invalid_bars": self.invalid_bars,
            "incomplete_rules": self.incomplete_rules,
            "non_pit_sources": self.non_pit_sources,
            "source_revisions": self.source_revisions,
            "fixture_sources": self.fixture_sources,
            "issues": [issue.as_dict() for issue in self.issues],
            "requested_window": {
                "start_date": self.requested_window.start_date,
                "end_date": self.requested_window.end_date,
            },
            "warmup": {
                "requested_sessions": self.warmup_sessions_count,
                "anchor": self.resolved_sessions[0].session_date if self.resolved_sessions else None,
            },
            "pit_context": context,
            "data_cutoff": context.get("data_cutoff") if isinstance(context, Mapping) else None,
            "cutoff_local_date": context.get("cutoff_local_date") if isinstance(context, Mapping) else None,
            "include_cutoff_day": context.get("include_cutoff_day") if isinstance(context, Mapping) else None,
            "pit_profile": context.get("pit_profile") if isinstance(context, Mapping) else None,
            "profile_version": context.get("profile_version") if isinstance(context, Mapping) else None,
            "knowledge_as_of": context.get("knowledge_as_of") if isinstance(context, Mapping) else None,
            "non_strict_pit": self.non_strict_pit,
            "non_strict_pit_capabilities": self.non_strict_pit_capabilities,
            "adjustment_series_policy": (
                {
                    "key": self.adjustment_series_policy.key,
                    "version": self.adjustment_series_policy.version,
                }
                if self.adjustment_series_policy is not None
                else None
            ),
            "adjustment_policy_status": self.adjustment_policy_status,
            "adjustment_adapter_version": self.adjustment_adapter_version,
            "adjustment_formula_version": self.adjustment_formula_version,
            "adjustment_qfq_anchor": self.adjustment_qfq_anchor,
            "adjustment_hfq_anchor": self.adjustment_hfq_anchor,
            "adjustment_factor_cutoff_rule": self.adjustment_factor_cutoff_rule,
            "adjustment_verification_input_hash": self.adjustment_verification_input_hash,
            "adjustment_verification_output_hash": self.adjustment_verification_output_hash,
            "adjustment_verification_evidence_hash": self.adjustment_verification_evidence_hash,
            "adjustment_factor_coverage": self.adjustment_factor_coverage,
            "calendar_ids": self.resolved_calendar_ids,
            "coverage": self.calendar_summary.get("coverage") if self.calendar_summary else None,
            "universe_eligibility_policy_version": (
                {
                    "key": self.universe_eligibility_policy_version.key,
                    "version": self.universe_eligibility_policy_version.version,
                }
                if isinstance(self.universe_eligibility_policy_version, ContractRef)
                else self.universe_eligibility_policy_version
            ),
            "qualification_policy_version": (
                {
                    "key": self.qualification_policy_version.key,
                    "version": self.qualification_policy_version.version,
                }
                if isinstance(self.qualification_policy_version, ContractRef)
                else self.qualification_policy_version
            ),
            "qualification_policy": (
                {
                    "key": self.qualification_policy.key,
                    "version": self.qualification_policy.version,
                }
                if isinstance(self.qualification_policy, ContractRef)
                else self.qualification_policy
            ),
            "universe_eligibility_summary": self.universe_eligibility_summary,
            "non_zero_initial_position_instrument_ids": [
                str(item) for item in self.non_zero_initial_position_instrument_ids
            ],
            "universe_scope_snapshot_hash": self.universe_scope_snapshot_hash,
            "universe_candidate_count": self.universe_candidate_count,
            "universe_filtered_reason_counts": self.universe_filtered_reason_counts,
            "universe_target_ids": [str(item) for item in self.universe_target_ids],
            "universe_final_rechecks": self.universe_final_rechecks,
            "report_hash": self.report_hash,
            "hash_schema_version": self.hash_schema_version,
            "issues_complete": True,
            "cursor": None,
            "next_cursor": None,
            "truncated": False,
            "retention": {"kind": "response_only", "persisted": False, "queryable": False},
        }
        if self.hash_schema_version == 2:
            details.update({
                "calendar_summary": self.calendar_summary,
                "session_summary": self.session_summary,
                "definition_usage_by_date": self.definition_usage_by_date,
                "calendar_revision_digest": self.calendar_revision_digest,
                "revision_digest": self.calendar_revision_digest,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "calendar_semantic_signature": self.calendar_semantic_signature,
                "calendar_session_signature": self.calendar_session_signature,
                "warmup_session_signature": self.warmup_session_signature,
            })
        if self.universe_scope_resolution is not None:
            resolver = self.universe_scope_resolution
            as_dict = getattr(resolver, "as_dict", None)
            canonical_content = getattr(resolver, "canonical_content", None)
            details["universe_scope_resolution"] = (
                as_dict() if callable(as_dict) else resolver
            )
            if not callable(as_dict) and callable(canonical_content):
                details["universe_scope_resolution"] = dict(canonical_content())
            elif not callable(as_dict) and not callable(canonical_content):
                # Never put arbitrary provider objects in an API/report
                # payload.  The machine scope hash already captures the
                # admissible semantics.
                details["universe_scope_resolution"] = None
        return {
            "status": self.status.value,
            "run_kind": self.run_kind,
            "preflight_profile": (
                f"{self.preflight_profile_key}@{self.preflight_profile_version}"
                if self.preflight_profile_key is not None
                else None
            ),
            "run_id": None,
            "persisted": False,
            "reason_code": "data_preflight_blocked" if self.blocked else None,
            "title": "数据预检未通过" if self.blocked else "数据预检",
            "message": "交易日历与会话预检未通过，运行未创建。" if self.blocked else "交易日历与会话预检已通过。",
            "details": details,
        }

    def _hash_content(self) -> dict[str, object]:
        """Build the hash-relevant machine content of this report.

        Excluded deliberately: ``generated_at``, issue ``message`` texts,
        and ``resolved_calendar_definitions`` (their semantics are already
        covered by the calendar session signature).
        """

        def sessions_payload(points: Sequence[SessionPoint]) -> list[dict[str, object]]:
            return [
                {
                    "session_date": point.session_date,
                    "session_id": point.session_id,
                    "timezone": point.timezone,
                    "sessions": [
                        {"start": w.start_time, "end": w.end_time}
                        for w in point.sessions
                    ],
                }
                for point in points
            ]

        payload = {
            "status": self.status,
            "provider_key": self.provider_key,
            "capability_manifest_version": self.capability_manifest_version,
            "data_contract_version": self.data_contract_version,
            "requested_window": {
                "start_date": self.requested_window.start_date,
                "end_date": self.requested_window.end_date,
            },
            "frequency": self.frequency,
            "warmup_sessions_count": self.warmup_sessions_count,
            "market_scope": {
                "markets": self.market_scope.markets if self.market_scope else (),
                "exchanges": self.market_scope.exchanges if self.market_scope else (),
                "asset_classes": (
                    self.market_scope.asset_classes if self.market_scope else ()
                ),
                "currencies": self.market_scope.currencies if self.market_scope else (),
            },
            "universe_query_policy": {
                "candidate_set_rules": [
                    {"key": rule.key, "version": rule.version}
                    for rule in (
                        self.universe_query_policy.candidate_set_rules
                        if self.universe_query_policy
                        else ()
                    )
                ]
            },
            "scope_mode": self.scope_mode,
            "static_instrument_ids": [
                str(item) for item in self.static_instrument_ids
            ],
            "mandatory_instrument_ids": [
                str(item) for item in self.mandatory_instrument_ids
            ],
            "rule_package": {"key": self.rule_package.key, "version": self.rule_package.version},
            "rule_exception_set": (
                {"key": self.rule_exception_set.key, "version": self.rule_exception_set.version}
                if self.rule_exception_set
                else None
            ),
            "allowed_settlement_rule_class": self.allowed_settlement_rule_class,
            "adjustment_series_policy": (
                {
                    "key": self.adjustment_series_policy.key,
                    "version": self.adjustment_series_policy.version,
                }
                if self.adjustment_series_policy
                else None
            ),
            # Keep the immutable adjustment contract in the report digest.
            # Generation timestamps and credentials are intentionally absent;
            # changing policy/evidence/coverage must produce a new hash.
            "adjustment_policy_status": self.adjustment_policy_status,
            "adjustment_adapter_version": self.adjustment_adapter_version,
            "adjustment_formula_version": self.adjustment_formula_version,
            "adjustment_qfq_anchor": self.adjustment_qfq_anchor,
            "adjustment_hfq_anchor": self.adjustment_hfq_anchor,
            "adjustment_factor_cutoff_rule": self.adjustment_factor_cutoff_rule,
            "adjustment_verification_input_hash": self.adjustment_verification_input_hash,
            "adjustment_verification_output_hash": self.adjustment_verification_output_hash,
            "adjustment_verification_evidence_hash": self.adjustment_verification_evidence_hash,
            "adjustment_factor_coverage": self.adjustment_factor_coverage,
            "quality_mode": self.quality_mode,
            "required_capabilities": self.required_capabilities,
            "strategy_price_bases": self.strategy_price_bases,
            "engine_price_basis": self.engine_price_basis,
            "resolved_calendar_ids": self.resolved_calendar_ids,
            "resolved_timezone": self.resolved_timezone,
            "calendar_axis_policy": {
                "key": self.calendar_axis_policy.key,
                "version": self.calendar_axis_policy.version,
            },
            "calendar_compatibility_status": self.calendar_compatibility_status,
            "calendar_session_signature": self.calendar_session_signature,
            "resolved_sessions": sessions_payload(self.resolved_sessions),
            "warmup_sessions": sessions_payload(self.warmup_sessions),
            "warmup_resolution_signature": self.warmup_resolution_signature,
            "calendar_axis_differences": [
                {
                    "date": difference.date,
                    "calendar_id": difference.calendar_id,
                    "field": difference.field,
                    "actual_value": difference.actual_value,
                    "expected_value": difference.expected_value,
                    # The legacy pair is retained for old clients, but the
                    # complete multi-calendar evidence is hash relevant.
                    "values_by_calendar": difference.values_by_calendar,
                    "definition_versions_by_calendar": difference.definition_versions_by_calendar,
                    "definition_fact_ids_by_calendar": difference.definition_fact_ids_by_calendar,
                    "selected_fact_ids_by_calendar": difference.selected_fact_ids_by_calendar,
                    "fact_versions_by_calendar": difference.fact_versions_by_calendar,
                    "source_revisions_by_calendar": difference.source_revisions_by_calendar,
                    "reference_calendar_id": difference.reference_calendar_id,
                    "error_code": difference.error_code,
                }
                for difference in self.calendar_axis_differences
            ],
            "warmup_axis_differences": [
                {
                    "date": difference.date,
                    "calendar_id": difference.calendar_id,
                    "field": difference.field,
                    "actual_value": difference.actual_value,
                    "expected_value": difference.expected_value,
                    "values_by_calendar": difference.values_by_calendar,
                    "definition_versions_by_calendar": difference.definition_versions_by_calendar,
                    "definition_fact_ids_by_calendar": difference.definition_fact_ids_by_calendar,
                    "selected_fact_ids_by_calendar": difference.selected_fact_ids_by_calendar,
                    "fact_versions_by_calendar": difference.fact_versions_by_calendar,
                    "source_revisions_by_calendar": difference.source_revisions_by_calendar,
                    "reference_calendar_id": difference.reference_calendar_id,
                    "error_code": difference.error_code,
                }
                for difference in self.warmup_axis_differences
            ],
            "max_lookback_sessions": self.max_lookback_sessions,
            "knowledge_as_of": self.knowledge_as_of,
            "non_strict_pit_capabilities": self.non_strict_pit_capabilities,
            "consistency_mode": self.consistency_mode,
            "consistency_token_capability": self.consistency_token_capability,
            "consistency_token_contract": (
                {
                    "key": self.consistency_token_contract.key,
                    "version": self.consistency_token_contract.version,
                }
                if self.consistency_token_contract
                else None
            ),
            "data_chunk_policy": {
                "key": self.data_chunk_policy.key,
                "version": self.data_chunk_policy.version,
            },
            "data_chunk_size_sessions": self.data_chunk_size_sessions,
            "coverage_reports": [
                report.machine_content() for report in self.coverage_reports
            ],
            "source_revisions": self.source_revisions,
            "issues": [issue.machine_fields() for issue in self.issues],
            # Candidate membership is deliberately absent.  These fields
            # capture only frozen policies and aggregate evidence; daily
            # dynamic candidate ordering is a runtime observation, not an
            # admission input.
            "non_zero_initial_position_instrument_ids": [
                str(item) for item in self.non_zero_initial_position_instrument_ids
            ],
            "universe_eligibility_policy_version": (
                {
                    "key": self.universe_eligibility_policy_version.key,
                    "version": self.universe_eligibility_policy_version.version,
                }
                if isinstance(self.universe_eligibility_policy_version, ContractRef)
                else self.universe_eligibility_policy_version
            ),
            "qualification_policy_version": (
                {
                    "key": self.qualification_policy_version.key,
                    "version": self.qualification_policy_version.version,
                }
                if isinstance(self.qualification_policy_version, ContractRef)
                else self.qualification_policy_version
            ),
            "qualification_policy": (
                {
                    "key": self.qualification_policy.key,
                    "version": self.qualification_policy.version,
                }
                if isinstance(self.qualification_policy, ContractRef)
                else self.qualification_policy
            ),
            "universe_scope_snapshot_hash": self.universe_scope_snapshot_hash,
            # Selected target ids and final-recheck details are decision audit
            # data.  Keep them out of the request-level report hash so a
            # changing daily candidate list never changes admission identity.
        }
        if self.run_kind is not None:
            payload.update(
                {
                    "run_kind": self.run_kind,
                    "preflight_profile_key": self.preflight_profile_key,
                    "preflight_profile_version": self.preflight_profile_version,
                    "resolved_instruments": [
                        str(item) for item in self.resolved_instruments
                    ],
                    "instrument_mapping_coverage": self.instrument_mapping_coverage,
                    "instrument_rule_fact_summary": self.instrument_rule_fact_summary,
                    "lookback_session_bar_coverage": self.lookback_session_bar_coverage,
                    "bar_validity_summary": self.bar_validity_summary,
                    "missing_bars": self.missing_bars,
                    "missing_fields": self.missing_fields,
                    "invalid_bars": self.invalid_bars,
                    "incomplete_rules": self.incomplete_rules,
                    "non_pit_sources": self.non_pit_sources,
                    "fixture_sources": self.fixture_sources,
                }
            )
        # Legacy @1 reports retain their historical payload exactly.  Calendar
        # PIT evidence is opt-in @2 and is never silently folded into an old
        # report merely because the Python object has compatibility defaults.
        if self.hash_schema_version == 2:
            payload.update({
                "query_boundary": (
                    {
                        "data_cutoff": self.query_boundary.data_cutoff,
                        "knowledge_as_of": self.query_boundary.knowledge_as_of,
                        "include_cutoff_day": self.query_boundary.include_cutoff_day,
                    }
                    if self.query_boundary is not None
                    else None
                ),
                "hash_schema_version": self.hash_schema_version,
                "pit_context": self.pit_context,
                "calendar_revision_digest": self.calendar_revision_digest,
                "snapshot_fingerprint": self.snapshot_fingerprint,
                "non_strict_pit": self.non_strict_pit,
                "calendar_semantic_signature": self.calendar_semantic_signature,
                "warmup_session_signature": self.warmup_session_signature,
                "definition_usage_by_date": self.definition_usage_by_date,
                "calendar_summary": self.calendar_summary,
                "session_summary": self.session_summary,
            })
        return payload

    def _compute_hash(self) -> str:
        return canonical_hash(self._hash_content())


def calendar_semantic_signature(
    calendar_ids: Sequence[str],
    days: Sequence[Mapping[str, object]],
    *,
    policy_key: str = "strict_compatible",
    policy_version: int = 1,
) -> str:
    """Hash only the final daily calendar semantics for compatibility."""

    return canonical_hash({
        "policy": {"key": policy_key, "version": policy_version},
        "calendar_ids": sorted(set(calendar_ids)),
        "days": sorted((dict(day) for day in days), key=lambda item: str(item.get("date", ""))),
    })


def calendar_revision_digest(rows: Sequence[Mapping[str, object]]) -> str:
    """Hash complete registry/fact/source revision evidence in stable order."""

    return canonical_hash(sorted((dict(row) for row in rows), key=lambda item: (
        str(item.get("calendar_id", "")),
        str(item.get("date", "")),
        str(item.get("scope_key", "")),
        str(item.get("selected_fact_id", item.get("fact_id", ""))),
    )))


def calendar_session_signature(
    semantic_signature: str,
    formal_sessions: Sequence[Mapping[str, object]],
    *,
    pit_context: Mapping[str, object] | None = None,
    revision_digest: str | None = None,
) -> str:
    """Hash formal session semantics plus selected version evidence."""

    return canonical_hash({
        "semantic_signature": semantic_signature,
        "formal_sessions": [dict(item) for item in formal_sessions],
        "pit_context": dict(pit_context or {}),
        "revision_digest": revision_digest,
    })


def warmup_session_signature(
    warmup_sessions: Sequence[Mapping[str, object]],
    *,
    requested_sessions: int,
    anchor: date | None,
    revision_digest: str | None = None,
) -> str:
    """Hash the isolated warmup tuple and its formal anchor."""

    return canonical_hash({
        "requested_sessions": requested_sessions,
        "anchor": anchor,
        "warmup_sessions": [dict(item) for item in warmup_sessions],
        "revision_digest": revision_digest,
    })


def calendar_snapshot_fingerprint(payload: Mapping[str, object]) -> str:
    """Canonical fingerprint for one immutable calendar snapshot envelope."""

    return canonical_hash(dict(payload))


def data_preflight_hash_v2(payload: Mapping[str, object]) -> str:
    """Canonical @2 report hash; the @2 label is carried separately."""

    return canonical_hash(dict(payload))
