import unittest
from unittest.mock import Mock

from sqlalchemy.dialects import postgresql

from app.data_ingestion.repositories.sync_checkpoint import (
    DataSyncCheckpointRepository,
    SyncCheckpointConflictError,
)


class DataSyncCheckpointRepositoryTestCase(unittest.TestCase):
    def test_advances_an_existing_checkpoint_with_optimistic_locking(self) -> None:
        session = Mock()
        session.execute.return_value.mappings.return_value.one_or_none.return_value = {
            "sync_key": "tushare.trade_calendar",
            "scope_key": "exchange=SSE",
            "cursor": {"synced_through_date": "2026-08-15"},
            "cursor_version": 1,
            "version": 4,
        }

        checkpoint = DataSyncCheckpointRepository(session).advance(
            sync_key="tushare.trade_calendar",
            scope_key="exchange=SSE",
            cursor={"synced_through_date": "2026-08-15"},
            expected_version=3,
        )

        self.assertEqual(checkpoint.version, 4)
        statement = session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("AND data_sync_checkpoints.version =", sql)

    def test_rejects_a_stale_checkpoint_advance(self) -> None:
        session = Mock()
        session.execute.return_value.mappings.return_value.one_or_none.return_value = None

        with self.assertRaises(SyncCheckpointConflictError):
            DataSyncCheckpointRepository(session).advance(
                sync_key="tushare.trade_calendar",
                scope_key="exchange=SSE",
                cursor={"synced_through_date": "2026-08-15"},
                expected_version=3,
            )
