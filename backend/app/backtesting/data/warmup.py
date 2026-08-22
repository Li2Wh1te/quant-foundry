"""Warmup-session resolution for the authoritative data session.

Warmup sessions are the historical common trading sessions immediately
preceding the first formal session.  They exist only to serve history
queries (for example ``lookback_sessions=N``) before the first formal
decision; they never enter ``TimeAxis``, never produce equity, decisions,
orders, fills, or any other backtest business record.

Resolution reuses exactly one calendar semantics: the ``strict_compatible@1``
policy from the named-calendar deliverable, applied to the same frozen
calendar set as the formal window.  Missing facts are never interpreted as
closed days, incompatible days are never skipped to reach the requested
count, and a short history blocks the run instead of being silently
trimmed.

The historical search is bounded by an injected
:class:`WarmupSessionResolver`: it must derive a finite candidate window
from provable per-calendar fact coverage *before* any resolution runs.
Nothing in this module scans indefinitely into the past.

This module is deliberately free of ORM, database session, FastAPI, and
Tushare imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Iterable, Mapping, Protocol, Sequence

from app.backtesting.calendar_axis import (
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
    CalendarAxisDataProvider,
    CalendarAxisDifference,
    CalendarAxisDifferenceField,
    CalendarAxisStatus,
    SessionPoint,
    resolve_calendar_axis,
)
from app.backtesting.data.errors import InvalidDataRequestError
from app.backtesting.data.reports import (
    PreflightIssue,
    _sorted_issues,
    _validated_session_tuple,
    canonical_hash,
)
from app.backtesting.data.requests import DateRange, IssueSeverity

__all__ = [
    "NO_FORMAL_SESSIONS",
    "WARMUP_CALENDAR_INCOMPATIBLE",
    "WARMUP_COVERAGE_INSUFFICIENT",
    "WARMUP_DEFINITION_MISSING",
    "WARMUP_FACT_MISSING",
    "WARMUP_HISTORY_UNRESOLVED",
    "WARMUP_SESSION_UNRESOLVED",
    "CoverageBoundedWarmupSessionResolver",
    "WarmupCoverageStatus",
    "WarmupResolution",
    "WarmupSessionResolver",
    "WarmupStatus",
    "resolve_warmup_sessions",
]


# ---------------------------------------------------------------------------
# Stable machine issue codes (never renamed; display copy is separate)
# ---------------------------------------------------------------------------

NO_FORMAL_SESSIONS = "NO_FORMAL_SESSIONS"
WARMUP_COVERAGE_INSUFFICIENT = "WARMUP_COVERAGE_INSUFFICIENT"
WARMUP_HISTORY_UNRESOLVED = "WARMUP_HISTORY_UNRESOLVED"
WARMUP_CALENDAR_INCOMPATIBLE = "WARMUP_CALENDAR_INCOMPATIBLE"
WARMUP_FACT_MISSING = "WARMUP_FACT_MISSING"
WARMUP_DEFINITION_MISSING = "WARMUP_DEFINITION_MISSING"
WARMUP_SESSION_UNRESOLVED = "WARMUP_SESSION_UNRESOLVED"

SCOPE_WARMUP = "warmup_sessions"
SCOPE_FORMAL = "formal_sessions"


def _plain_date(value: object, field_name: str) -> date:
    """Require a plain calendar date and reject datetimes and strings."""

    if not isinstance(value, date) or isinstance(value, datetime):
        raise InvalidDataRequestError(f"{field_name} must be a datetime.date")
    return value


def _strict_int(value: object, field_name: str) -> int:
    """Require a plain integer; booleans are not integers in this contract."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidDataRequestError(f"{field_name} must be an integer")
    return value


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class WarmupStatus(StrEnum):
    """Overall warmup-resolution outcome."""

    READY = "ready"
    BLOCKED = "blocked"


