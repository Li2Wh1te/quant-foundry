"""PostgreSQL persistence for stable instrument identities and PIT mappings."""

from datetime import UTC, date, datetime
from collections.abc import Mapping
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.backtesting.data.errors import (
    IdentityMappingConflictError,
    IdentityMappingEvidenceMissingError,
    IdentityMappingIncompleteError,
)
from app.instruments.domain import (
    InstrumentCodeMapping,
    order_mapping_segments,
)
from app.instruments.models import Instrument, InstrumentCodeMappingRecord


class InstrumentCodeMappingRepository:
    """Read and append PIT source-code mappings without owning transactions.

    Reads filter by both the validity window and ``data_cutoff`` knowledge
    time, then validate the surviving segments: overlaps and internal
    coverage gaps surface as explicit domain errors instead of being
    silently repaired with today's codes.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve_code_mappings(
        self,
        instrument_id: UUID,
        *,
        source: str,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> tuple[InstrumentCodeMapping, ...]:
        """Return evidenced mappings fully covering ``[start_date, end_date]``.

        A mapping intersects the window when its half-open validity range
        covers at least one day in it.  Only rows with ``known_at <=
        data_cutoff`` participate.  The surviving segments must jointly
        cover the whole requested window and chain without overlap or gap;
        violations raise :class:`MappingConflictError` or
        :class:`MappingCoverageGapError` — including a leading gap (first
        segment starts after ``start_date``), a trailing gap (last segment
        ends on or before ``end_date``), and an empty result.
        """

        if not isinstance(data_cutoff, datetime):
            raise DomainValidationError("data_cutoff must be a datetime")
        cutoff = _aware_datetime(data_cutoff, "data_cutoff")
        if not isinstance(instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        if not isinstance(start_date, date) or isinstance(start_date, datetime):
            raise DomainValidationError("start_date must be a calendar date")
        if not isinstance(end_date, date) or isinstance(end_date, datetime):
            raise DomainValidationError("end_date must be a calendar date")
        if start_date > end_date:
            raise DomainValidationError("start_date cannot be after end_date")
        normalized_source = source.strip() if isinstance(source, str) else source
        if not isinstance(normalized_source, str) or not normalized_source:
            raise DomainValidationError("source must be non-blank text")
        source_lookup = normalized_source.casefold()

        # Fetch every cutoff-visible row for this identity/source pair before
        # applying the effective window.  A correction may change its
        # effective interval; filtering first would leave its predecessor in
        # place and incorrectly resurrect it when the requested day is no
        # longer covered by the correction.  The selected ``intersects``
        # expression keeps the effective-window predicates visible to query
        # inspectors while the Python fold below applies them after revision
        # resolution.
        intersects_window = and_(
            InstrumentCodeMappingRecord.valid_from <= end_date,
            or_(
                InstrumentCodeMappingRecord.valid_to.is_(None),
                InstrumentCodeMappingRecord.valid_to > start_date,
            ),
        ).label("intersects_requested_window")
        rows = self.session.execute(
            select(InstrumentCodeMappingRecord, intersects_window)
            .where(
                InstrumentCodeMappingRecord.instrument_id == instrument_id,
                # Keep the direct equality arm for simple indexed lookups;
                # the normalized arm makes imports resilient to legacy rows
                # containing casing or surrounding whitespace differences.
                or_(
                    InstrumentCodeMappingRecord.source == normalized_source,
                    func.lower(func.trim(InstrumentCodeMappingRecord.source))
                    == source_lookup,
                ),
                # Knowledge-time visibility: facts learned after the cutoff
                # do not exist for this query.
                InstrumentCodeMappingRecord.known_at <= cutoff,
            )
            .order_by(InstrumentCodeMappingRecord.valid_from)
        ).scalars().all()
        # A provider/session double must not be able to smuggle a row from a
        # different identity or source past the SQL predicate.  Production
        # databases enforce this through the WHERE clause; re-checking here
        # keeps the public boundary deterministic for every adapter.
        for row in rows:
            row_instrument = getattr(row, "instrument_id", None)
            row_source = getattr(row, "source", None)
            if (
                row_instrument != instrument_id
                or not isinstance(row_source, str)
                or row_source.strip().casefold() != source_lookup
            ):
                raise IdentityMappingIncompleteError(
                    "stored mapping row does not belong to the queried instrument/source",
                    details={
                        "instrument_id": _mapping_detail_value(instrument_id),
                        "source": normalized_source,
                        "source_code": _mapping_detail_value(
                            getattr(row, "source_code", None)
                        ),
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                        "data_cutoff": _mapping_detail_value(cutoff),
                        "expected": {
                            "instrument_id": _mapping_detail_value(instrument_id),
                            "source": normalized_source,
                        },
                        "actual": {
                            "instrument_id": _mapping_detail_value(row_instrument),
                            "source": _mapping_detail_value(row_source),
                        },
                        "fact_version": _mapping_detail_value(
                            getattr(row, "fact_version", None)
                        ),
                    },
                )
        duplicate_versions: dict[tuple[str, int], list[object]] = {}
        for row in rows:
            logical_key = getattr(row, "logical_fact_key", None) or (
                f"legacy:{getattr(row, 'id', id(row))}"
            )
            version = getattr(row, "fact_version", 1) or 1
            duplicate_versions.setdefault((logical_key, version), []).append(row)
        for (logical_key, version), duplicates in duplicate_versions.items():
            if len(duplicates) < 2:
                continue
            first = duplicates[0]
            fields = (
                "id",
                "instrument_id",
                "source",
                "source_code",
                "trading_code",
                "valid_from",
                "valid_to",
                "source_revision",
                "mapping_source",
                "evidence", "known_at", "observed_at", "fact_version",
                "logical_fact_key",
                "supersedes_fact_id",
            )
            same = all(
                getattr(item, "id", None) == getattr(first, "id", None)
                and all(
                    getattr(item, field, None) == getattr(first, field, None)
                    for field in fields
                )
                for item in duplicates[1:]
            )
            if same:
                continue
            raise IdentityMappingConflictError(
                "duplicate immutable mapping fact version in one logical fact chain",
                details={
                    "instrument_id": _mapping_detail_value(instrument_id),
                    "source": normalized_source,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "data_cutoff": _mapping_detail_value(cutoff),
                    "expected": "one immutable fact per logical_fact_key/fact_version",
                    "actual": len(duplicates),
                    "logical_fact_key": logical_key,
                    "fact_version": version,
                    "fact_ids": [
                        _mapping_detail_value(getattr(item, "id", None))
                        for item in duplicates
                    ],
                },
            )
        # A correction appends a newer revision under the same logical key.
        # Resolve that chain before checking effective coverage; otherwise a
        # legitimate correction would look like an overlap with the fact it
        # supersedes.  Distinct logical keys remain visible and are checked
        # as conflicts below.
        latest_by_logical: dict[str, InstrumentCodeMappingRecord] = {}
        for row in rows:
            logical_key = getattr(row, "logical_fact_key", None)
            if not logical_key:
                # Legacy rows predate logical keys.  Keep their own row as an
                # independent fact instead of inventing a revision chain.
                logical_key = f"legacy:{row.id}"
            current = latest_by_logical.get(logical_key)
            if current is None or (
                _stored_aware(row.known_at, "known_at"),
                getattr(row, "fact_version", 1),
            ) > (
                _stored_aware(current.known_at, "known_at"),
                getattr(current, "fact_version", 1),
            ):
                latest_by_logical[logical_key] = row
        candidates = [
            row
            for row in latest_by_logical.values()
            if row.valid_from <= end_date
            and (row.valid_to is None or row.valid_to > start_date)
        ]
        try:
            domains = [
                _to_domain(
                    row,
                    instrument_id=instrument_id,
                    source=normalized_source,
                    session_date=start_date,
                    data_cutoff=cutoff,
                )
                for row in candidates
            ]
            return order_mapping_segments(
                domains,
                start_date=start_date,
                end_date=end_date,
                data_cutoff=cutoff,
                instrument_id=instrument_id,
                source=normalized_source,
            )
        except (
            IdentityMappingIncompleteError,
            IdentityMappingConflictError,
            IdentityMappingEvidenceMissingError,
        ) as exc:
            # Keep diagnostics machine-readable and include the full
            # requested date window, not just the first failing session.
            details = dict(getattr(exc, "details", {}) or {})
            details.setdefault("start_date", start_date.isoformat())
            details.setdefault("end_date", end_date.isoformat())
            raise type(exc)(str(exc), details=details) from exc

    def add_mapping(self, mapping: InstrumentCodeMapping) -> UUID:
        """Append one mapping in strictly increasing knowledge order.

        Historical corrections/backfills must use the explicitly named
        ``append_historical_mapping`` or ``append_reconstructed_mapping``
        entry points.  No update path exists: corrections are new rows so
        history stays reproducible for any later ``data_cutoff``.
        """

        return self._append_mapping(mapping, allow_historical=False)

    def append_historical_mapping(self, mapping: InstrumentCodeMapping) -> UUID:
        """Append a deliberately reconstructed historical mapping fact.

        This is the only mapping append path that permits ``known_at`` to be
        earlier than the current logical chain head.  Version continuity and
        ``supersedes_fact_id`` still have to identify the immediate prior row.
        """

        return self._append_mapping(mapping, allow_historical=True)

    append_reconstructed_mapping = append_historical_mapping

    def _append_mapping(
        self, mapping: InstrumentCodeMapping, *, allow_historical: bool
    ) -> UUID:
        """Validate and persist one mapping row without mutating history."""

        if not isinstance(mapping, InstrumentCodeMapping):
            raise DomainValidationError("mapping must be an InstrumentCodeMapping")
        _ensure_instrument_exists(self.session, mapping.instrument_id)
        # Ordinary appends must move the knowledge boundary forward for a
        # logical fact chain.  Historical corrections explicitly identify
        # their predecessor and may be inserted through a reconstruction
        # workflow; no row is ever updated in place.
        execute = getattr(self.session, "execute", None)
        if callable(execute):
            existing_rows = execute(
                select(InstrumentCodeMappingRecord).where(
                    InstrumentCodeMappingRecord.logical_fact_key
                    == mapping.logical_fact_key
                )
            ).scalars().all()
            if existing_rows:
                latest = max(
                    existing_rows,
                    key=lambda row: (
                        getattr(row, "fact_version", 1),
                        _stored_aware(row.known_at, "known_at"),
                    ),
                )
                if mapping.fact_version != getattr(latest, "fact_version", 1) + 1:
                    raise DomainValidationError(
                        "mapping fact_version must be the next version in the logical fact chain"
                    )
                if mapping.supersedes_fact_id is None:
                    raise DomainValidationError(
                        "a mapping revision must reference the preceding fact through supersedes_fact_id"
                    )
                if mapping.supersedes_fact_id != getattr(latest, "id"):
                    raise DomainValidationError(
                        "mapping supersedes_fact_id must reference the latest fact in the same logical chain"
                    )
                if getattr(latest, "instrument_id") != mapping.instrument_id:
                    raise DomainValidationError(
                        "mapping supersedes_fact_id must belong to the same instrument identity"
                    )
                latest_source = getattr(latest, "source", None)
                if (
                    not isinstance(latest_source, str)
                    or latest_source.strip().casefold()
                    != mapping.source.strip().casefold()
                ):
                    raise DomainValidationError(
                        "mapping revisions must stay in the same source namespace"
                    )
                latest_known_at = _stored_aware(
                    getattr(latest, "known_at"), "known_at"
                )
                if not allow_historical and mapping.known_at <= latest_known_at:
                    raise DomainValidationError(
                        "ordinary mapping appends require known_at to be strictly "
                        "later than the latest fact; use "
                        "append_historical_mapping or "
                        "append_reconstructed_mapping for an explicitly "
                        "reconstructed chain"
                    )
            elif mapping.fact_version != 1 or mapping.supersedes_fact_id is not None:
                raise DomainValidationError(
                    "the first mapping fact in a logical chain must have version 1 and no predecessor"
                )
        row = InstrumentCodeMappingRecord(
            id=mapping.fact_id or uuid4(),
            instrument_id=mapping.instrument_id,
            source=mapping.source,
            source_code=mapping.source_code,
            trading_code=mapping.trading_code,
            valid_from=mapping.valid_from,
            valid_to=mapping.valid_to,
            fact_version=mapping.fact_version,
            logical_fact_key=mapping.logical_fact_key,
            supersedes_fact_id=mapping.supersedes_fact_id,
            effective_range=_date_range_value(
                mapping.valid_from, mapping.valid_to, session=self.session
            ),
            knowledge_range=_knowledge_range_value(mapping.known_at, session=self.session),
            source_revision=mapping.source_revision,
            mapping_source=mapping.mapping_source,
            evidence=mapping.evidence,
            known_at=mapping.known_at,
            observed_at=mapping.observed_at,
        )
        self.session.add(row)
        return row.id

    append_mapping = add_mapping


def _mapping_detail_value(value: object) -> object:
    """Convert stored-row diagnostics to JSON-safe stable values."""

    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _mapping_detail_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mapping_detail_value(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        return _mapping_detail_value(enum_value)
    return repr(value)


def _mapping_evidence_error_details(
    row: object,
    *,
    instrument_id: object = None,
    source: object = None,
    session_date: object = None,
    data_cutoff: object = None,
    actual: object = None,
) -> dict[str, object]:
    """Build JSON-safe diagnostics for a corrupted stored mapping row."""

    resolved_instrument = (
        instrument_id
        if instrument_id is not None
        else getattr(row, "instrument_id", None)
    )
    resolved_source = (
        source if source is not None else getattr(row, "source", None)
    )
    resolved_session = (
        session_date
        if session_date is not None
        else getattr(row, "valid_from", None)
    )
    evidence = getattr(row, "evidence", None) if actual is None else actual
    fact_version = getattr(row, "fact_version", None)
    return {
        "instrument_id": _mapping_detail_value(resolved_instrument),
        "source": _mapping_detail_value(resolved_source),
        "source_code": _mapping_detail_value(getattr(row, "source_code", None)),
        "session_date": _mapping_detail_value(resolved_session),
        "expected": "non-blank evidence",
        "actual": _mapping_detail_value(evidence),
        "data_cutoff": _mapping_detail_value(data_cutoff),
        "fact_version": _mapping_detail_value(fact_version),
    }


def _to_domain(
    row: InstrumentCodeMappingRecord,
    *,
    instrument_id: UUID | None = None,
    source: str | None = None,
    session_date: date | None = None,
    data_cutoff: datetime | None = None,
) -> InstrumentCodeMapping:
    """Project one ORM row into its immutable domain counterpart.

    Validation runs again here so corrupted or hand-edited rows fail loudly
    at query time instead of leaking invalid facts into callers.
    """

    evidence = getattr(row, "evidence", None)
    if not isinstance(evidence, str) or not evidence.strip():
        raise IdentityMappingEvidenceMissingError(
            "stored instrument code mapping carries no evidence",
            details=_mapping_evidence_error_details(
                row,
                instrument_id=instrument_id,
                source=source,
                session_date=session_date,
                data_cutoff=data_cutoff,
                actual=evidence,
            ),
        )
    try:
        return InstrumentCodeMapping(
            fact_id=getattr(row, "id", None),
            instrument_id=row.instrument_id,
            source=row.source,
            source_code=row.source_code,
            trading_code=row.trading_code,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            source_revision=row.source_revision,
            mapping_source=row.mapping_source,
            evidence=row.evidence,
            known_at=_stored_aware(row.known_at, "known_at"),
            observed_at=_stored_aware(row.observed_at, "observed_at"),
            fact_version=getattr(row, "fact_version", 1) or 1,
            logical_fact_key=getattr(row, "logical_fact_key", None),
            supersedes_fact_id=getattr(row, "supersedes_fact_id", None),
        )
    except DomainValidationError as exc:
        if "evidence" in str(exc).lower():
            raise IdentityMappingEvidenceMissingError(
                "stored instrument code mapping carries no evidence",
                details=_mapping_evidence_error_details(
                    row,
                    instrument_id=instrument_id,
                    source=source,
                    session_date=session_date,
                    data_cutoff=data_cutoff,
                    actual=evidence,
                ),
            ) from exc
        raise DomainValidationError(
            f"stored instrument code mapping {getattr(row, 'id', None)} violates the domain contract: {exc}"
        ) from exc


def _stored_aware(value: datetime, field_name: str) -> datetime:
    """Restore UTC marker for SQLite test storage; PostgreSQL is already aware."""

    if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
        return value.replace(tzinfo=UTC)
    return _aware_datetime(value, field_name)


def _ensure_instrument_exists(session: Session, instrument_id: UUID) -> None:
    """Reject a mapping that does not reference a stable identity row."""

    getter = getattr(session, "get", None)
    if not callable(getter):
        return
    try:
        row = getter(Instrument, instrument_id)
    except OperationalError as exc:  # pragma: no cover - only incomplete test schemas
        # SQLite allows a fact table to be created in isolation.  Production
        # deployments always have the referenced identity table, so only this
        # known fixture limitation is ignored.
        if "no such table" in str(exc).lower():
            return
        raise
    if row is None:
        raise DomainValidationError(
            f"instrument identity {instrument_id} does not exist"
        )


def _date_range_value(
    start: date, end: date | None, *, session: Session | None = None
) -> object:
    """Build a half-open date range for PostgreSQL or a portable test value."""

    # SQLAlchemy's PostgreSQL Range is the native value expected by the
    # DATERANGE bind processor.  SQLite unit tests use the model's TEXT DDL;
    # the string representation remains deterministic and auditable there.
    dialect_name = getattr(getattr(getattr(session, "bind", None), "dialect", None), "name", None)
    if dialect_name == "sqlite":
        return f"[{start.isoformat()},{end.isoformat() if end is not None else ''})"
    return Range(start, end, bounds="[)")


def _knowledge_range_value(
    start: datetime,
    end: datetime | None = None,
    *,
    session: Session | None = None,
) -> object:
    """Build the open-ended knowledge range beginning at ``known_at``.

    Fact rows are immutable, so their knowledge interval is represented as
    ``[known_at, infinity)``.  The fact-level exclusion constraints pair this
    range with ``logical_fact_key <>``: competing logical chains conflict at
    every later knowledge instant, while revisions in one chain remain legal.
    Resolution heads pass the next revision's ``known_at`` as ``end`` to get
    the finite ``[known_at, next_known_at)`` interval required by the index.
    """

    dialect_name = getattr(getattr(getattr(session, "bind", None), "dialect", None), "name", None)
    if dialect_name == "sqlite":
        return f"[{start.isoformat()},{end.isoformat() if end is not None else ''})"
    return Range(start, end, bounds="[)")


def __getattr__(name: str):
    """Lazily expose identity/display repositories from the legacy module.

    Older callers import all instrument repositories from this module.  A
    lazy bridge preserves that surface without introducing an import cycle
    while the task-10 fact repositories live in their focused module.
    """

    exported = {
        "InstrumentDisplayFactRepository",
        "InstrumentIdentityFactRepository",
        "InstrumentIdentityRepository",
        "InstrumentIdentityService",
        "MappingResolutionRepository",
    }
    if name in exported:
        from app.instruments import identity_repository

        return getattr(identity_repository, name)
    raise AttributeError(name)
