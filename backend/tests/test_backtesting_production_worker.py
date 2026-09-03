from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DataRequest,
    DateRange,
    InstrumentScopeMode,
    MarketScope,
    PriceBasis,
    PreflightStatus,
    QueryBoundary,
    UniverseQueryPolicy,
)
from app.backtesting.production_runtime import deserialize_data_request, serialize_data_request
from app.backtesting.runner_protocol import CATEGORY_TO_EXIT_CODE, ExitCategory
from app.backtesting.runner_worker import WorkerExecutionResult, main


class _Session:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, _model, _run_id):
        return self.row


def test_production_worker_main_uses_wired_callbacks_instead_of_fixed_rejection():
    run_id = uuid4()
    launch_id = uuid4()
    config_hash = "a" * 64
    row = SimpleNamespace(config_hash=config_hash)
    binding = SimpleNamespace(
        run_id=run_id,
        status="starting",
        config_hash=config_hash,
    )
    integrity = {
        "digest": "sha256:" + "b" * 64,
        "counts": {
            "steps": 0,
            "decisions": 0,
            "orders": 0,
            "order_updates": 0,
            "fills": 0,
            "positions": 0,
            "equity_points": 0,
            "metrics": 0,
        },
    }
    calls = []
    callbacks = (
        lambda _run_id: binding,
        lambda _binding, **_kwargs: WorkerExecutionResult("succeeded", integrity),
        lambda _payload: calls.append("handshake") or True,
        lambda _result: calls.append("results") or True,
        lambda _payload: calls.append("marker") or True,
        lambda _payload: calls.append("resource") or True,
    )
    settings = SimpleNamespace(
        backtest_memory_limit_mib=None,
        backtest_heartbeat_max_interval_seconds=15,
        backtest_progress_persist_interval_seconds=5,
        backtest_lost_heartbeat_seconds=60,
    )

    with (
        patch("app.core.config.get_settings", return_value=settings),
        patch("app.db.session.get_engine", return_value=object()),
        patch("sqlalchemy.orm.Session", side_effect=lambda _engine: _Session(row)),
        patch("app.backtesting.runner_worker._production_callbacks", return_value=callbacks),
    ):
        result = main(["--run-id", str(run_id), "--launch-id", str(launch_id)])

    assert result == CATEGORY_TO_EXIT_CODE[ExitCategory.SUCCEEDED.value]
    assert calls == ["resource", "handshake", "results", "marker"]


def test_frozen_data_request_round_trips_through_the_worker_snapshot():
    request = DataRequest(
        provider_key="etf_ingestion",
        requested_window=DateRange(date(2026, 1, 1), date(2026, 1, 2)),
        frequency="1d",
        rule_package=ContractRef("china_listed_etf_rules", 1),
        market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        universe_query_policy=UniverseQueryPolicy(),
        instrument_scope_mode=InstrumentScopeMode.FIXED,
        required_capabilities=(DataCapability.BARS,),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
        consistency_token_contract=ContractRef("sql_revision_vector", 1),
        query_boundary=QueryBoundary(datetime(2026, 1, 3, tzinfo=timezone.utc)),
        resolved_calendar_ids=("SSE",),
        resolved_timezone="Asia/Shanghai",
        admission_calendar_session_signature="b" * 64,
        admission_preflight_status=PreflightStatus.READY,
        admission_preflight_hash="c" * 64,
        static_instrument_ids=(uuid4(),),
        resolved_rule_snapshot_hash="d" * 64,
    )
    assert deserialize_data_request(serialize_data_request(request)) == request
