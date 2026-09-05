"""Pure terminal evidence reconciliation contracts."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.backtesting.run_supervision_adapter import reconcile_terminal_evidence
from app.backtesting.runner_integrity import compute_result_integrity
from app.backtesting.runner_protocol import COVERED_RESULT_TABLES, build_completion_marker
from app.backtesting.run_repository import DatabaseRunRepository, RunRepository
from app.backtesting.result_writer import (
    BacktestResultContext,
    BacktestResultPersistenceService,
)
from app.backtesting.supervisor_lock import SupervisorLockNotHeld


def _evidence(config_hash: str):
    return compute_result_integrity(
        {table: [] for table in COVERED_RESULT_TABLES}, config_hash=config_hash
    )


def test_reconcile_returns_full_decision_for_each_consistent_category() -> None:
    run_id = uuid4()
    config_hash = "a" * 64
    integrity = _evidence(config_hash)
    for code, category in ((0, "succeeded"), (10, "failed"), (20, "cancelled"), (30, "timed_out")):
        marker = build_completion_marker(
            run_id=run_id,
            declared_category=category,
            digest=integrity.digest,
            result_counts=integrity.counts,
            failure_phase=None if category == "succeeded" else "runtime",
            failure_type=None if category == "succeeded" else "WorkerError",
            config_hash=config_hash,
        )
        decision = reconcile_terminal_evidence(
            run_id,
            code,
            None,
            marker,
            integrity,
            config_hash,
            None,
        )
        assert decision.terminal_status == category
        assert decision.marker_validation.valid
        assert decision.integrity_validation.valid
        assert decision.preserve_evidence


def test_reconcile_is_conservative_for_conflicts_missing_evidence_and_signals() -> None:
    run_id = uuid4()
    config_hash = "b" * 64
    integrity = _evidence(config_hash)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="succeeded",
        digest=integrity.digest,
        result_counts=integrity.counts,
        config_hash=config_hash,
    )
    conflict = reconcile_terminal_evidence(
        run_id, 10, None, marker, integrity, config_hash, None
    )
    missing = reconcile_terminal_evidence(
        run_id, 0, None, None, integrity, config_hash, None
    )
    signalled = reconcile_terminal_evidence(
        run_id, -9, 9, marker, integrity, config_hash, {"forced": True}
    )
    assert conflict.terminal_status == "indeterminate"
    assert missing.terminal_status == "indeterminate"
    assert signalled.terminal_status == "indeterminate"
    assert signalled.exit_classification.signal_number == 9


def test_reconcile_does_not_invoke_or_mutate_evidence_callbacks() -> None:
    run_id = uuid4()
    config_hash = "c" * 64
    integrity = _evidence(config_hash)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="succeeded",
        digest=integrity.digest,
        result_counts=integrity.counts,
    )
    callbacks: list[str] = []
    decision = reconcile_terminal_evidence(
        run_id,
        raw_exit_code=0,
        completion_marker=marker,
        recomputed_integrity=integrity,
        expected_config_hash=config_hash,
        termination_evidence={"callback": lambda: callbacks.append("called")},
    )
    assert decision.terminal_status == "succeeded"
    assert callbacks == []


def test_reconcile_requires_the_frozen_config_hash_to_prove_identity() -> None:
    run_id = uuid4()
    config_hash = "d" * 64
    integrity = _evidence(config_hash)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="succeeded",
        digest=integrity.digest,
        result_counts=integrity.counts,
    )
    decision = reconcile_terminal_evidence(
        run_id,
        0,
        None,
        marker,
        integrity,
        None,
        None,
    )
    assert decision.terminal_status == "indeterminate"
    assert decision.terminal_decision_reason == "config_hash_unavailable"


def test_historical_repository_cannot_write_a_terminal_decision() -> None:
    """The compatibility repository exposes no terminal-state write bypass."""

    repository = RunRepository()
    run_id = uuid4()
    try:
        repository.adjudicate(run_id, marker=None, exit_code=None)
    except PermissionError as exc:
        assert "Supervisor" in str(exc)
    else:  # pragma: no cover - the assertion documents the ownership contract
        raise AssertionError("historical repository unexpectedly wrote terminal state")


def test_terminal_repository_requires_lock_and_keeps_first_cas_decision() -> None:
    """The canonical repository performs one locked CAS and keeps first-writer wins."""

    run_id = uuid4()
    row = SimpleNamespace(
        id=run_id,
        status="running",
        terminal_status=None,
        launch_id=None,
        child_pid=None,
        child_start_identity=None,
        child_process_group_id=None,
        worker_handshake_at=None,
    )

    class Session:
        def __init__(self):
            self.executed = []

        def scalar(self, _statement):
            return row

        def execute(self, statement):
            self.executed.append(statement)
            row.status = "succeeded"
            row.terminal_status = "succeeded"
            return SimpleNamespace(rowcount=1)

        def flush(self):
            return None

    class Lock:
        held = True

        def assert_held(self):
            return None

    session = Session()
    repository = DatabaseRunRepository(session)
    with pytest.raises(SupervisorLockNotHeld):
        repository.set_terminal(
            run_id,
            "succeeded",
            supervisor_lock=SimpleNamespace(held=False),
            terminal_decision_reason="completion_evidence_consistent",
        )
    assert session.executed == []

    first = repository.set_terminal(
        run_id,
        "succeeded",
        supervisor_lock=Lock(),
        terminal_decision_reason="completion_evidence_consistent",
    )
    second = repository.set_terminal(
        run_id,
        "failed",
        supervisor_lock=Lock(),
        terminal_decision_reason="late_conflicting_evidence",
    )
    assert first is row
    assert second is row
    assert row.status == "succeeded"
    assert row.terminal_status == "succeeded"
    assert len(session.executed) == 1


def test_worker_marker_writer_uses_launch_cas_and_replays_idempotently() -> None:
    """A stale launch cannot publish a marker; an exact replay cannot replace it."""

    run_id = uuid4()
    launch_id = uuid4()
    config_hash = "e" * 64
    integrity = _evidence(config_hash)
    marker = build_completion_marker(
        run_id=run_id,
        declared_category="succeeded",
        digest=integrity.digest,
        result_counts=integrity.counts,
        config_hash=config_hash,
    )
    row = SimpleNamespace(
        id=run_id,
        run_kind="backtest_run",
        profile="formal@1",
        tenant_id="default",
        config_hash=config_hash,
        status="running",
        terminal_status=None,
        launch_id=launch_id,
        completion_marker=None,
        runner_exit_code=None,
    )

    class Session:
        def __init__(self):
            self.executed = []

        def get(self, _model, _run_id):
            return row

        def execute(self, statement):
            self.executed.append(statement)
            if row.completion_marker is None:
                row.completion_marker = dict(marker)
                row.runner_exit_code = 0
                return SimpleNamespace(rowcount=1)
            return SimpleNamespace(rowcount=0)

        def flush(self):
            return None

    session = Session()
    stale = BacktestResultPersistenceService(
        session,
        BacktestResultContext(
            run_id=run_id,
            run_kind="backtest_run",
            profile="formal@1",
            config_hash=config_hash,
            launch_id=uuid4(),
        ),
    )
    with pytest.raises(ValueError, match="launch_id"):
        stale.record_completion_marker(marker, exit_code=0)
    assert session.executed == []

    writer = BacktestResultPersistenceService(
        session,
        BacktestResultContext(
            run_id=run_id,
            run_kind="backtest_run",
            profile="formal@1",
            config_hash=config_hash,
            launch_id=launch_id,
        ),
    )
    writer.record_completion_marker(marker, exit_code=0)
    writer.record_completion_marker(marker, exit_code=0)
    assert row.completion_marker == marker
    assert row.runner_exit_code == 0
    assert len(session.executed) == 2


def test_result_writer_requires_launch_identity_for_all_durable_writes() -> None:
    """A run id without the current launch id cannot write progress or markers."""

    run_id = uuid4()
    config_hash = "f" * 64
    launch_id = uuid4()
    row = SimpleNamespace(
        id=run_id,
        run_kind="backtest_run",
        profile="formal@1",
        tenant_id="default",
        config_hash=config_hash,
        status="running",
        terminal_status=None,
        launch_id=launch_id,
        progress=0,
    )

    class Session:
        def get(self, _model, _run_id):
            return row

        def execute(self, _statement):
            raise AssertionError("missing launch identity must fail before SQL")

    writer = BacktestResultPersistenceService(
        Session(),
        BacktestResultContext(
            run_id=run_id,
            run_kind="backtest_run",
            profile="formal@1",
            config_hash=config_hash,
        ),
    )
    with pytest.raises(ValueError, match="launch_id"):
        writer.record_progress(0.25)


def test_database_repository_progress_requires_matching_launch_and_active_state() -> None:
    """The canonical progress repository rejects missing and stale identities."""

    run_id = uuid4()
    launch_id = uuid4()
    row = SimpleNamespace(
        id=run_id,
        status="running",
        launch_id=launch_id,
        progress=0,
    )

    class Session:
        def __init__(self):
            self.executed = 0

        def scalar(self, _statement):
            return row

        def execute(self, _statement):
            self.executed += 1
            return SimpleNamespace(rowcount=1)

        def flush(self):
            return None

    session = Session()
    repository = DatabaseRunRepository(session)
    with pytest.raises(ValueError, match="launch_id"):
        repository.record_progress(run_id, 0.1)
    assert session.executed == 0

    assert repository.record_progress(run_id, 0.1, launch_id=uuid4()) is False
    assert session.executed == 0
