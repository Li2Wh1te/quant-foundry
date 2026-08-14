from __future__ import annotations

import atexit
import copy
import logging
import logging.handlers
import queue
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import structlog
from concurrent_log_handler import ConcurrentTimedRotatingFileHandler

from app.core.config import Settings


LOG_FILE_NAME = "app.jsonl"


def _add_application_context(
    service: str,
    environment: str,
):
    def processor(
        logger: logging.Logger,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        event_dict.setdefault("service", service)
        event_dict.setdefault("environment", environment)
        return event_dict

    return processor


class DroppingQueueHandler(logging.handlers.QueueHandler):
    """Keep request handling non-blocking if disk logging falls behind."""

    def __init__(self, log_queue: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(log_queue)
        self.dropped_count = 0
        self._counter_lock = Lock()

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            if record.levelno >= logging.WARNING:
                try:
                    self.queue.put(record, timeout=0.05)
                    return
                except queue.Full:
                    pass
            with self._counter_lock:
                self.dropped_count += 1

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # ProcessorFormatter needs the structured event dict on the listener thread.
        return copy.copy(record)


@dataclass
class LoggingRuntime:
    listener: logging.handlers.QueueListener
    queue_handler: DroppingQueueHandler
    file_handler: ConcurrentTimedRotatingFileHandler
    log_file: Path
    _stopped: bool = False

    def stop(self) -> None:
        if self._stopped:
            return
        self.listener.stop()
        for handler in self.listener.handlers:
            handler.close()
        self._stopped = True

_runtime: LoggingRuntime | None = None
_runtime_lock = Lock()


def configure_logging(settings: Settings) -> LoggingRuntime:
    global _runtime

    with _runtime_lock:
        if _runtime is not None and not _runtime._stopped:
            return _runtime

        settings.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = settings.log_dir / LOG_FILE_NAME
        level = getattr(logging, settings.log_level)
        timestamp = structlog.processors.TimeStamper(fmt="iso", utc=True)
        application_context = _add_application_context(
            settings.app_name,
            settings.environment,
        )
        shared_processors = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            timestamp,
            application_context,
        ]

        formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
        file_handler = ConcurrentTimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=settings.log_retention_days,
            encoding="utf-8",
            utc=True,
            use_gzip=True,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)

        log_queue: queue.Queue[logging.LogRecord] = queue.Queue(
            maxsize=settings.log_queue_size
        )
        queue_handler = DroppingQueueHandler(log_queue)
        queue_handler.setLevel(level)
        listener = logging.handlers.QueueListener(
            log_queue,
            file_handler,
            console_handler,
            respect_handler_level=True,
        )

        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.addHandler(queue_handler)
        root_logger.setLevel(level)
        for logger_name in ("uvicorn", "uvicorn.error"):
            logger = logging.getLogger(logger_name)
            logger.handlers.clear()
            logger.propagate = True
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.handlers.clear()
        access_logger.propagate = False

        structlog.configure(
            processors=[
                *shared_processors,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        listener.start()
        _runtime = LoggingRuntime(listener, queue_handler, file_handler, log_file)
        atexit.register(_runtime.stop)
        return _runtime


def get_logging_runtime() -> LoggingRuntime | None:
    return _runtime
