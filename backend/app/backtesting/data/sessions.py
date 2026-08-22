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
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Callable, Mapping

from app.backtesting.calendar_axis import (
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
    CalendarAxisDataProvider,
    CalendarAxisResolution,
    CalendarAxisStatus,
    CalendarDefinition,
    SessionPoint,
    resolve_calendar_axis,
)
from app.backtesting.data.errors import (
    DataSessionClosedError,
    InvalidDataRequestError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.protocols import DataConsistencyContext
from app.backtesting.data.reports import DataPreflightReport, PreflightIssue
from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataChunkQuery,
    DataPreflightRequest,
    DataRequest,
    IssueSeverity,
    PreflightStatus,
)
from app.backtesting.data.warmup import (
    NO_FORMAL_SESSIONS,
    SCOPE_FORMAL,
    SCOPE_WARMUP,
    WarmupResolution,
    WarmupSessionResolver,
    WarmupStatus,
    _difference_details,
    resolve_warmup_sessions,
)

__all__ = [
    "AuthoritativeDataSession",
    "DataSessionState",
]


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
    ) -> None:
        if not isinstance(request, DataRequest):
            raise InvalidDataRequestError("request must be a frozen DataRequest")
        self._request = request
        self._calendar_provider = calendar_provider
        self._warmup_resolver = warmup_resolver
        self._capability_manifest_version = capability_manifest_version
        self._on_ready = on_ready
        self._on_close = on_close
        self._state = DataSessionState.CREATED
        self._axis: CalendarAxisResolution | None = None
        self._resolved_sessions: tuple[SessionPoint, ...] | None = None
        self._warmup_sessions: tuple[SessionPoint, ...] | None = None
        self._warmup_resolution: WarmupResolution | None = None
        self._report: DataPreflightReport | None = None
        self._closed_resources = False
        self._preflight_done = False

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
    def report(self) -> DataPreflightReport | None:
        """The immutable preflight report, or ``None`` before preflight."""

        return self._report

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

        # 1-2. Resolve the formal window strictly through strict_compatible@1.
        axis = resolve_calendar_axis(
            self._calendar_provider,
            policy_key=POLICY_KEY_STRICT_COMPATIBLE,
            policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
            start_date=frozen_request.requested_window.start_date,
            end_date=frozen_request.requested_window.end_date,
            calendar_ids=frozen_request.resolved_calendar_ids,
        )
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
            knowledge_as_of=frozen_request.knowledge_as_of,
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
        )
        self._resolved_sessions = formal_sessions
        self._warmup_sessions = warmup_sessions
        self._warmup_resolution = warmup_resolution
        self._report = report
        self._preflight_done = True
        self._state = (
            DataSessionState.BLOCKED if blocked else DataSessionState.READY
        )
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
