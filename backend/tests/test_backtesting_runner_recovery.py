"""Startup recovery and PID-reuse safety tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from app.backtesting.runner_process import LaunchIdentity
from app.backtesting.runner_supervisor import InMemoryRunRepository, RunnerSupervisor
import app.backtesting.runner_process as process_api


class FakeLock:
    held = False

    def acquire(self):
        self.held = True
        return True

    def release(self):
        self.held = False

    def assert_held(self):
        if not self.held:
            raise RuntimeError("lock missing")


class Row:
    def __init__(self, status: str) -> None:
        self.id = uuid4()
        self.run_kind = "backtest_run"
        self.status = status
        self.config_hash = "b" * 64
        self.launch_id = uuid4()
        self.child_pid = 4321
        self.child_start_identity = "old-start"
        self.child_process_group_id = 4321
        self.completion_marker = None
        self.runner_exit_code = None
        self.result_integrity_evidence = None
        self.terminal_status = None
        self.created_at = datetime.now(UTC)


class RecoveryTestCase(unittest.TestCase):
    def test_pid_reuse_is_not_signalled_and_becomes_indeterminate(self) -> None:
        row = Row("running")
        repo = InMemoryRunRepository([row])
        old_alive = process_api.is_process_alive
        old_match = process_api.process_identity_matches
        old_term = process_api.send_graceful_termination
        old_kill = process_api.send_force_kill
        signals = []
        process_api.is_process_alive = lambda _pid: True
        process_api.process_identity_matches = lambda _identity: False
        process_api.send_graceful_termination = lambda identity: signals.append(("term", identity)) or True
        process_api.send_force_kill = lambda identity: signals.append(("kill", identity)) or True
        try:
            supervisor = RunnerSupervisor(repository=repo, lock=FakeLock())
            supervisor.acquire_lock()
            supervisor.startup_recovery()
        finally:
            process_api.is_process_alive = old_alive
            process_api.process_identity_matches = old_match
            process_api.send_graceful_termination = old_term
            process_api.send_force_kill = old_kill
        self.assertEqual(signals, [])
        self.assertEqual(row.status, "indeterminate")
        self.assertEqual(row.failure_phase, "runner_supervisor_recovery")
        self.assertIsNotNone(row.recovery_observed_at)
        self.assertEqual(row.recovery_action, "identity_unverified")

    def test_queued_rows_remain_queued_during_recovery(self) -> None:
        row = Row("queued")
        row.launch_id = None
        row.child_pid = None
        row.child_start_identity = None
        row.child_process_group_id = None
        repo = InMemoryRunRepository([row])
        supervisor = RunnerSupervisor(repository=repo, lock=FakeLock())
        supervisor.acquire_lock()
        supervisor.startup_recovery()
        self.assertEqual(row.status, "queued")

    def test_exited_child_is_reconciled_with_recovery_process_state(self) -> None:
        row = Row("running")
        row.runner_exit_code = 10
        repo = InMemoryRunRepository([row])
        old_alive = process_api.is_process_alive
        old_match = process_api.process_identity_matches
        try:
            process_api.is_process_alive = lambda _pid: False
            process_api.process_identity_matches = lambda _identity: False
            supervisor = RunnerSupervisor(repository=repo, lock=FakeLock())
            supervisor.acquire_lock()
            outcomes = supervisor.startup_recovery()
        finally:
            process_api.is_process_alive = old_alive
            process_api.process_identity_matches = old_match

        self.assertEqual(outcomes, (f"{row.id}:indeterminate",))
        self.assertEqual(row.status, "indeterminate")
        self.assertEqual(row.recovery_process_state, {
            "identity_present": True,
            "pid_alive": False,
            "identity_matches": False,
        })


if __name__ == "__main__":
    unittest.main()
