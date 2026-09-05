"""Supervisor lock, formal-first claim, handshake, and terminal tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.backtesting.runner_integrity import compute_result_integrity
from app.backtesting.runner_process import LaunchIdentity
from app.backtesting.runner_protocol import build_completion_marker
from app.backtesting.runner_supervisor import (
    InMemoryRunRepository,
    RunnerSupervisor,
    SupervisorSettings,
    SupervisorLockNotHeld,
)
import app.backtesting.runner_process as process_api


class FakeLock:
    def __init__(self) -> None:
        self.held = False

    def acquire(self) -> bool:
        if self.held:
            return False
        self.held = True
        return True

    def release(self) -> None:
        self.held = False

    def assert_held(self) -> None:
        if not self.held:
            raise RuntimeError("not held")


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code = None
        self.stdout = None

    def poll(self):
        return self.exit_code


class FakeLauncher:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process

    def start(self, _run_id, _launch_id):
        return self.process


class Row:
    def __init__(self, kind: str) -> None:
        self.id = uuid4()
        self.run_kind = kind
        self.status = "queued"
        self.config_hash = "a" * 64
        self.created_at = datetime.now(UTC)
        self.cancel_requested = False
        self.completion_marker = None
        self.result_integrity_evidence = None
        self.terminal_status = None


class RunnerSupervisorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.original_identity = process_api.identity_from_process
        self.original_alive = process_api.is_process_alive
        self.original_match = process_api.process_identity_matches
        self.original_term = process_api.send_graceful_termination
        self.original_kill = process_api.send_force_kill
        process_api.identity_from_process = lambda process, run_id, launch_id: LaunchIdentity(run_id, launch_id, process.pid, "start", process.pid)
        process_api.is_process_alive = lambda _pid: True
        process_api.process_identity_matches = lambda _identity: True
        process_api.send_graceful_termination = lambda _identity: True
        process_api.send_force_kill = lambda _identity: True

    def tearDown(self) -> None:
        process_api.identity_from_process = self.original_identity
        process_api.is_process_alive = self.original_alive
        process_api.process_identity_matches = self.original_match
        process_api.send_graceful_termination = self.original_term
        process_api.send_force_kill = self.original_kill

    def test_formal_queue_is_claimed_before_internal(self) -> None:
        internal = Row("internal_link_acceptance")
        formal = Row("backtest_run")
        repository = InMemoryRunRepository([internal, formal])
        process = FakeProcess(1001)
        supervisor = RunnerSupervisor(repository=repository, lock=FakeLock(), launcher=FakeLauncher(process))
        self.assertTrue(supervisor.acquire_lock())
        supervisor.run_once()
        self.assertEqual(formal.status, "starting")
        self.assertEqual(internal.status, "queued")
        self.assertEqual(len(supervisor.children), 1)

    def test_handshake_is_required_before_running(self) -> None:
        row = Row("backtest_run")
        repository = InMemoryRunRepository([row])
        process = FakeProcess(1002)
        supervisor = RunnerSupervisor(repository=repository, lock=FakeLock(), launcher=FakeLauncher(process))
        supervisor.acquire_lock()
        supervisor.run_once()
        handle = next(iter(supervisor.children.values()))
        self.assertEqual(row.status, "starting")
        self.assertTrue(
            supervisor.handle_handshake(
                {
                    "protocol_version": "runner_handshake@1",
                    "run_id": str(row.id),
                    "launch_id": str(handle.launch_id),
                    "pid": 1002,
                    "start_identity": "start",
                    "process_group_id": 1002,
                }
            )
        )
        self.assertEqual(row.status, "running")

    def test_consistent_evidence_is_reconciled_and_terminal_is_immutable(self) -> None:
        row = Row("backtest_run")
        rows = {name: [] for name in (
            "backtest_steps", "backtest_decisions", "backtest_orders",
            "backtest_order_updates", "backtest_fills", "backtest_positions",
            "backtest_equity_curve", "backtest_metrics",
        )}
        integrity = compute_result_integrity(rows, config_hash=row.config_hash)
        row.completion_marker = build_completion_marker(
            run_id=row.id,
            declared_category="succeeded",
            digest=integrity.digest,
            result_counts=integrity.counts,
        )
        repository = InMemoryRunRepository([row])
        process = FakeProcess(1003)
        supervisor = RunnerSupervisor(
            repository=repository,
            lock=FakeLock(),
            launcher=FakeLauncher(process),
            integrity_checker_factory=lambda _row: integrity,
        )
        supervisor.acquire_lock()
        supervisor.run_once()
        process.exit_code = 0
        self.assertEqual(supervisor.run_once(), ("succeeded",))
        self.assertEqual(row.status, "succeeded")
        self.assertEqual(supervisor.reconcile_run(row.id, marker=row.completion_marker, exit_code=0, integrity=integrity), "succeeded")
        self.assertEqual(row.status, "succeeded")
        self.assertEqual(
            supervisor.reconcile_run(
                row.id,
                marker=None,
                exit_code=10,
                integrity=None,
                reason="late_conflicting_evidence",
            ),
            "succeeded",
        )
        self.assertEqual(row.terminal_decision_reason, "completion_evidence_consistent")

    def test_supervisor_fallback_cannot_write_terminal_state_without_repository_cas(self) -> None:
        row = Row("backtest_run")

        class ReadOnlyRepository:
            def get(self, run_id):
                return row if str(run_id) == str(row.id) else None

            def commit(self):
                raise AssertionError("a read-only repository must not be committed")

        supervisor = RunnerSupervisor(
            repository=ReadOnlyRepository(),
            lock=FakeLock(),
        )
        supervisor.acquire_lock()
        with self.assertRaises(PermissionError):
            supervisor._write_terminal(
                row.id,
                status="indeterminate",
                marker=None,
                exit_code=None,
                integrity=None,
                reason="missing_evidence",
                failure_phase="runner_supervisor_recovery",
                forced=False,
                recovery_action="identity_unverified",
            )
        self.assertEqual(row.status, "queued")

    def test_queued_cancellation_is_closed_without_starting_a_child(self) -> None:
        row = Row("backtest_run")
        # The persisted queue keeps ``status=queued`` and records the request
        # in its dedicated cancellation flag until Supervisor closes it.
        row.status = "queued"
        row.cancel_requested = True
        row.cancel_requested_at = datetime.now(UTC)
        repository = InMemoryRunRepository([row])
        supervisor = RunnerSupervisor(repository=repository, lock=FakeLock())
        self.assertTrue(supervisor.acquire_lock())

        supervisor.run_once()

        self.assertEqual(row.status, "cancelled")
        self.assertEqual(row.terminal_decision_reason, "cancelled_before_start")
        self.assertIsNone(row.completion_marker)
        self.assertEqual(supervisor.children, {})

    def test_direct_launch_api_closes_queued_cancellation_before_claim(self) -> None:
        row = Row("backtest_run")
        row.cancel_requested = True
        repository = InMemoryRunRepository([row])
        supervisor = RunnerSupervisor(repository=repository, lock=FakeLock())
        supervisor.acquire_lock()

        self.assertIsNone(repository.claim_next())
        self.assertIsNone(supervisor.launch_next())
        self.assertEqual(row.status, "cancelled")
        self.assertEqual(row.terminal_decision_reason, "cancelled_before_start")
        self.assertEqual(supervisor.children, {})

    def test_first_missing_heartbeat_is_detected_and_audited(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        row = Row("backtest_run")
        repository = InMemoryRunRepository([row])
        process = FakeProcess(1004)
        supervisor = RunnerSupervisor(
            repository=repository,
            lock=FakeLock(),
            launcher=FakeLauncher(process),
            clock=lambda: base,
        )
        self.assertTrue(supervisor.acquire_lock())
        supervisor.run_once()
        handle = next(iter(supervisor.children.values()))

        lost = supervisor.process_heartbeat_timeouts(now=base + timedelta(seconds=60))

        self.assertEqual(lost, (str(row.id),))
        self.assertEqual(row.failure_phase, "runner_lost_heartbeat")
        self.assertIsNotNone(row.termination_requested_at)
        self.assertEqual(row.termination_reason, "runner_lost_heartbeat")
        self.assertIs(handle, supervisor.children[str(row.id)])

    def test_lost_heartbeat_detection_is_emitted_once_per_launch(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        row = Row("backtest_run")
        repository = InMemoryRunRepository([row])
        process = FakeProcess(1007)
        supervisor = RunnerSupervisor(
            repository=repository,
            lock=FakeLock(),
            launcher=FakeLauncher(process),
            clock=lambda: base,
        )
        supervisor.acquire_lock()
        supervisor.run_once()

        first = supervisor.process_heartbeat_timeouts(now=base + timedelta(seconds=60))
        second = supervisor.process_heartbeat_timeouts(now=base + timedelta(seconds=61))

        self.assertEqual(first, (str(row.id),))
        self.assertEqual(second, ())

    def test_lock_loss_stops_known_worker_without_durable_claims(self) -> None:
        row = Row("backtest_run")
        repository = InMemoryRunRepository([row])
        process = FakeProcess(1005)
        lock = FakeLock()
        supervisor = RunnerSupervisor(
            repository=repository,
            lock=lock,
            launcher=FakeLauncher(process),
        )
        supervisor.acquire_lock()
        supervisor.run_once()
        lock.held = False
        with self.assertRaises(SupervisorLockNotHeld):
            supervisor.run_once()
        self.assertEqual(row.status, "starting")

    def test_cancel_grace_escalates_from_term_to_kill(self) -> None:
        row = Row("backtest_run")
        repository = InMemoryRunRepository([row])
        process = FakeProcess(1006)
        now = [0.0]
        supervisor = RunnerSupervisor(
            repository=repository,
            lock=FakeLock(),
            launcher=FakeLauncher(process),
            settings=SupervisorSettings(
                cancel_grace_seconds=1,
                run_timeout_seconds=10,
                memory_limit_mib=None,
            ),
            monotonic=lambda: now[0],
        )
        supervisor.acquire_lock()
        supervisor.run_once()
        row.cancel_requested = True
        supervisor.process_cancellations()
        now[0] = 2.0
        supervisor.process_cancellations()
        self.assertTrue(next(iter(supervisor.children.values())).force_kill_sent)


if __name__ == "__main__":
    unittest.main()
