"""Append-only SQL repository for named-calendar facts.

The repository is intentionally small and deterministic.  It never upserts a
fact row: a correction is appended with a new ``fact_id``/``fact_version``
and an explicit ``supersedes_fact_id``.  The SQL provider can therefore use
the same pure PIT selector as the in-memory provider.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.backtesting.calendar_axis import (
    CalendarCapabilityDeclaration,
    CalendarDefinition,
    canonical_hash,
    CalendarExchangeBinding,
    CalendarPITContext,
    CalendarQualityStatus,
    CalendarRegistry,
    CalendarSessionFact,
    CalendarSourcePriority,
    SessionWindow,
    normalize_calendar_id,
    normalize_window_payloads,
    select_pit_candidate,
)
from app.backtesting.calendar_models import (
    CalendarCapabilityDeclarationRecord,
    CalendarDefinitionRecord,
    CalendarExchangeBindingRecord,
    CalendarReconciliationRangeRecord,
    CalendarRegistryRecord,
    CalendarSessionFactRecord,
    CalendarResolutionHeadRecord,
    CalendarSourcePriorityRecord,
)
from app.backtesting.data.errors import (
    CalendarContractError,
    CalendarFactMissingError,
    CalendarSourceRevisionConflictError,
    ProviderContractViolationError,
)


def _utc(value: datetime | None) -> datetime | None:
    """Normalize persisted timestamps to aware UTC for domain construction."""

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _windows_payload(windows: Sequence[SessionWindow] | None) -> list[dict[str, object]] | None:
    """Convert domain windows to the canonical JSON representation."""

    if windows is None:
        return None
    return [
        {
            "start": window.start_time.isoformat(),
            "end": window.end_time.isoformat(),
            "day_offset": window.day_offset,
            "end_day_offset": window.end_day_offset,
            **({"label": window.label} if window.label is not None else {}),
        }
        for window in windows
    ]


def _evidence(value: object) -> object:
    """Keep JSON evidence JSON-safe while preserving strings used by legacy rows."""

    return value


class CalendarFactRepository:
    """Append-only writer and batch candidate reader for calendar facts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Append-only writes
    # ------------------------------------------------------------------

    def append_registry(self, value: CalendarRegistry) -> CalendarRegistryRecord:
        value.strict_validate()
        record = CalendarRegistryRecord(
            fact_id=value.fact_id or uuid4(),
            fact_version=value.fact_version,
            logical_fact_key=value.logical_fact_key,
            supersedes_fact_id=value.supersedes_fact_id,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            source=value.source,
            source_revision=value.source_revision,
            source_priority_fact_id=value.source_priority_fact_id,
            source_priority_version=value.source_priority_version,
            source_priority=value.source_priority,
            source_revision_order=value.source_revision_order,
            bootstrap_seed_id=value.bootstrap_seed_id,
            bootstrap_seed_version=value.bootstrap_seed_version,
            bootstrap_seed_hash=value.bootstrap_seed_hash,
            evidence=_evidence(value.evidence),
            known_at=value.known_at,
            knowledge_from=value.knowledge_from,
            knowledge_to=value.knowledge_to,
            knowledge_as_of=value.knowledge_as_of,
            observed_at=value.observed_at,
            quality_status=value.quality_status.value,
            content_hash=value.content_hash,
            created_at=value.created_at,
            calendar_id=value.calendar_id,
            registry_version=value.registry_version,
            display_name=value.display_name,
            timezone_policy=value.timezone_policy,
            status=value.status,
        )
        return self._add(record)

    def append_source_priority(self, value: CalendarSourcePriority) -> CalendarSourcePriorityRecord:
        value.strict_validate()
        record = CalendarSourcePriorityRecord(
            fact_id=value.fact_id or uuid4(),
            fact_version=value.fact_version,
            logical_fact_key=value.logical_fact_key,
            supersedes_fact_id=value.supersedes_fact_id,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            source=value.source,
            source_revision=value.source_revision,
            source_priority_fact_id=None,
            source_priority_version=value.source_priority_version,
            source_priority=value.source_priority,
            source_revision_order=value.source_revision_order,
            bootstrap_seed_id=value.bootstrap_seed_id,
            bootstrap_seed_version=value.bootstrap_seed_version,
            bootstrap_seed_hash=value.bootstrap_seed_hash,
            evidence=_evidence(value.evidence),
            known_at=value.known_at,
            knowledge_from=value.knowledge_from,
            knowledge_to=value.knowledge_to,
            knowledge_as_of=value.knowledge_as_of,
            observed_at=value.observed_at,
            quality_status=value.quality_status.value,
            content_hash=value.content_hash,
            created_at=value.created_at,
        )
        return self._add(record)

    def append_definition(self, value: CalendarDefinition) -> CalendarDefinitionRecord:
        value.strict_validate()
        record = CalendarDefinitionRecord(
            fact_id=value.fact_id or uuid4(),
            fact_version=value.fact_version,
            logical_fact_key=value.logical_fact_key,
            supersedes_fact_id=value.supersedes_fact_id,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            source=value.source,
            source_revision=value.source_revision,
            source_priority_fact_id=value.source_priority_fact_id,
            source_priority_version=value.source_priority_version,
            source_priority=value.source_priority,
            source_revision_order=value.source_revision_order,
            bootstrap_seed_id=value.bootstrap_seed_id,
            bootstrap_seed_version=value.bootstrap_seed_version,
            bootstrap_seed_hash=value.bootstrap_seed_hash,
            evidence=_evidence(value.evidence),
            known_at=value.known_at,
            knowledge_from=value.knowledge_from,
            knowledge_to=value.knowledge_to,
            knowledge_as_of=value.knowledge_as_of,
            observed_at=value.observed_at,
            quality_status=value.quality_status.value,
            content_hash=value.content_hash,
            created_at=value.created_at,
            calendar_id=value.calendar_id,
            registry_fact_id=value.registry_fact_id,
            registry_version=value.registry_version,
            definition_version=value.definition_version,
            timezone=value.timezone,
            default_sessions=_windows_payload(value.default_sessions),
        )
        return self._add(record)

    def append_session_fact(self, value: CalendarSessionFact) -> CalendarSessionFactRecord:
        value.strict_validate()
        record = CalendarSessionFactRecord(
            fact_id=value.fact_id or uuid4(),
            fact_version=value.fact_version,
            logical_fact_key=value.logical_fact_key,
            supersedes_fact_id=value.supersedes_fact_id,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            source=value.source,
            source_revision=value.source_revision,
            source_priority_fact_id=value.source_priority_fact_id,
            source_priority_version=value.source_priority_version,
            source_priority=value.source_priority,
            source_revision_order=value.source_revision_order,
            bootstrap_seed_id=value.bootstrap_seed_id,
            bootstrap_seed_version=value.bootstrap_seed_version,
            bootstrap_seed_hash=value.bootstrap_seed_hash,
            evidence=_evidence(value.evidence),
            known_at=value.known_at,
            knowledge_from=value.knowledge_from,
            knowledge_to=value.knowledge_to,
            knowledge_as_of=value.knowledge_as_of,
            observed_at=value.observed_at,
            quality_status=value.quality_status.value,
            content_hash=value.content_hash,
            created_at=value.created_at,
            calendar_id=value.calendar_id,
            session_date=value.session_date,
            registry_fact_id=value.registry_fact_id,
            registry_version=value.registry_version,
            definition_version=value.definition_version,
            definition_fact_id=value.definition_fact_id,
            is_open=value.is_open,
            timezone_override=value.timezone_override,
            sessions_override=_windows_payload(value.sessions_override),
            override_mode=value.override_mode,
        )
        return self._add(record)

    def rebuild_resolution_heads(
        self,
        *,
        calendar_id: str,
        start_date: Any,
        end_date: Any,
    ) -> int:
        """Rebuild PIT resolution-head slices from immutable session facts.

        Heads are a replaceable query index, not a second fact source.  Each
        natural day gets non-overlapping knowledge slices for the candidate
        selected by the strict PIT/source-priority ranking, allowing the SQL
        snapshot prepare query to select the correct historical fact without
        reading fact payloads.  The source facts remain append-only and are
        never deleted or updated.
        """

        canonical = normalize_calendar_id(calendar_id)
        end_exclusive = end_date + timedelta(days=1)
        facts = self.list_session_facts((canonical,), start_date, end_exclusive)
        grouped: dict[Any, list[CalendarSessionFact]] = {}
        for fact in facts:
            if fact.known_at is None:
                raise CalendarContractError("resolution head requires known_at")
            grouped.setdefault(fact.session_date, []).append(fact)
        # A head is one selected natural-day cell, not one row per logical
        # chain.  The resolver ranks all same-day candidates together, so a
        # newer low-priority source must not hide an older high-priority row.
        priorities = self.list_source_priorities()
        from app.backtesting.data.requests import QueryBoundary
        # Head rows are rebuildable materialized state.  Remove only the
        # requested scope before writing the deterministic new projection.
        self.session.execute(
            delete(CalendarResolutionHeadRecord).where(
                CalendarResolutionHeadRecord.calendar_id == canonical,
                CalendarResolutionHeadRecord.effective_date >= start_date,
                CalendarResolutionHeadRecord.effective_date <= end_date,
            )
        )
        payload: list[dict[str, Any]] = []
        for day, candidates in grouped.items():
            # Selection can change at a fact or source-priority knowledge
            # boundary.  Re-evaluate at every boundary using the canonical
            # PIT selector instead of duplicating its ranking rules here.
            fact_times = {fact.known_at for fact in candidates}
            first_fact_time = min(fact_times)
            event_times = set(fact_times)
            event_times.update(
                priority.known_at
                for priority in priorities
                if priority.known_at is not None
                and priority.known_at >= first_fact_time
                and priority.source in {fact.source for fact in candidates}
                and priority.applies_to(day)
            )
            ordered_times = sorted(event_times)
            slices: list[dict[str, Any]] = []
            for known_at in ordered_times:
                assert known_at is not None
                context = CalendarPITContext.from_query_boundary(
                    QueryBoundary(data_cutoff=known_at, include_cutoff_day=True)
                )
                fact = select_pit_candidate(
                    candidates,
                    effective_day=day,
                    pit_context=context,
                    source_priorities=priorities,
                )
                if not isinstance(fact, CalendarSessionFact):
                    raise ProviderContractViolationError(
                        "selected resolution head row is not a session fact"
                    )
                if slices and slices[-1]["selected_fact_id"] == fact.fact_id:
                    # The same fact remains selected across a lower-priority
                    # source arrival or another irrelevant PIT boundary.
                    continue
                if slices:
                    slices[-1]["knowledge_to"] = known_at
                slices.append(
                    {
                        "logical_fact_key": fact.logical_fact_key,
                        "calendar_id": canonical,
                        "effective_date": day,
                        "is_open": fact.is_open,
                        "selected_fact_id": fact.fact_id,
                        "selected_fact_version": fact.fact_version,
                        "knowledge_from": known_at,
                        "knowledge_to": None,
                    }
                )
            payload.extend(slices)
        if not payload:
            return 0
        revision_digest = canonical_hash(
            sorted(
                [
                    {
                        "logical_fact_key": item["logical_fact_key"],
                        "effective_date": item["effective_date"],
                        "selected_fact_id": item["selected_fact_id"],
                        "selected_fact_version": item["selected_fact_version"],
                        "is_open": item["is_open"],
                    }
                    for item in payload
                ],
                key=lambda item: (
                    item["logical_fact_key"],
                    item["effective_date"].isoformat(),
                    str(item["selected_fact_id"]),
                ),
            )
        )
        for item in payload:
            item["revision_digest"] = revision_digest
            item["id"] = uuid4()
        self.session.add_all([CalendarResolutionHeadRecord(**item) for item in payload])
        self.session.flush()
        return len(payload)

    def append_session_facts_idempotent(
        self, values: Iterable[CalendarSessionFact]
    ) -> tuple[int, int, int]:
        """Append one batch, returning fetched/changed/unchanged counts.

        A matching logical key, semantic content hash and source revision is
        a replay of the same source batch and is not inserted again.  A
        changed source row receives the next fact version and points to the
        previous row; no existing row is updated.
        """

        received = changed = unchanged = 0
        for value in values:
            received += 1
            existing = self.session.scalars(
                select(CalendarSessionFactRecord).where(
                    CalendarSessionFactRecord.logical_fact_key == value.logical_fact_key,
                    CalendarSessionFactRecord.content_hash == value.content_hash,
                    CalendarSessionFactRecord.source_revision == value.source_revision,
                )
            ).first()
            if existing is not None:
                unchanged += 1
                continue
            latest = self.session.scalars(
                select(CalendarSessionFactRecord).where(
                    CalendarSessionFactRecord.logical_fact_key == value.logical_fact_key
                ).order_by(CalendarSessionFactRecord.fact_version.desc())
            ).first()
            if latest is not None:
                value = replace(
                    value,
                    fact_id=uuid4(),
                    fact_version=max(value.fact_version, latest.fact_version + 1),
                    supersedes_fact_id=latest.fact_id,
                )
            self.append_session_fact(value)
            changed += 1
        return received, changed, unchanged

    def append_binding(self, value: CalendarExchangeBinding) -> CalendarExchangeBindingRecord:
        value.strict_validate()
        record = CalendarExchangeBindingRecord(
            fact_id=value.fact_id or uuid4(),
            fact_version=value.fact_version,
            logical_fact_key=value.logical_fact_key,
            supersedes_fact_id=value.supersedes_fact_id,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            source=value.source,
            source_revision=value.source_revision,
            source_priority_fact_id=value.source_priority_fact_id,
            source_priority_version=value.source_priority_version,
            source_priority=value.source_priority,
            source_revision_order=value.source_revision_order,
            bootstrap_seed_id=value.bootstrap_seed_id,
            bootstrap_seed_version=value.bootstrap_seed_version,
            bootstrap_seed_hash=value.bootstrap_seed_hash,
            evidence=_evidence(value.evidence),
            known_at=value.known_at,
            knowledge_from=value.knowledge_from,
            knowledge_to=value.knowledge_to,
            knowledge_as_of=value.knowledge_as_of,
            observed_at=value.observed_at,
            quality_status=value.quality_status.value,
            content_hash=value.content_hash,
            created_at=value.created_at,
            alias=value.alias,
            canonical_calendar_id=value.canonical_calendar_id,
            registry_fact_id=value.registry_fact_id,
            registry_version=value.registry_version,
            binding_version=value.binding_version,
        )
        return self._add(record)

    def append_capability(self, value: CalendarCapabilityDeclaration) -> CalendarCapabilityDeclarationRecord:
        value.strict_validate()
        record = CalendarCapabilityDeclarationRecord(
            fact_id=value.fact_id or uuid4(),
            fact_version=value.fact_version,
            logical_fact_key=value.logical_fact_key,
            supersedes_fact_id=value.supersedes_fact_id,
            valid_from=value.valid_from,
            valid_to=value.valid_to,
            source=value.source,
            source_revision=value.source_revision,
            source_priority_fact_id=value.source_priority_fact_id,
            source_priority_version=value.source_priority_version,
            source_priority=value.source_priority,
            source_revision_order=value.source_revision_order,
            bootstrap_seed_id=value.bootstrap_seed_id,
            bootstrap_seed_version=value.bootstrap_seed_version,
            bootstrap_seed_hash=value.bootstrap_seed_hash,
            evidence=_evidence(value.evidence),
            known_at=value.known_at,
            knowledge_from=value.knowledge_from,
            knowledge_to=value.knowledge_to,
            knowledge_as_of=value.knowledge_as_of,
            observed_at=value.observed_at,
            quality_status=value.quality_status.value,
            content_hash=value.content_hash,
            created_at=value.created_at,
            scope_kind=value.scope_kind,
            scope_key=value.scope_key,
            provider_key=value.provider_key,
            package_key=value.package_key,
            package_version=str(value.package_version) if value.package_version is not None else None,
            calendar_id=value.calendar_id,
            registry_fact_id=value.registry_fact_id,
            registry_version=value.registry_version,
            instrument_id=value.instrument_id,
            capability=value.capability,
            value=value.value.value,
            applicability=value.applicability.value if value.applicability else None,
        )
        return self._add(record)

    def _add(self, record: Any) -> Any:
        """Add without conflict replacement; callers commit the transaction."""

        self.session.add(record)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise CalendarSourceRevisionConflictError(
                "append-only calendar fact violates its identity/version contract"
            ) from exc
        return record

    # ------------------------------------------------------------------
    # Batch readers
    # ------------------------------------------------------------------

    def list_registries(self, calendar_ids: Iterable[str] | None = None) -> tuple[CalendarRegistry, ...]:
        statement = select(CalendarRegistryRecord)
        if calendar_ids is not None:
            statement = statement.where(CalendarRegistryRecord.calendar_id.in_(tuple(calendar_ids)))
        rows = self.session.scalars(statement.order_by(CalendarRegistryRecord.calendar_id, CalendarRegistryRecord.registry_version, CalendarRegistryRecord.fact_version)).all()
        return tuple(_registry(row) for row in rows)

    def list_definitions(self, calendar_ids: Iterable[str]) -> tuple[CalendarDefinition, ...]:
        ids = tuple(calendar_ids)
        rows = self.session.scalars(select(CalendarDefinitionRecord).where(CalendarDefinitionRecord.calendar_id.in_(ids)).order_by(CalendarDefinitionRecord.calendar_id, CalendarDefinitionRecord.valid_from, CalendarDefinitionRecord.fact_version)).all()
        return tuple(_definition(row) for row in rows)

    def list_session_facts(self, calendar_ids: Iterable[str], start_date: Any, end_date: Any) -> tuple[CalendarSessionFact, ...]:
        ids = tuple(calendar_ids)
        rows = self.session.scalars(select(CalendarSessionFactRecord).where(
            CalendarSessionFactRecord.calendar_id.in_(ids),
            CalendarSessionFactRecord.session_date >= start_date,
            CalendarSessionFactRecord.session_date < end_date,
        ).order_by(CalendarSessionFactRecord.calendar_id, CalendarSessionFactRecord.session_date, CalendarSessionFactRecord.fact_version)).all()
        return tuple(_session(row) for row in rows)

    def list_open_dates(
        self,
        *,
        calendar_id: str,
        start_date: Any,
        end_date: Any,
        data_cutoff: datetime,
    ) -> list[Any]:
        """Return PIT-selected open dates from named calendar facts only.

        ETF ingestion must not use the mutable legacy exchange calendar as a
        substitute for an identity-bound named calendar.  Every natural day
        in the requested range needs an explicit, accepted session fact; a
        missing day is a coverage error rather than an implicit closed day.
        """

        canonical = normalize_calendar_id(calendar_id)
        from app.backtesting.data.requests import QueryBoundary

        boundary = QueryBoundary(data_cutoff=data_cutoff, include_cutoff_day=True)
        context = CalendarPITContext.from_query_boundary(boundary)
        start = start_date
        end = end_date
        boundary.require_not_past_cutoff(
            end,
            "Asia/Shanghai",
            "calendar end_date",
        )
        facts = self.list_session_facts(
            (canonical,), start, end + timedelta(days=1)
        )
        priorities = self.list_source_priorities()
        by_day: dict[Any, list[CalendarSessionFact]] = {}
        for fact in facts:
            by_day.setdefault(fact.session_date, []).append(fact)
        result: list[Any] = []
        day = start
        while day <= end:
            candidates = by_day.get(day, [])
            if not candidates:
                raise CalendarFactMissingError(
                    "named calendar has no explicit fact for requested natural day",
                    details={"calendar_id": canonical, "date": day.isoformat()},
                )
            selected = select_pit_candidate(
                candidates,
                effective_day=day,
                pit_context=context,
                source_priorities=priorities,
            )
            if not isinstance(selected, CalendarSessionFact):
                raise ProviderContractViolationError("selected calendar row is not a session fact")
            try:
                selected.strict_validate()
            except CalendarContractError:
                raise
            if selected.is_open:
                result.append(day)
            day += timedelta(days=1)
        return result

    def list_bindings(self, aliases: Iterable[str] | None = None) -> tuple[CalendarExchangeBinding, ...]:
        statement = select(CalendarExchangeBindingRecord)
        if aliases is not None:
            statement = statement.where(CalendarExchangeBindingRecord.alias.in_(tuple(aliases)))
        rows = self.session.scalars(statement.order_by(CalendarExchangeBindingRecord.alias, CalendarExchangeBindingRecord.fact_version)).all()
        return tuple(_binding(row) for row in rows)

    def list_capabilities(self, *, scope_keys: Iterable[str] | None = None) -> tuple[CalendarCapabilityDeclaration, ...]:
        statement = select(CalendarCapabilityDeclarationRecord)
        if scope_keys is not None:
            statement = statement.where(CalendarCapabilityDeclarationRecord.scope_key.in_(tuple(scope_keys)))
        rows = self.session.scalars(statement.order_by(CalendarCapabilityDeclarationRecord.scope_kind, CalendarCapabilityDeclarationRecord.scope_key, CalendarCapabilityDeclarationRecord.capability, CalendarCapabilityDeclarationRecord.fact_version)).all()
        return tuple(_capability(row) for row in rows)

    def list_source_priorities(self, sources: Iterable[str] | None = None) -> tuple[CalendarSourcePriority, ...]:
        statement = select(CalendarSourcePriorityRecord)
        if sources is not None:
            statement = statement.where(CalendarSourcePriorityRecord.source.in_(tuple(sources)))
        rows = self.session.scalars(statement.order_by(CalendarSourcePriorityRecord.source, CalendarSourcePriorityRecord.fact_version)).all()
        return tuple(_priority(row) for row in rows)

    def enqueue_reconciliation(
        self,
        *,
        calendar_id: str,
        range_start: Any,
        range_end: Any,
        source_revision: str,
        reason: str,
    ) -> CalendarReconciliationRangeRecord:
        """Queue one affected range idempotently for source reconciliation."""

        canonical = normalize_calendar_id(calendar_id)
        existing = self.session.scalars(
            select(CalendarReconciliationRangeRecord).where(
                CalendarReconciliationRangeRecord.calendar_id == canonical,
                CalendarReconciliationRangeRecord.range_start == range_start,
                CalendarReconciliationRangeRecord.range_end == range_end,
                CalendarReconciliationRangeRecord.source_revision == source_revision,
                CalendarReconciliationRangeRecord.reason == reason,
                CalendarReconciliationRangeRecord.status.in_(("pending", "running")),
            )
        ).first()
        if existing is not None:
            return existing
        row = CalendarReconciliationRangeRecord(
            id=uuid4(),
            calendar_id=canonical,
            range_start=range_start,
            range_end=range_end,
            source_revision=source_revision,
            reason=reason,
            status="pending",
            rescan_count=0,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def complete_reconciliation(self, reconciliation_id: UUID) -> None:
        row = self.session.get(CalendarReconciliationRangeRecord, reconciliation_id)
        if row is None:
            raise ProviderContractViolationError("reconciliation range does not exist")
        row.status = "completed"
        row.completed_at = datetime.now(timezone.utc)
        row.rescan_count += 1


# ---------------------------------------------------------------------------
# Domain materializers
# ---------------------------------------------------------------------------


def _common(row: Any) -> dict[str, object]:
    return {
        "fact_id": row.fact_id,
        "fact_version": row.fact_version,
        "logical_fact_key": row.logical_fact_key,
        "supersedes_fact_id": row.supersedes_fact_id,
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
        "source": row.source,
        "source_revision": row.source_revision,
        "source_priority_fact_id": row.source_priority_fact_id,
        "source_priority_version": row.source_priority_version,
        "source_priority": row.source_priority,
        "source_revision_order": row.source_revision_order,
        "bootstrap_seed_id": row.bootstrap_seed_id,
        "bootstrap_seed_version": row.bootstrap_seed_version,
        "bootstrap_seed_hash": row.bootstrap_seed_hash,
        "evidence": row.evidence,
        "known_at": _utc(row.known_at),
        "knowledge_from": _utc(row.knowledge_from),
        "knowledge_to": _utc(row.knowledge_to),
        "knowledge_as_of": _utc(row.knowledge_as_of),
        "observed_at": _utc(row.observed_at),
        "quality_status": row.quality_status,
        "content_hash": row.content_hash,
        "created_at": _utc(row.created_at),
    }


def _registry(row: CalendarRegistryRecord) -> CalendarRegistry:
    return CalendarRegistry(**_common(row), calendar_id=row.calendar_id, display_name=row.display_name, timezone_policy=row.timezone_policy, status=row.status, registry_version=row.registry_version)


def _priority(row: CalendarSourcePriorityRecord) -> CalendarSourcePriority:
    values = _common(row)
    # Priority rows are the bootstrap-rooted exception to ordinary fact
    # priority references.  Preserve their source-revision evidence while
    # removing only the ordinary-fact reference that is not a field on the
    # priority domain object.
    values.pop("source_priority_fact_id", None)
    return CalendarSourcePriority(**values)


def _definition(row: CalendarDefinitionRecord) -> CalendarDefinition:
    return CalendarDefinition(**_common(row), calendar_id=row.calendar_id, registry_fact_id=row.registry_fact_id, registry_version=row.registry_version, definition_version=row.definition_version, timezone=row.timezone, default_sessions=normalize_window_payloads(row.default_sessions, "default_sessions"))


def _session(row: CalendarSessionFactRecord) -> CalendarSessionFact:
    override = None if row.sessions_override is None else normalize_window_payloads(row.sessions_override, "sessions_override")
    return CalendarSessionFact(**_common(row), calendar_id=row.calendar_id, session_date=row.session_date, registry_fact_id=row.registry_fact_id, registry_version=row.registry_version, definition_version=row.definition_version, definition_fact_id=row.definition_fact_id, is_open=row.is_open, timezone_override=row.timezone_override, sessions_override=override, override_mode=row.override_mode)


def _binding(row: CalendarExchangeBindingRecord) -> CalendarExchangeBinding:
    return CalendarExchangeBinding(**_common(row), alias=row.alias, canonical_calendar_id=row.canonical_calendar_id, registry_fact_id=row.registry_fact_id, registry_version=row.registry_version, binding_version=row.binding_version)


def _capability(row: CalendarCapabilityDeclarationRecord) -> CalendarCapabilityDeclaration:
    values = _common(row)
    return CalendarCapabilityDeclaration(**values, scope_kind=row.scope_kind, scope_key=row.scope_key, provider_key=row.provider_key, package_key=row.package_key, package_version=row.package_version, calendar_id=row.calendar_id, registry_fact_id=row.registry_fact_id, registry_version=row.registry_version, instrument_id=row.instrument_id, capability=row.capability, value=row.value, applicability=row.applicability)


# Stable aliases used by adapters/tests during the migration window.
CalendarRepository = CalendarFactRepository
CalendarAxisRepository = CalendarFactRepository

__all__ = ["CalendarFactRepository", "CalendarRepository", "CalendarAxisRepository"]