class WarmupCoverageStatus(StrEnum):
    """How warmup history coverage was established.

    ``proven`` means every day inside the resolved history window has an
    explicit fact for every requested calendar; ``insufficient`` means the
    proven window holds fewer common sessions than requested;
    ``unresolved`` means coverage itself could not be established (missing
    facts or definitions inside the window, or no provable window at all).
    """

    PROVEN = "proven"
    INSUFFICIENT = "insufficient"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class WarmupResolution:
    """Immutable result of one warmup-session resolution attempt.

    A ``ready`` resolution carries exactly ``requested_sessions`` points,
    all strictly earlier than ``first_formal_session`` and ordered
    ascending.  A ``blocked`` resolution carries an empty tuple plus at
    least one error issue, so partial results can never leak into a run.
    ``history_window`` records the bounded search window used, which keeps
    every block auditable (range, actual count, blocking reason).
    """

    requested_sessions: int
    first_formal_session: date
    status: WarmupStatus
    coverage_status: WarmupCoverageStatus
    resolved_sessions: tuple[SessionPoint, ...] = ()
    history_window: DateRange | None = None
    issues: tuple[PreflightIssue, ...] = ()
    # Locatable calendar-axis differences found inside the history window
    # (empty unless resolution hit an incompatibility).  They are machine
    # audit evidence and participate in the signature.
    axis_differences: tuple[CalendarAxisDifference, ...] = ()
    # Recomputed in __post_init__; the placeholder keeps the field defaulted.
    resolution_signature: str = ""

    def __post_init__(self) -> None:
        requested = _strict_int(self.requested_sessions, "requested_sessions")
        if requested < 0:
            raise InvalidDataRequestError(
                "requested_sessions must not be negative"
            )
        object.__setattr__(self, "requested_sessions", requested)
        object.__setattr__(
            self,
            "first_formal_session",
            _plain_date(self.first_formal_session, "first_formal_session"),
        )
        if not isinstance(self.status, WarmupStatus):
            raise InvalidDataRequestError("status must be a WarmupStatus")
        if not isinstance(self.coverage_status, WarmupCoverageStatus):
            raise InvalidDataRequestError(
                "coverage_status must be a WarmupCoverageStatus"
            )
        sessions = _validated_session_tuple(
            self.resolved_sessions, "resolved_sessions"
        )
        if self.history_window is not None and not isinstance(
            self.history_window, DateRange
        ):
            raise InvalidDataRequestError("history_window must be a DateRange")
        if self.history_window is not None:
            # The audit window itself must lie strictly before the anchor
            # and must contain every resolved session.
            if self.history_window.end_date >= self.first_formal_session:
                raise InvalidDataRequestError(
                    "history_window must end before first_formal_session"
                )
            for point in sessions:
                if not (
                    self.history_window.start_date
                    <= point.session_date
                    <= self.history_window.end_date
                ):
                    raise InvalidDataRequestError(
                        "resolved_sessions must lie inside history_window"
                    )
        # Copy, type-check, sort, and freeze the issues so later mutation of
        # the caller's sequence can never desynchronize content from the
        # resolution signature.
        issues = _sorted_issues(self.issues)
        errors = [
            issue for issue in issues
            if issue.severity is IssueSeverity.ERROR
        ]
        if self.status is WarmupStatus.READY:
            if errors:
                raise InvalidDataRequestError(
                    "ready warmup resolutions must not carry error issues"
                )
            if len(sessions) != requested:
                raise InvalidDataRequestError(
                    "ready warmup resolutions must carry exactly "
                    "requested_sessions sessions"
                )
            if requested > 0 and self.history_window is None:
                raise InvalidDataRequestError(
                    "ready warmup resolutions with requested sessions "
                    "require a proven history_window"
                )
            if any(
                point.session_date >= self.first_formal_session
                for point in sessions
            ):
                raise InvalidDataRequestError(
                    "warmup sessions must all be earlier than "
                    "first_formal_session"
                )
            if (
                self.coverage_status is not WarmupCoverageStatus.PROVEN
                and requested > 0
            ):
                raise InvalidDataRequestError(
                    "ready warmup resolutions with requested sessions must "
                    "have proven coverage"
                )
        else:
            if sessions:
                raise InvalidDataRequestError(
                    "blocked warmup resolutions must not expose partial "
                    "sessions"
                )
            if not errors:
                raise InvalidDataRequestError(
                    "blocked warmup resolutions must carry at least one "
                    "error issue"
                )
            if self.coverage_status is WarmupCoverageStatus.PROVEN:
                raise InvalidDataRequestError(
                    "blocked warmup resolutions cannot claim proven "
                    "coverage"
                )
        object.__setattr__(self, "resolved_sessions", sessions)
        object.__setattr__(self, "issues", issues)
        differences = tuple(self.axis_differences)
        for difference in differences:
            if not isinstance(difference, CalendarAxisDifference):
                raise InvalidDataRequestError(
                    "axis_differences entries must be CalendarAxisDifference "
                    "instances"
                )
        if differences and self.status is WarmupStatus.READY:
            # Difference evidence only exists for a blocked resolution; a
            # ready one proved the axis compatible inside its window.
            raise InvalidDataRequestError(
                "ready warmup resolutions must not carry axis differences"
            )
        object.__setattr__(
            self,
            "axis_differences",
            tuple(sorted(differences, key=lambda item: item.sort_key)),
        )
        object.__setattr__(
            self, "resolution_signature", self._compute_signature()
        )

    def _signature_content(self) -> dict[str, object]:
        """Machine content hashed into ``resolution_signature``.

        Chinese issue messages are excluded on purpose: wording never
        changes audit identity.
        """

        return {
            "requested_sessions": self.requested_sessions,
            "first_formal_session": self.first_formal_session,
            "status": self.status,
            "coverage_status": self.coverage_status,
            "history_window": (
                {
                    "start_date": self.history_window.start_date,
                    "end_date": self.history_window.end_date,
                }
                if self.history_window is not None
                else None
            ),
            "resolved_sessions": [
                {
                    "session_date": point.session_date,
                    "session_id": point.session_id,
                    "timezone": point.timezone,
                    "sessions": [
                        {"start": window.start_time, "end": window.end_time}
                        for window in point.sessions
                    ],
                }
                for point in self.resolved_sessions
            ],
            "axis_differences": [
                {
                    "date": difference.date,
                    "calendar_id": difference.calendar_id,
                    "field": difference.field,
                    "actual_value": difference.actual_value,
                    "expected_value": difference.expected_value,
                }
                for difference in self.axis_differences
            ],
            "issues": [issue.machine_fields() for issue in self.issues],
        }

    def _compute_signature(self) -> str:
        return canonical_hash(self._signature_content())


