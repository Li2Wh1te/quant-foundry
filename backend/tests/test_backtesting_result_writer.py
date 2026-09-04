from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.backtesting.result_writer import (
    BacktestResultContext,
    BacktestResultPersistenceService,
    ResultBatch,
)
from app.backtesting.runtime import EquitySample, EventEnvelope


UTC = timezone.utc


def test_runtime_projection_preserves_event_and_point_in_time_valuation() -> None:
    run_id = uuid4()
    event_time = datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    event = EventEnvelope(
        run_id=str(run_id),
        event_sequence=1,
        step_sequence=0,
        phase_sequence=6,
        phase_key="value",
        event_type="portfolio_valued",
        event_time=event_time,
        payload={"equity": Decimal("101.25")},
    )
    sample = EquitySample(
        step_sequence=0,
        session_date=date(2026, 6, 1),
        as_of=event_time,
        equity=Decimal("101.25"),
        valuation_status="complete",
        cash=Decimal("98.25"),
        market_value=Decimal("3.00"),
        period_return=Decimal("0.0125"),
        total_pnl=Decimal("1.25"),
        cumulative_return=Decimal("0.0125"),
        drawdown=Decimal("0"),
        cumulative_fees=Decimal("0.10"),
        time_start=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        time_end=event_time,
        data_cutoff_at=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
    )
    result = SimpleNamespace(
        events=(event,),
        equity_curve=(sample,),
        decisions=(),
        order_outcomes=(),
        order_updates=(),
        components={
            "time_axis": {"key": "trading_day", "version": 1},
            "accounting_policy": {"key": "accounting_policy", "version": 1},
        },
        random_seed=17,
        analysis_status="partial",
        rule_snapshot_hash=None,
        universe_eligibility_summary={},
    )
    writer = BacktestResultPersistenceService(
        object(),
        BacktestResultContext(
            run_id=run_id,
            run_kind="backtest_run",
            profile="formal@1",
            config_hash="a" * 64,
            launch_id=uuid4(),
        ),
    )
    captured = []
    writer.persist_result_batch = lambda batch, *, commit=False: captured.append((batch, commit)) or 1

    assert writer.persist_runtime_result(result, commit=False) == 1
    batch, committed = captured[0]
    assert committed is False
    assert len(batch.events) == 1
    assert batch.events[0].payload["equity"] == "101.25"
    assert batch.steps[0].time_start == sample.time_start
    assert batch.steps[0].time_end == sample.time_end
    assert batch.steps[0].data_cutoff_at == sample.data_cutoff_at
    assert batch.equity_curve[0].cash == Decimal("98.25")
    assert batch.equity_curve[0].market_value == Decimal("3.00")
    assert batch.equity_curve[0].period_return == Decimal("0.0125")
    assert batch.equity_curve[0].cumulative_fees == Decimal("0.10")
    assert batch.component_snapshot["time_axis"]["key"] == "trading_day"
    assert batch.result_summary["random_seed"] == 17


def test_component_snapshot_is_written_to_the_durable_run_summary() -> None:
    root = SimpleNamespace(result_counts={}, result_summary={"schema_version": "result-v1"})
    writer = BacktestResultPersistenceService(
        SimpleNamespace(flush=lambda: None),
        BacktestResultContext(
            run_id=uuid4(),
            run_kind="backtest_run",
            profile="formal@1",
            config_hash="b" * 64,
            launch_id=uuid4(),
        ),
    )
    writer._root = lambda: root

    writer._persist_result_batch(
        ResultBatch(
            component_snapshot={
                "time_axis": {"key": "trading_day", "version": 1},
                "accounting_policy": {"key": "accounting_policy", "version": 1},
            }
        )
    )

    assert root.result_summary["schema_version"] == "result-v1"
    assert root.result_summary["components"]["accounting_policy"]["version"] == 1
