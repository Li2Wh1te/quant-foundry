"""PostgreSQL persistence for stable instrument identities and PIT mappings."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.domain import DomainValidationError, _aware_datetime
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

        _aware_datetime(data_cutoff, "data_cutoff")
        if not isinstance(instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        if not isinstance(start_date, date) or isinstance(start_date, datetime):
            raise DomainValidationError("start_date must be a calendar date")
        if not isinstance(end_date, date) or isinstance(end_date, datetime):
            raise DomainValidationError("end_date must be a calendar date")
        if start_date > end_date:
            raise DomainValidationError("start_date cannot be after end_date")

        rows = self.session.execute(
            select(InstrumentCodeMappingRecord)
            .where(
                InstrumentCodeMappingRecord.instrument_id == instrument_id,
                InstrumentCodeMappingRecord.source == source,
                # Half-open window intersection: the mapping starts no later
                # than the last requested day and (when closed) ends after
                # the first requested day.
                InstrumentCodeMappingRecord.valid_from <= end_date,
                (
                    InstrumentCodeMappingRecord.valid_to.is_(None)
                    | (InstrumentCodeMappingRecord.valid_to > start_date)
                ),
                # Knowledge-time visibility: facts learned after the cutoff
                # do not exist for this query.
                InstrumentCodeMappingRecord.known_at <= data_cutoff,
            )
            .order_by(InstrumentCodeMappingRecord.valid_from)
        ).scalars().all()
        return order_mapping_segments(
            [_to_domain(row) for row in rows],
            start_date=start_date,
            end_date=end_date,
        )

    def add_mapping(self, mapping: InstrumentCodeMapping) -> UUID:
        """Append one immutable evidenced mapping row and return its id.

        The mapping is validated domain-side before it reaches SQL.  No
        update path exists on purpose: corrections are new rows so history
        stays reproducible for any later ``data_cutoff``.
        """

        row = InstrumentCodeMappingRecord(
            id=uuid4(),
            instrument_id=mapping.instrument_id,
            source=mapping.source,
            source_code=mapping.source_code,
            trading_code=mapping.trading_code,
            valid_from=mapping.valid_from,
            valid_to=mapping.valid_to,
            source_revision=mapping.source_revision,
            mapping_source=mapping.mapping_source,
            evidence=mapping.evidence,
            known_at=mapping.known_at,
            observed_at=mapping.observed_at,
        )
        self.session.add(row)
        return row.id


def _to_domain(row: InstrumentCodeMappingRecord) -> InstrumentCodeMapping:
    """Project one ORM row into its immutable domain counterpart.

    Validation runs again here so corrupted or hand-edited rows fail loudly
    at query time instead of leaking invalid facts into callers.
    """

    try:
        return InstrumentCodeMapping(
            instrument_id=row.instrument_id,
            source=row.source,
            source_code=row.source_code,
            trading_code=row.trading_code,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            source_revision=row.source_revision,
            mapping_source=row.mapping_source,
            evidence=row.evidence,
            known_at=row.known_at,
            observed_at=row.observed_at,
        )
    except DomainValidationError as exc:
        raise DomainValidationError(
            f"stored instrument code mapping {row.id} violates the domain contract: {exc}"
        ) from exc
