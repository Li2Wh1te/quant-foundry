import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from pydantic import BaseModel

from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry
from app.scheduling.runtime import SchedulerRuntime
from app.core.config import Settings
from app.data_ingestion.scheduler_tasks.corporate_action import (
    CorporateActionSyncParameters,
    _handler as corporate_action_handler,
)
from app.data_ingestion.scheduler_tasks.trading_status import (
    TradingStatusSyncParameters,
    _handler as trading_status_handler,
)


class _CheckpointRepo:
    def __init__(self, state=None):
        self.state = state
        self.calls = []

    def get(self, sync_key, scope_key):
        return self.state

    def advance(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(cursor=kwargs["cursor"])


class _TradingSession:
    def __init__(self):
        self.rows = {}
        self.added = []

    def get(self, model, key):
        return self.rows.get(key)

    def add(self, row):
        self.added.append(row)


class _EmptyParameters(BaseModel):
    pass


class IngestionSchedulerTaskTests(unittest.TestCase):
    def test_trading_status_handler_persists_facts_and_advances_checkpoint(self):
        client = MagicMock()
        client.suspend_d.return_value = [
            {"ts_code": "510300.SH", "trade_date": "2026-08-31", "suspend_type": "S"}
        ]
        session = _TradingSession()
        checkpoint_repo = _CheckpointRepo()
        context = TaskContext(
            task_id=uuid4(),
            run_id=uuid4(),
            task_type="data.sync_trading_status",
            client=client,
            session=session,
            checkpoint_repo=checkpoint_repo,
            sync_key="tushare.trading_status",
        )

        result = trading_status_handler(
            context,
            TradingStatusSyncParameters(
                start_date=date(2026, 8, 31), end_date=date(2026, 8, 31)
            ),
        )

        client.suspend_d.assert_called_once_with(
            start_date="2026-08-31", end_date="2026-08-31"
        )
        self.assertEqual(len(session.added), 1)
        self.assertEqual(result["changed_count"], 1)
        self.assertTrue(result["checkpoint_advanced"])
        self.assertEqual(
            checkpoint_repo.calls[0]["cursor"],
            {"synced_through_date": "2026-08-31"},
        )

    @patch("app.data_ingestion.scheduler_tasks.corporate_action.sync_fund_div")
    def test_corporate_action_handler_passes_production_resources(self, sync_mock):
        instrument_id = uuid4()
        session = MagicMock()
        session.execute.return_value.all.return_value = [("510300.SH", instrument_id)]
        checkpoint_repo = _CheckpointRepo()
        client = MagicMock()
        sync_mock.return_value = {
            "fetched": 1,
            "changed": 1,
            "unchanged": 0,
            "failed": 0,
            "checkpoint_advanced": True,
            "checkpoint_after": {"synced_through_date": "2026-08-31"},
        }
        context = TaskContext(
            task_id=uuid4(),
            run_id=uuid4(),
            task_type="data.sync_etf_cash_dividend_incremental",
            client=client,
            session=session,
            checkpoint_repo=checkpoint_repo,
            sync_key="tushare.fund_div.incremental",
        )

        result = corporate_action_handler(
            context,
            CorporateActionSyncParameters(
                start_date=date(2026, 8, 1), end_date=date(2026, 8, 31)
            ),
        )

        kwargs = sync_mock.call_args.kwargs
        self.assertIs(sync_mock.call_args.args[0], client)
        self.assertIs(kwargs["session"], session)
        self.assertEqual(kwargs["instrument_map"], {"510300.SH": instrument_id})
        self.assertEqual(kwargs["start_date"], "2026-08-01")
        self.assertEqual(kwargs["end_date"], "2026-08-31")
        self.assertIs(kwargs["checkpoint_repo"], checkpoint_repo)
        self.assertEqual(result["fetched_count"], 1)
        self.assertTrue(result["checkpoint_advanced"])
    def test_runtime_binds_client_transaction_and_checkpoint_repository(self):
        task_id, run_id = uuid4(), uuid4()
        task_type = "data.sync_trading_status"
        run = SimpleNamespace(
            status="running",
            task_id=task_id,
            task_type=task_type,
            parameters={},
            parameter_version=1,
        )
        task = SimpleNamespace(state="active")
        captured = {}

        def handler(context, parameters):
            captured["context"] = context
            return {"status": "completed"}

        registry = TaskRegistry()
        registry.register(
            TaskDefinition(
                key=task_type,
                name="交易状态",
                english_name="Trading status",
                parameters_model=_EmptyParameters,
                handler=handler,
            )
        )
        settings = Settings(
            api_token="a" * 64,
            database_password="test-secret",
            cursor_signing_key="c" * 32,
            scheduler_enabled=False,
            _env_file=None,
        )
        lookup_session = MagicMock()
        lookup_session.__enter__.return_value = lookup_session
        ingestion_session = MagicMock()
        finish_session = MagicMock()
        finish_session.__enter__.return_value = finish_session

        with (
            patch("app.scheduling.runtime.Session", side_effect=[lookup_session, ingestion_session, finish_session]),
            patch("app.scheduling.runtime.get_engine", return_value=object()),
            patch("app.scheduling.runtime.SchedulerRepository") as repository_class,
            patch("app.scheduling.runtime.TushareClient", return_value=object()) as client_class,
            patch("app.scheduling.runtime.DataSyncCheckpointRepository") as checkpoint_class,
        ):
            repository_class.return_value.get_run.return_value = run
            repository_class.return_value.get_task.return_value = task
            runtime = SchedulerRuntime(settings, registry)
            runtime._execute_run(run_id)

        context = captured["context"]
        self.assertEqual(context.task_id, task_id)
        self.assertIsNotNone(context.client)
        self.assertIs(context.session, ingestion_session)
        self.assertIs(context.checkpoint_repo, checkpoint_class.return_value)
        self.assertEqual(context.sync_key, "tushare.trading_status")
        client_class.assert_called_once_with(settings)
        ingestion_session.commit.assert_called_once_with()
        ingestion_session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
