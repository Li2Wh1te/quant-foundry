import contextlib
import io
import json
import logging
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import structlog

from app.core.config import Settings
from app.core.logging import configure_logging


API_TOKEN = "a" * 64


def _write_logs_from_process(log_dir: str, process_number: int, count: int) -> None:
    with contextlib.redirect_stderr(io.StringIO()):
        runtime = configure_logging(
            Settings(
                api_token=API_TOKEN,
                log_dir=Path(log_dir),
                log_queue_size=1000,
                database_password="test-secret",
                _env_file=None,
            )
        )
        logger = structlog.get_logger("test.multiprocess")
        for index in range(count):
            logger.info("process_record", process=process_number, index=index)
        runtime.stop()


class LoggingTestCase(unittest.TestCase):
    def test_concurrent_records_are_written_as_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings(
                api_token=API_TOKEN,
                log_dir=Path(temporary_directory),
                log_queue_size=1000,
                database_password="test-secret",
                _env_file=None,
            )
            with contextlib.redirect_stderr(io.StringIO()):
                runtime = configure_logging(settings)
                logger = structlog.get_logger("test.concurrent")

                with ThreadPoolExecutor(max_workers=8) as executor:
                    list(
                        executor.map(
                            lambda index: logger.info("record", index=index), range(200)
                        )
                    )
                self.assertFalse(logging.getLogger("uvicorn.access").propagate)
                runtime.stop()

            lines = runtime.log_file.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines]

        self.assertEqual(len(records), 200)
        self.assertEqual({record["index"] for record in records}, set(range(200)))
        self.assertTrue(all(record["event"] == "record" for record in records))
        self.assertEqual(runtime.queue_handler.dropped_count, 0)
        logging.getLogger().handlers.clear()

    def test_multiple_processes_share_one_jsonl_file_safely(self) -> None:
        process_count = 3
        records_per_process = 40
        with tempfile.TemporaryDirectory() as temporary_directory:
            context = multiprocessing.get_context("spawn")
            processes = [
                context.Process(
                    target=_write_logs_from_process,
                    args=(temporary_directory, process_number, records_per_process),
                )
                for process_number in range(process_count)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            lines = (Path(temporary_directory) / "app.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            records = [json.loads(line) for line in lines]

        self.assertEqual(len(records), process_count * records_per_process)
        self.assertEqual(
            {(record["process"], record["index"]) for record in records},
            {
                (process_number, index)
                for process_number in range(process_count)
                for index in range(records_per_process)
            },
        )

    def test_runner_event_jsonl_contains_independent_chinese_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings(
                api_token=API_TOKEN,
                cursor_signing_key="b" * 64,
                log_dir=Path(temporary_directory),
                log_queue_size=100,
                database_password="test-secret",
                _env_file=None,
            )
            with contextlib.redirect_stderr(io.StringIO()):
                runtime = configure_logging(settings)
                logging.getLogger("backtesting.runner.supervisor").info(
                    "回测 worker 已启动，等待身份握手。",
                    extra={"event": "backtest_worker_started", "run_id": "run-1"},
                )
                runtime.stop()
            records = [
                json.loads(line)
                for line in runtime.log_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(records)
            self.assertTrue(
                any(
                    record.get("event") == "backtest_worker_started"
                    and "回测 worker" in record.get("message", "")
                    for record in records
                )
            )
            logging.getLogger().handlers.clear()


if __name__ == "__main__":
    unittest.main()
