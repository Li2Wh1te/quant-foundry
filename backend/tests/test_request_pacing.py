import unittest
from unittest.mock import patch

from app.data_ingestion.request_pacing import RequestPacer


class RequestPacerTestCase(unittest.TestCase):
    @patch("app.data_ingestion.request_pacing.time.sleep")
    @patch(
        "app.data_ingestion.request_pacing.time.monotonic",
        side_effect=[10.0, 10.0, 10.2, 10.2],
    )
    def test_waits_until_the_next_request_slot(self, monotonic_mock, sleep_mock) -> None:
        pacer = RequestPacer()

        pacer.wait_for_turn(1_000)
        pacer.wait_for_turn(1_000)

        sleep_mock.assert_called_once()
        self.assertAlmostEqual(sleep_mock.call_args.args[0], 0.8)
