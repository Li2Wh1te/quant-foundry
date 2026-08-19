"""Database access helpers for private strategy storage.

The repository never updates or deletes published revisions.  Publication is an
append-only operation owned by ``StrategyStorageService`` so callers cannot
accidentally bypass the strategy-version lifecycle.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.strategies.models import Strategy, StrategyDraft, StrategyRevision


class StrategyRepository:
    """Load strategy records while leaving transaction ownership to callers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_strategy(
        self, strategy_id: UUID, *, for_update: bool = False
    ) -> Strategy | None:
        """Return one strategy, optionally locking its lifecycle row."""
        statement = select(Strategy).where(Strategy.id == strategy_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_strategies(
        self, *, include_archived: bool, limit: int, offset: int
    ) -> list[Strategy]:
        """Return a stable, newest-first page of private strategy identities."""
        statement = select(Strategy)
        if not include_archived:
            statement = statement.where(Strategy.state != "archived")
        statement = (
            statement.order_by(Strategy.updated_at.desc(), Strategy.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def get_draft(
        self, strategy_id: UUID, *, for_update: bool = False
    ) -> StrategyDraft | None:
        """Return the strategy's sole mutable draft, optionally row-locked."""
        statement = select(StrategyDraft).where(
            StrategyDraft.strategy_id == strategy_id
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_revision(self, revision_id: UUID) -> StrategyRevision | None:
        """Return one immutable revision by its stable UUID."""
        return self.session.get(StrategyRevision, revision_id)

    def get_revision_by_number(
        self, strategy_id: UUID, revision_number: int
    ) -> StrategyRevision | None:
        """Return one revision only when it belongs to the requested strategy."""
        return self.session.scalar(
            select(StrategyRevision).where(
                StrategyRevision.strategy_id == strategy_id,
                StrategyRevision.revision_number == revision_number,
            )
        )

    def list_revisions(self, strategy_id: UUID) -> list[StrategyRevision]:
        """Return newest published revisions first for a strategy history view."""
        statement = (
            select(StrategyRevision)
            .where(StrategyRevision.strategy_id == strategy_id)
            .order_by(StrategyRevision.revision_number.desc())
        )
        return list(self.session.scalars(statement))

    def next_revision_number(self, strategy_id: UUID) -> int:
        """Calculate the next sequence number while the owning strategy is locked.

        ``StrategyStorageService.publish_revision`` locks the strategy row before
        calling this method.  That lock serializes all application publication
        attempts for one strategy, while the database unique constraint remains
        the final defense against any out-of-band writer.
        """
        latest = self.session.scalar(
            select(func.max(StrategyRevision.revision_number)).where(
                StrategyRevision.strategy_id == strategy_id
            )
        )
        return (latest or 0) + 1
