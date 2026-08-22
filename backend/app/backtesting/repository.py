"""Persistence helpers for editable backtest account profiles."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backtesting.models import BacktestAccountProfileRecord


class BacktestAccountProfileRepository:
    """Load and persist account profiles while callers own transactions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(
        self,
        profile_id: UUID,
        *,
        for_update: bool = False,
    ) -> BacktestAccountProfileRecord | None:
        """Return one profile, optionally locking its row for an update."""

        statement = select(BacktestAccountProfileRecord).where(
            BacktestAccountProfileRecord.id == profile_id
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list(
        self,
        *,
        status: str | None,
        name_query: str | None,
        limit: int,
        offset: int,
    ) -> list[BacktestAccountProfileRecord]:
        """Return a deterministic page, optionally filtered by account name."""

        statement = select(BacktestAccountProfileRecord)
        if status is not None:
            statement = statement.where(BacktestAccountProfileRecord.status == status)
        if name_query:
            statement = statement.where(
                func.lower(BacktestAccountProfileRecord.name).contains(
                    name_query.strip().lower()
                )
            )
        statement = (
            statement.order_by(
                func.lower(BacktestAccountProfileRecord.name),
                BacktestAccountProfileRecord.id,
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(statement))

    def name_exists(
        self,
        name: str,
        *,
        excluding_id: UUID | None = None,
    ) -> bool:
        """Check case-insensitive uniqueness for a user-facing account name."""

        statement = select(BacktestAccountProfileRecord.id).where(
            func.lower(BacktestAccountProfileRecord.name) == name.strip().lower()
        )
        if excluding_id is not None:
            statement = statement.where(BacktestAccountProfileRecord.id != excluding_id)
        return self.session.scalar(statement) is not None