# ---------------------------------------------------------------------------
# Bounded history-window resolvers
# ---------------------------------------------------------------------------


class WarmupSessionResolver(Protocol):
    """Derives the bounded historical search window for warmup resolution.

    Implementations must only ever return windows they can prove complete
    from explicit fact coverage; returning ``None`` reports that the needed
    history range cannot be proven.  The resolver never decides session
    semantics itself — that stays with ``strict_compatible@1``.
    """

    def history_window(
        self,
        provider: CalendarAxisDataProvider,
        calendar_ids: tuple[str, ...],
        first_formal_session: date,
        requested_sessions: int,
    ) -> DateRange | None:
        """Return the inclusive candidate window, or ``None``."""
        ...


class CoverageBoundedWarmupSessionResolver:
    """Default resolver driven by explicit per-calendar coverage bounds.

    ``coverage_floor`` maps every requested calendar id to the earliest
    date from which its facts are provably complete.  The derived window
    starts at the latest floor across the frozen calendar set (before that
    day at least one calendar cannot distinguish closed days from missing
    facts) and ends the day before the first formal session.
    """

    def __init__(self, coverage_floor: Mapping[str, date]) -> None:
        normalized: dict[str, date] = {}
        for calendar_id, floor in coverage_floor.items():
            if not isinstance(calendar_id, str) or not calendar_id.strip():
                raise InvalidDataRequestError(
                    "coverage_floor keys must be non-blank calendar ids"
                )
            normalized[calendar_id] = _plain_date(floor, "coverage_floor value")
        self._coverage_floor: Mapping[str, date] = dict(normalized)

    def history_window(
        self,
        provider: CalendarAxisDataProvider,
        calendar_ids: tuple[str, ...],
        first_formal_session: date,
        requested_sessions: int,
    ) -> DateRange | None:
        floors = [
            self._coverage_floor.get(calendar_id) for calendar_id in calendar_ids
        ]
        if any(floor is None for floor in floors):
            return None
        start = max(floor for floor in floors if floor is not None)
        end = first_formal_session - timedelta(days=1)
        if start > end:
            return None
        return DateRange(start_date=start, end_date=end)


