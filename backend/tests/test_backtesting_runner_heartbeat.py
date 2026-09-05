"""Frozen timeline progress, throttling, and liveness tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.backtesting.runner_progress import (
    FrozenTimelineProgress,
    ProgressReporter,
    is_lost_heartbeat,
)


class RunnerHeartbeatTestCase(unittest.TestCase):
    def test_progress_is_monotonic_and_step_updates_are_throttled(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        events = []
        reporter = ProgressReporter(
            "run-1",
            persist_progress=lambda snapshot: events.append(("progress", snapshot)),
            persist_heartbeat=lambda snapshot: events.append(("heartbeat", snapshot)),
            clock=lambda: base,
        )
        reporter.report(FrozenTimelineProgress(10, 1, "step-1", "2026-01-01"), now=base)
        reporter.report(FrozenTimelineProgress(10, 2, "step-2", "2026-01-02"), now=base + timedelta(seconds=1))
        self.assertEqual([kind for kind, _ in events], ["progress"])
        reporter.report(FrozenTimelineProgress(10, 2, "step-2", "2026-01-02"), now=base + timedelta(seconds=6))
        self.assertEqual([kind for kind, _ in events], ["progress", "progress"])
        with self.assertRaises(ValueError):
            reporter.report(FrozenTimelineProgress(10, 1, "step-1", "2026-01-01"), now=base + timedelta(seconds=16))

    def test_lost_heartbeat_uses_timezone_aware_60_second_bound(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertFalse(is_lost_heartbeat(base, now=base + timedelta(seconds=59)))
        self.assertTrue(is_lost_heartbeat(base, now=base + timedelta(seconds=60)))
        self.assertTrue(
            is_lost_heartbeat(
                None,
                started_at=base,
                now=base + timedelta(seconds=60),
            )
        )


if __name__ == "__main__":
    unittest.main()
