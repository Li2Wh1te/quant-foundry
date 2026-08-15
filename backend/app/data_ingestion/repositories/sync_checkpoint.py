"""Generic persistence for incremental synchronization checkpoints."""

from typing import Any

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.data_ingestion.models.sync_checkpoint import DataSyncCheckpoint
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState


class SyncCheckpointConflictError(Exception):
    """Raised when another execution advanced the same checkpoint first."""


class DataSyncCheckpointRepository:
    """Read and advance generic checkpoints without managing transactions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, sync_key: str, scope_key: str) -> DataSyncCheckpointState | None:
        """Return the current checkpoint snapshot for one synchronization scope."""
        checkpoint = self.session.get(
            DataSyncCheckpoint,
            {"sync_key": sync_key, "scope_key": scope_key},
        )
        return self._to_state(checkpoint) if checkpoint is not None else None

    def advance(
        self,
        *,
        sync_key: str,
        scope_key: str,
        cursor: dict[str, Any],
        expected_version: int | None,
        cursor_version: int = 1,
    ) -> DataSyncCheckpointState:
        """Atomically create or advance a checkpoint with optimistic locking."""
        table = DataSyncCheckpoint.__table__
        if expected_version is None:
            statement = (
                insert(table)
                .values(
                    sync_key=sync_key,
                    scope_key=scope_key,
                    cursor=cursor,
                    cursor_version=cursor_version,
                    version=1,
                )
                .on_conflict_do_nothing()
                .returning(
                    table.c.sync_key,
                    table.c.scope_key,
                    table.c.cursor,
                    table.c.cursor_version,
                    table.c.version,
                )
            )
        else:
            statement = (
                update(table)
                .where(
                    table.c.sync_key == sync_key,
                    table.c.scope_key == scope_key,
                    table.c.version == expected_version,
                )
                .values(
                    cursor=cursor,
                    cursor_version=cursor_version,
                    version=table.c.version + 1,
                    updated_at=func.now(),
                )
                .returning(
                    table.c.sync_key,
                    table.c.scope_key,
                    table.c.cursor,
                    table.c.cursor_version,
                    table.c.version,
                )
            )
        row = self.session.execute(statement).mappings().one_or_none()
        if row is None:
            raise SyncCheckpointConflictError(
                f"checkpoint changed concurrently: {sync_key}/{scope_key}"
            )
        return DataSyncCheckpointState(
            sync_key=row["sync_key"],
            scope_key=row["scope_key"],
            cursor=dict(row["cursor"]),
            cursor_version=row["cursor_version"],
            version=row["version"],
        )

    @staticmethod
    def _to_state(checkpoint: DataSyncCheckpoint) -> DataSyncCheckpointState:
        return DataSyncCheckpointState(
            sync_key=checkpoint.sync_key,
            scope_key=checkpoint.scope_key,
            cursor=dict(checkpoint.cursor),
            cursor_version=checkpoint.cursor_version,
            version=checkpoint.version,
        )
