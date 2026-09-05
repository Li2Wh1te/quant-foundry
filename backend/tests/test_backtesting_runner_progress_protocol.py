"""Worker wiring and runner progress protocol tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from app.backtesting.runner_integrity import compute_result_integrity
from app.backtesting.runner_progress import (
    DatabaseProgressPersistence,
    FrozenTimelineProgress,
    ProgressReporter,
)
from app.backtesting.runner_worker import WorkerExecutionResult, run_worker
from app.backtesting.runner_process import WorkerHandshake
from tests.backtest_runtime_fixture import (
    DictMarketData,
    INSTRUMENT_ID,
    ScriptedStrategy,
    build_axis,
    build_runner,
)


def _empty_integrity(config_hash: str):
    return compute_result_integrity(
        {name: [] for name in (
            "backtest_steps",
            "backtest_decisions",
            "backtest_orders",
            "backtest_order_updates",
            "backtest_fills",
            "backtest_positions",
            "backtest_equity_curve",
            "backtest_metrics",
        )},
        config_hash=config_hash,
    )


def test_worker_stops_reporter_after_result_commit_before_marker() -> None:
    run_id = uuid4()
    launch_id = uuid4()
    config_hash = "a" * 64
    integrity = _empty_integrity(config_hash)
    calls: list[str] = []

    class Reporter:
        def start(self):
            calls.append("reporter_start")

        def stop(self, *, flush: bool):
            calls.append(f"reporter_stop:{flush}")

    with patch(
        "app.backtesting.runner_worker.build_handshake",
        return_value=WorkerHandshake(
            str(run_id), str(launch_id), 1, "start", 1,
            "runner_handshake@1", "worker", datetime.now(UTC),
        ),
    ):
        code = run_worker(
            run_id,
            launch_id,
            load_binding=lambda key: {
                "run_id": key,
                "launch_id": launch_id,
                "status": "running",
                "config_hash": config_hash,
            },
            execute=lambda _binding, **_kwargs: WorkerExecutionResult(
                "succeeded", integrity
            ),
            write_handshake=lambda _value: calls.append("handshake"),
            persist_results=lambda _result: calls.append("result_commit"),
            write_marker=lambda _marker: calls.append("marker"),
            memory_limit_mib=None,
            progress_reporter=Reporter(),
        )

    assert code == 0
    assert calls == [
        "handshake",
        "reporter_start",
        "result_commit",
        "reporter_stop:True",
        "marker",
    ]


def test_worker_failure_persists_locatable_evidence_without_writing_terminal_status() -> None:
    run_id = uuid4()
    launch_id = uuid4()
    config_hash = "a" * 64
    failures = []

    def execute(_binding, **_kwargs):
        raise ValueError("token=secret-value")

    with patch(
        "app.backtesting.runner_worker.build_handshake",
        return_value=WorkerHandshake(
            str(run_id), str(launch_id), 1, "start", 1,
            "runner_handshake@1", "worker", datetime.now(UTC),
        ),
    ):
        code = run_worker(
            run_id,
            launch_id,
            load_binding=lambda key: {
                "run_id": key,
                "launch_id": launch_id,
                "status": "running",
                "config_hash": config_hash,
            },
            execute=execute,
            write_handshake=lambda _value: None,
            persist_results=lambda _result: True,
            write_marker=lambda _marker: (_ for _ in ()).throw(
                AssertionError("a raised runtime must not write a marker")
            ),
            write_failure_evidence=lambda evidence: failures.append(evidence) or True,
            memory_limit_mib=None,
        )

    assert code == 10
    assert len(failures) == 1
    assert failures[0]["error_type"] == "ValueError"
    assert failures[0]["desensitized"] is True
    assert "secret-value" not in failures[0]["technical_detail"]


def test_worker_marker_write_failure_does_not_report_success_or_write_status() -> None:
    """A failed marker CAS must leave final-state ownership with Supervisor."""

    run_id = uuid4()
    launch_id = uuid4()
    config_hash = "a" * 64
    integrity = _empty_integrity(config_hash)
    status_writes: list[str] = []

    with patch(
        "app.backtesting.runner_worker.build_handshake",
        return_value=WorkerHandshake(
            str(run_id), str(launch_id), 1, "start", 1,
            "runner_handshake@1", "worker", datetime.now(UTC),
        ),
    ):
        code = run_worker(
            run_id,
            launch_id,
            load_binding=lambda key: {
                "run_id": key,
                "launch_id": launch_id,
                "status": "running",
                "config_hash": config_hash,
            },
            execute=lambda _binding, **_kwargs: WorkerExecutionResult(
                "succeeded", integrity
            ),
            write_handshake=lambda _value: None,
            persist_results=lambda _result: True,
            write_marker=lambda _marker: False,
            memory_limit_mib=None,
        )

    assert code == 10
    assert status_writes == []


def test_reporter_requires_successful_callback_for_valid_heartbeat() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    reporter = ProgressReporter(
        uuid4(),
        launch_id=uuid4(),
        persist_progress=lambda _snapshot: False,
        persist_heartbeat=lambda _snapshot: False,
    )

    first = reporter.report(
        FrozenTimelineProgress(2, 1, 0, date(2026, 1, 1)),
        now=base,
    )
    assert not first.progress_persisted
    assert not first.heartbeat_persisted
    assert reporter.last_heartbeat_at is None


def test_database_progress_persistence_rejects_stale_launch() -> None:
    run_id = uuid4()
    stale_launch = uuid4()
    sessions = []

    class Session:
        def __init__(self):
            self.rolled_back = False
            self.closed = False

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            raise AssertionError("stale launch must not commit")

        def close(self):
            self.closed = True

    class Repository:
        def __init__(self, _session):
            pass

        def record_progress(self, *_args, **_kwargs):
            return False

    def factory():
        session = Session()
        sessions.append(session)
        return session

    adapter = DatabaseProgressPersistence(factory, repository_factory=Repository)
    snapshot = ProgressReporter(
        run_id,
        launch_id=stale_launch,
        persist_progress=adapter.persist_progress,
    ).report(
        FrozenTimelineProgress(1, 0, 0, date(2026, 1, 1)),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert not snapshot.progress_persisted
    assert sessions[0].rolled_back
    assert sessions[0].closed


def test_runner_emits_phase_and_completed_step_progress() -> None:
    axis = build_axis([date(2026, 8, 3), date(2026, 8, 4)])
    events: list[tuple[str, str, int, int, int]] = []

    class Sink:
        def phase_started(self, trading_date, current_step, completed_steps, total_steps):
            events.append(("phase_started", str(trading_date), current_step, completed_steps, total_steps))

        def step_completed(self, trading_date, current_step, completed_steps, total_steps):
            events.append(("step_completed", str(trading_date), current_step, completed_steps, total_steps))

    runner = build_runner(
        run_id="progress-sink-run",
        axis=axis,
        market_data=DictMarketData(
            {
                date(2026, 8, 3): {INSTRUMENT_ID: ("99", "100")},
                date(2026, 8, 4): {INSTRUMENT_ID: ("100", "101")},
            }
        ),
        strategy_view=type("View", (), {
            "bars": lambda self, *_args, **_kwargs: (),
        })(),
        strategy=ScriptedStrategy({}),
        progress_sink=Sink(),
    )

    runner.run()

    assert events
    assert events[0][0] == "phase_started"
    assert events[0][2:] == (0, 0, 2)
    completed = [event for event in events if event[0] == "step_completed"]
    assert [event[2:] for event in completed] == [(0, 1, 2), (1, 2, 2)]
