"""Worker launch and bounded diagnostic output tests."""

from __future__ import annotations

import unittest
from uuid import uuid4

from app.backtesting.runner_process import (
    HANDSHAKE_PROTOCOL,
    StdoutCapture,
    apply_memory_limit,
    build_worker_command,
    memory_limit_evidence,
    WorkerProcessLauncher,
    validate_handshake,
    drain_output,
)


class RunnerProcessTestCase(unittest.TestCase):
    def test_command_has_only_run_and_launch_identity(self) -> None:
        run_id = uuid4()
        launch_id = uuid4()
        command = build_worker_command(run_id, launch_id)
        self.assertEqual(command[-4:], ["--run-id", str(run_id), "--launch-id", str(launch_id)])
        self.assertNotIn("shell", command)
        self.assertNotIn("token", " ".join(command).lower())

    def test_output_cap_keeps_digest_and_bounded_excerpt(self) -> None:
        capture = StdoutCapture(max_bytes=4, excerpt_bytes=3)
        self.assertFalse(capture.consume(b"abc"))
        self.assertTrue(capture.consume(b"de"))
        self.assertEqual(capture.bytes, 5)
        self.assertTrue(capture.truncated)
        self.assertEqual(capture.excerpt, "abc")
        self.assertTrue(capture.digest.startswith("sha256:"))

    def test_handshake_requires_every_identity_field(self) -> None:
        run_id = uuid4()
        launch_id = uuid4()
        payload = {
            "protocol_version": HANDSHAKE_PROTOCOL,
            "run_id": str(run_id),
            "launch_id": str(launch_id),
            "pid": 123,
            "start_identity": "start",
            "process_group_id": 123,
        }
        self.assertTrue(
            validate_handshake(
                payload,
                expected_run_id=run_id,
                expected_launch_id=launch_id,
                expected_pid=123,
                expected_start_identity="start",
                expected_process_group_id=123,
            )
        )
        payload["pid"] = 124
        self.assertFalse(
            validate_handshake(
                payload,
                expected_run_id=run_id,
                expected_launch_id=launch_id,
                expected_pid=123,
                expected_start_identity="start",
                expected_process_group_id=123,
            )
        )

    def test_memory_evidence_is_explicit_when_limit_is_disabled(self) -> None:
        evidence = memory_limit_evidence(None)
        self.assertFalse(evidence.supported)
        self.assertFalse(evidence.applied)
        self.assertIsNone(evidence.requested)
        self.assertEqual(apply_memory_limit(None).as_dict(), evidence.as_dict())

    def test_worker_command_rejects_non_uuid_identity(self) -> None:
        with self.assertRaises(ValueError):
            build_worker_command("not-a-uuid", "also-not-a-uuid")

    def test_launcher_waits_for_worker_confirmation_of_memory_evidence(self) -> None:
        launcher = WorkerProcessLauncher(memory_limit_mib=64)
        evidence = launcher.resource_limit_evidence()
        if evidence.supported:
            self.assertFalse(evidence.applied)
            self.assertEqual(evidence.error, "awaiting_worker_confirmation")
            self.assertEqual(evidence.requested, 64)
        else:
            self.assertFalse(evidence.applied)

    def test_drain_output_stops_at_the_configured_cap(self) -> None:
        class Stream:
            def __init__(self):
                self.requests = []

            def read(self, size):
                self.requests.append(size)
                return b"x" * size

        stream = Stream()
        process = type("Process", (), {"stdout": stream})()
        capture = StdoutCapture(max_bytes=4)

        self.assertTrue(drain_output(process, capture, chunk_size=64))
        self.assertEqual(capture.bytes, 4)
        self.assertEqual(stream.requests, [4])


if __name__ == "__main__":
    unittest.main()
