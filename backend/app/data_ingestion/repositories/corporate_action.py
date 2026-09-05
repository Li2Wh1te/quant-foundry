"""Read-only PIT repository for normalized corporate-action facts."""
from datetime import UTC, date, datetime
from uuid import UUID
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import Session
from app.backtesting.data.errors import ProviderContractViolationError
from app.data_ingestion.models.corporate_action import CorporateActionFact, CorporateActionCoverageFact

class CorporateActionRepository:
    """Select one active fact version per logical key at a PIT cutoff."""
    def __init__(self, session: Session): self.session = session

    def list_facts(
        self,
        instrument_ids,
        start_date: date,
        end_date: date,
        *,
        cutoff: datetime | None = None,
        knowledge_as_of: datetime | None = None,
        action_types=(),
    ):
        ids = tuple(instrument_ids)
        overlap = or_(
            and_(CorporateActionFact.record_date.is_not(None), CorporateActionFact.record_date.between(start_date, end_date)),
            and_(CorporateActionFact.ex_date.is_not(None), CorporateActionFact.ex_date.between(start_date, end_date)),
            and_(CorporateActionFact.cash_effective_date.is_not(None), CorporateActionFact.cash_effective_date.between(start_date, end_date)),
            and_(CorporateActionFact.record_date <= start_date, CorporateActionFact.cash_effective_date >= end_date),
        )
        stmt = select(CorporateActionFact).where(CorporateActionFact.instrument_id.in_(ids), overlap)
        if action_types: stmt = stmt.where(CorporateActionFact.action_type.in_(tuple(action_types)))
        if cutoff is not None:
            # ``observed_at`` is the explicit platform acceptance boundary;
            # ``created_at`` remains a compatibility fallback for legacy rows.
            stmt = stmt.where(
                or_(
                    and_(CorporateActionFact.observed_at.is_not(None), CorporateActionFact.observed_at <= cutoff),
                    and_(CorporateActionFact.observed_at.is_(None), CorporateActionFact.created_at <= cutoff),
                ),
                or_(
                    CorporateActionFact.known_at.is_(None),
                    CorporateActionFact.known_at <= cutoff,
                ),
            )
        if knowledge_as_of is not None:
            stmt = stmt.where(
                CorporateActionFact.known_at.is_not(None),
                CorporateActionFact.known_at <= knowledge_as_of,
            )
        rows = self.session.scalars(stmt.order_by(CorporateActionFact.logical_fact_key, CorporateActionFact.fact_version.desc())).all()
        grouped = {}
        for row in rows:
            grouped.setdefault(row.logical_fact_key, []).append(row)
        out=[]
        for logical_key, versions in grouped.items():
            versions.sort(key=lambda item: item.fact_version, reverse=True)
            winner = versions[0]
            if len(versions) > 1:
                # A revision is valid only when it explicitly supersedes the
                # prior event.  Two visible versions without that linkage are
                # independent active facts and cannot be resolved by order.
                superseded_ids = {item.event_id for item in versions[1:]}
                if winner.supersedes_fact_id not in superseded_ids:
                    raise ProviderContractViolationError(
                        "multiple active corporate-action fact versions",
                        details={"logical_fact_key": logical_key, "fact_versions": [item.fact_version for item in versions]},
                    )
            out.append(winner)
        out.sort(key=lambda item: (item.logical_fact_key, -item.fact_version))
        return tuple(out)

    def coverage(
        self,
        instrument_ids,
        start_date: date,
        end_date: date,
        *,
        cutoff: datetime | None = None,
        knowledge_as_of: datetime | None = None,
        action_types=(),
    ):
        """Return complete-window proofs visible at the requested PIT cutoff."""

        statement = select(CorporateActionCoverageFact).where(
            CorporateActionCoverageFact.instrument_id.in_(tuple(instrument_ids)),
            CorporateActionCoverageFact.start_date <= start_date,
            CorporateActionCoverageFact.end_date >= end_date,
        )
        if action_types:
            statement = statement.where(
                CorporateActionCoverageFact.action_type.in_(tuple(action_types))
            )
        if cutoff is not None:
            statement = statement.where(
                or_(
                    and_(CorporateActionCoverageFact.observed_at.is_not(None), CorporateActionCoverageFact.observed_at <= cutoff),
                    and_(CorporateActionCoverageFact.observed_at.is_(None), CorporateActionCoverageFact.computed_at <= cutoff),
                ),
                or_(
                    CorporateActionCoverageFact.known_at.is_(None),
                    CorporateActionCoverageFact.known_at <= cutoff,
                ),
            )
        if knowledge_as_of is not None:
            statement = statement.where(
                CorporateActionCoverageFact.known_at.is_not(None),
                CorporateActionCoverageFact.known_at <= knowledge_as_of,
            )
        rows = self.session.scalars(
            statement.order_by(
                CorporateActionCoverageFact.instrument_id,
                CorporateActionCoverageFact.action_type,
                CorporateActionCoverageFact.observed_at.desc(),
            )
        ).all()

        def aware(value):
            if not isinstance(value, datetime):
                return datetime.min.replace(tzinfo=UTC)
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

        # Coverage is an assertion that can itself be revised.  Keep only the
        # latest visible assertion per instrument/action family so an older
        # ``complete`` proof cannot survive a newer incomplete result.
        selected = {}
        for row in rows:
            key = (row.instrument_id, row.action_type)
            rank = (
                aware(getattr(row, "known_at", None)),
                aware(getattr(row, "observed_at", None) or row.computed_at),
            )
            if key not in selected or rank > selected[key][0]:
                selected[key] = (rank, row)
        return tuple(
            row
            for _, row in sorted(
                selected.values(),
                key=lambda item: (str(item[1].instrument_id), item[1].action_type or ""),
            )
        )
