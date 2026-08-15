"""In-process pacing for outbound data source requests."""

import threading
import time


class RequestPacer:
    """Serialize request starts so callers respect their requested minimum interval."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait_for_turn(self, interval_ms: int) -> None:
        """Wait until a request may start without violating the minimum interval."""
        interval_seconds = interval_ms / 1_000
        with self._lock:
            now = time.monotonic()
            request_at = max(now, self._next_request_at)
            self._next_request_at = request_at + interval_seconds
        delay = request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)


tushare_request_pacer = RequestPacer()