# ---------------------------------------------------------------------------
# Resolution coordination
# ---------------------------------------------------------------------------

_DIFFERENCE_ISSUE_CODES: Mapping[CalendarAxisDifferenceField, str] = {
    CalendarAxisDifferenceField.MISSING_FACT: WARMUP_FACT_MISSING,
    CalendarAxisDifferenceField.MISSING_DEFINITION: WARMUP_DEFINITION_MISSING,
    CalendarAxisDifferenceField.UNRESOLVED_SESSION: WARMUP_SESSION_UNRESOLVED,
}


def _difference_details(differences) -> list[dict[str, object]]:
    """JSON-safe evidence for calendar-axis differences."""

    return [
        {
            "date": difference.date.isoformat(),
            "calendar_id": difference.calendar_id,
            "field": difference.field.value,
            "actual_value": difference.actual_value,
            "expected_value": difference.expected_value,
        }
        for difference in sorted(differences, key=lambda item: item.sort_key)
    ]


def resolve_warmup_sessions(
    provider: CalendarAxisDataProvider,
    *,
    calendar_ids: tuple[str, ...],
    first_formal_session: date,
    requested_sessions: int,
    resolver: WarmupSessionResolver | None = None,
) -> WarmupResolution:
    """Resolve warmup sessions strictly before ``first_formal_session``.

    The same frozen calendar set and the same ``strict_compatible@1``
    policy as the formal window are applied to a bounded history window.
    Any blocking condition produces a ``blocked`` resolution with stable
    machine codes; success requires the exact requested count.
    """

    requested = _strict_int(requested_sessions, "requested_sessions")
    if requested < 0:
        raise InvalidDataRequestError("requested_sessions must not be negative")
    anchor = _plain_date(first_formal_session, "first_formal_session")
    if isinstance(calendar_ids, (str, bytes)) or not isinstance(
        calendar_ids, Iterable
    ):
        raise InvalidDataRequestError(
            "calendar_ids must be an iterable of non-blank strings"
        )
    collected: list[str] = []
    for calendar_id in calendar_ids:
        if not isinstance(calendar_id, str) or not calendar_id.strip():
            raise InvalidDataRequestError(
                "calendar_ids entries must be non-blank strings"
            )
        collected.append(calendar_id)
    ids = tuple(sorted(set(collected)))
    if not ids:
        raise InvalidDataRequestError("calendar_ids must not be empty")

    if requested == 0:
        # Zero warmup needs no anchor and no fabricated dates.
        return WarmupResolution(
            requested_sessions=0,
            first_formal_session=anchor,
            status=WarmupStatus.READY,
            coverage_status=WarmupCoverageStatus.PROVEN,
        )

    if resolver is None:
        # Without an injected resolver no history range can be proven, so
        # resolution fails instead of scanning indefinitely into the past.
        window = None
    else:
        window = resolver.history_window(provider, ids, anchor, requested)
    if window is None:
        return WarmupResolution(
            requested_sessions=requested,
            first_formal_session=anchor,
            status=WarmupStatus.BLOCKED,
            coverage_status=WarmupCoverageStatus.UNRESOLVED,
            issues=(
                PreflightIssue(
                    code=WARMUP_HISTORY_UNRESOLVED,
                    severity=IssueSeverity.ERROR,
                    scope=SCOPE_WARMUP,
                    message=(
                        f"无法证明首个正式会话 {anchor.isoformat()} 之前存在覆盖全部"
                        f"日历的完整历史事实范围，无法解析请求数量 {requested} 的 "
                        "warmup 会话"
                    ),
                    field="history_window",
                ),
            ),
        )
    if not isinstance(window, DateRange) or window.end_date >= anchor:
        # A resolver window touching or crossing the anchor is invalid:
        # resolution would read days at or after the first formal session.
        details = (
            {
                "history_start_date": window.start_date.isoformat(),
                "history_end_date": window.end_date.isoformat(),
                "first_formal_session": anchor.isoformat(),
            }
            if isinstance(window, DateRange)
            else None
        )
        return WarmupResolution(
            requested_sessions=requested,
            first_formal_session=anchor,
            status=WarmupStatus.BLOCKED,
            coverage_status=WarmupCoverageStatus.UNRESOLVED,
            # The invalid window is recorded in the issue details only; it
            # must not become part of the auditable resolution window.
            issues=(
                PreflightIssue(
                    code=WARMUP_HISTORY_UNRESOLVED,
                    severity=IssueSeverity.ERROR,
                    scope=SCOPE_WARMUP,
                    message=(
                        f"warmup 历史窗口上界非法：必须早于首个正式会话 "
                        f"{anchor.isoformat()}，已阻断请求数量 {requested} 的 "
                        "warmup 解析"
                    ),
                    field="history_window",
                    details=details,
                ),
            ),
        )

    axis = resolve_calendar_axis(
        provider,
        policy_key=POLICY_KEY_STRICT_COMPATIBLE,
        policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
        start_date=window.start_date,
        end_date=window.end_date,
        calendar_ids=ids,
    )

    def blocked(
        coverage: WarmupCoverageStatus, issues: Sequence[PreflightIssue]
    ) -> WarmupResolution:
        return WarmupResolution(
            requested_sessions=requested,
            first_formal_session=anchor,
            status=WarmupStatus.BLOCKED,
            coverage_status=coverage,
            resolved_sessions=(),
            history_window=window,
            axis_differences=tuple(axis.differences),
            issues=tuple(issues),
        )

    if axis.status is CalendarAxisStatus.INCOMPATIBLE:
        issues: list[PreflightIssue] = [
            PreflightIssue(
                code=WARMUP_CALENDAR_INCOMPATIBLE,
                severity=IssueSeverity.ERROR,
                scope=SCOPE_WARMUP,
                message=(
                    f"warmup 历史窗口 {window.start_date.isoformat()}.."
                    f"{window.end_date.isoformat()} 内日历轴不兼容，"
                    f"共 {len(axis.differences)} 处差异，请求数量 {requested}"
                ),
                field="calendar_axis",
                details={"differences": _difference_details(axis.differences)},
            ),
        ]
        seen_fields = {difference.field for difference in axis.differences}
        for difference_field, code in _DIFFERENCE_ISSUE_CODES.items():
            if difference_field in seen_fields:
                matching = [
                    difference
                    for difference in axis.differences
                    if difference.field is difference_field
                ]
                issues.append(
                    PreflightIssue(
                        code=code,
                        severity=IssueSeverity.ERROR,
                        scope=SCOPE_WARMUP,
                        message=(
                            f"warmup 历史窗口 {window.start_date.isoformat()}.."
                            f"{window.end_date.isoformat()} 内存在 "
                            f"{len(matching)} 处{difference_field.value}问题，"
                            f"请求数量 {requested}，实际无法确认任何共同交易会话"
                        ),
                        field=difference_field.value,
                        details={
                            "differences": _difference_details(matching),
                        },
                    ),
                )
        return blocked(WarmupCoverageStatus.UNRESOLVED, issues)

    candidates = [
        point
        for point in axis.resolved_sessions
        if point.session_date < anchor
    ]
    if len(candidates) < requested:
        actual = len(candidates)
        return blocked(
            WarmupCoverageStatus.INSUFFICIENT,
            (
                PreflightIssue(
                    code=WARMUP_COVERAGE_INSUFFICIENT,
                    severity=IssueSeverity.ERROR,
                    scope=SCOPE_WARMUP,
                    message=(
                        f"warmup 历史不足：窗口 {window.start_date.isoformat()}.."
                        f"{window.end_date.isoformat()} 内实际可确认 {actual} 个"
                        f"共同交易会话，少于请求数量 {requested}"
                    ),
                    field="resolved_sessions",
                    details={
                        "requested_sessions": requested,
                        "actual_sessions": actual,
                    },
                ),
            ),
        )

    resolved = tuple(candidates[-requested:])
    return WarmupResolution(
        requested_sessions=requested,
        first_formal_session=anchor,
        status=WarmupStatus.READY,
        coverage_status=WarmupCoverageStatus.PROVEN,
        resolved_sessions=resolved,
        history_window=window,
    )
