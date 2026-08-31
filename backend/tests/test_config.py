import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import Settings


API_TOKEN = "a" * 64
CURSOR_SIGNING_KEY = "b" * 64


class SettingsTestCase(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(
                api_token=API_TOKEN,
                cursor_signing_key=CURSOR_SIGNING_KEY,
                database_password="test-secret",
                _env_file=None,
            )

        self.assertEqual(settings.app_name, "Quant Foundry API")
        self.assertEqual(settings.environment, "local")
        self.assertFalse(settings.debug)
        self.assertEqual(str(settings.server_host), "127.0.0.1")
        self.assertEqual(settings.server_port, 8000)
        self.assertEqual(settings.api_token.get_secret_value(), API_TOKEN)
        self.assertEqual(
            settings.cursor_signing_key.get_secret_value(), CURSOR_SIGNING_KEY
        )
        self.assertEqual(settings.log_dir.name, "logs")
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(settings.log_retention_days, 30)
        self.assertEqual(settings.log_queue_size, 10_000)
        self.assertEqual(settings.log_query_max_files, 32)
        self.assertTrue(settings.scheduler_enabled)
        self.assertEqual(settings.scheduler_max_workers, 4)
        self.assertEqual(settings.scheduler_dispatch_interval_ms, 500)
        self.assertEqual(settings.scheduler_max_queued_runs, 1_000)
        self.assertEqual(settings.scheduler_misfire_grace_seconds, 60)
        self.assertEqual(settings.backtest_max_workers, 1)
        self.assertEqual(settings.backtest_max_queued_runs, 32)
        self.assertIsNone(settings.backtest_internal_max_queued_runs)
        self.assertEqual(settings.backtest_run_timeout_seconds, 7_200)
        self.assertEqual(settings.backtest_cancel_grace_seconds, 10)
        self.assertEqual(settings.backtest_stdout_max_bytes, 1_048_576)
        self.assertEqual(settings.backtest_memory_limit_mib, 1_024)
        self.assertEqual(settings.backtest_heartbeat_max_interval_seconds, 15)
        self.assertEqual(settings.backtest_lost_heartbeat_seconds, 60)
        self.assertEqual(settings.backtest_progress_persist_interval_seconds, 5)
        self.assertIsNone(settings.tushare_token)
        self.assertEqual(settings.tushare_api_url, "http://api.tushare.pro")
        self.assertEqual(settings.ingestion_request_interval_ms, 1_000)
        self.assertEqual(
            settings.database_url.render_as_string(hide_password=False),
            "postgresql+psycopg://postgres:test-secret@127.0.0.1:5432/quant_foundry",
        )

    def test_environment_variables_override_defaults(self) -> None:
        environment = {
            "QF_APP_NAME": "Test API",
            "QF_ENVIRONMENT": "test",
            "QF_DEBUG": "true",
            "QF_SERVER_HOST": "0.0.0.0",
            "QF_SERVER_PORT": "9000",
            "QF_API_TOKEN": API_TOKEN,
            "QF_CURSOR_SIGNING_KEY": CURSOR_SIGNING_KEY,
            "QF_LOG_DIR": "var/logs",
            "QF_LOG_LEVEL": "WARNING",
            "QF_LOG_RETENTION_DAYS": "14",
            "QF_LOG_QUEUE_SIZE": "5000",
            "QF_LOG_QUERY_MAX_FILES": "16",
            "QF_SCHEDULER_ENABLED": "false",
            "QF_SCHEDULER_MAX_WORKERS": "8",
            "QF_SCHEDULER_DISPATCH_INTERVAL_MS": "1000",
            "QF_SCHEDULER_MAX_QUEUED_RUNS": "500",
            "QF_SCHEDULER_MISFIRE_GRACE_SECONDS": "120",
            "QF_BACKTEST_MAX_WORKERS": "2",
            "QF_BACKTEST_MAX_QUEUED_RUNS": "16",
            "QF_BACKTEST_INTERNAL_MAX_QUEUED_RUNS": "4",
            "QF_BACKTEST_RUN_TIMEOUT_SECONDS": "3600",
            "QF_BACKTEST_CANCEL_GRACE_SECONDS": "30",
            "QF_BACKTEST_STDOUT_MAX_BYTES": "2048",
            "QF_BACKTEST_MEMORY_LIMIT_MIB": "512",
            "QF_BACKTEST_HEARTBEAT_MAX_INTERVAL_SECONDS": "20",
            "QF_BACKTEST_LOST_HEARTBEAT_SECONDS": "61",
            "QF_BACKTEST_PROGRESS_PERSIST_INTERVAL_SECONDS": "10",
            "QF_TUSHARE_TOKEN": "tushare-secret",
            "QF_TUSHARE_API_URL": "https://tu.brze.top",
            "QF_INGESTION_REQUEST_INTERVAL_MS": "1500",
            "QF_DATABASE_HOST": "db",
            "QF_DATABASE_PORT": "5433",
            "QF_DATABASE_USER": "app",
            "QF_DATABASE_PASSWORD": "secret",
            "QF_DATABASE_NAME": "quant_test",
        }

        with patch.dict(os.environ, environment, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.app_name, "Test API")
        self.assertEqual(settings.environment, "test")
        self.assertTrue(settings.debug)
        self.assertEqual(str(settings.server_host), "0.0.0.0")
        self.assertEqual(settings.server_port, 9000)
        self.assertEqual(settings.api_token.get_secret_value(), API_TOKEN)
        self.assertTrue(settings.log_dir.is_absolute())
        self.assertEqual(settings.log_dir.parts[-2:], ("var", "logs"))
        self.assertEqual(settings.log_level, "WARNING")
        self.assertEqual(settings.log_retention_days, 14)
        self.assertEqual(settings.log_queue_size, 5000)
        self.assertEqual(settings.log_query_max_files, 16)
        self.assertFalse(settings.scheduler_enabled)
        self.assertEqual(settings.scheduler_max_workers, 8)
        self.assertEqual(settings.scheduler_dispatch_interval_ms, 1000)
        self.assertEqual(settings.scheduler_max_queued_runs, 500)
        self.assertEqual(settings.scheduler_misfire_grace_seconds, 120)
        self.assertEqual(settings.backtest_max_workers, 2)
        self.assertEqual(settings.backtest_max_queued_runs, 16)
        self.assertEqual(settings.backtest_internal_max_queued_runs, 4)
        self.assertEqual(settings.backtest_run_timeout_seconds, 3_600)
        self.assertEqual(settings.backtest_cancel_grace_seconds, 30)
        self.assertEqual(settings.backtest_stdout_max_bytes, 2_048)
        self.assertEqual(settings.backtest_memory_limit_mib, 512)
        self.assertEqual(settings.backtest_heartbeat_max_interval_seconds, 20)
        self.assertEqual(settings.backtest_lost_heartbeat_seconds, 61)
        self.assertEqual(settings.backtest_progress_persist_interval_seconds, 10)
        self.assertEqual(settings.tushare_token.get_secret_value(), "tushare-secret")
        self.assertEqual(settings.tushare_api_url, "https://tu.brze.top")
        self.assertEqual(settings.ingestion_request_interval_ms, 1_500)
        self.assertEqual(
            settings.database_url.render_as_string(hide_password=False),
            "postgresql+psycopg://app:secret@db:5433/quant_test",
        )

    def test_invalid_environment_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {
                "QF_ENVIRONMENT": "invalid",
                "QF_API_TOKEN": API_TOKEN,
                "QF_CURSOR_SIGNING_KEY": CURSOR_SIGNING_KEY,
                "QF_DATABASE_PASSWORD": "test-secret",
            },
            clear=True,
        ):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)

    def test_invalid_server_settings_are_rejected(self) -> None:
        invalid_settings = (
            {"QF_SERVER_HOST": "not-an-ip"},
            {"QF_SERVER_PORT": "0"},
            {"QF_SERVER_PORT": "65536"},
            {"QF_SCHEDULER_MAX_WORKERS": "0"},
            {"QF_SCHEDULER_DISPATCH_INTERVAL_MS": "99"},
            {"QF_SCHEDULER_MAX_QUEUED_RUNS": "0"},
            {"QF_INGESTION_REQUEST_INTERVAL_MS": "-1"},
            {"QF_INGESTION_REQUEST_INTERVAL_MS": "60001"},
        )

        for environment in invalid_settings:
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValidationError):
                        Settings(
                            api_token=API_TOKEN,
                            cursor_signing_key=CURSOR_SIGNING_KEY,
                            database_password="test-secret",
                            _env_file=None,
                        )

    def test_backtest_settings_are_independent_from_scheduler_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "QF_SCHEDULER_MAX_WORKERS": "64",
                "QF_SCHEDULER_MAX_QUEUED_RUNS": "99999",
                "QF_API_TOKEN": API_TOKEN,
                "QF_CURSOR_SIGNING_KEY": CURSOR_SIGNING_KEY,
                "QF_DATABASE_PASSWORD": "test-secret",
            },
            clear=True,
        ):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.backtest_max_workers, 1)
        self.assertEqual(settings.backtest_max_queued_runs, 32)
        self.assertEqual(settings.scheduler_max_workers, 64)
        self.assertEqual(settings.scheduler_max_queued_runs, 99_999)

    def test_backtest_cross_field_constraints_are_rejected(self) -> None:
        invalid_settings = (
            {"backtest_lost_heartbeat_seconds": 44},
            {
                "backtest_heartbeat_max_interval_seconds": 20,
                "backtest_lost_heartbeat_seconds": 59,
            },
            {"backtest_progress_persist_interval_seconds": 16},
            {"backtest_cancel_grace_seconds": 7_200},
            {"backtest_internal_max_queued_runs": 0},
            {"backtest_internal_max_queued_runs": 32},
            {"backtest_max_queued_runs": 4, "backtest_internal_max_queued_runs": 4},
            {"backtest_max_queued_runs": 4, "backtest_internal_max_queued_runs": 5},
        )

        for overrides in invalid_settings:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    Settings(
                        api_token=API_TOKEN,
                        cursor_signing_key=CURSOR_SIGNING_KEY,
                        database_password="test-secret",
                        _env_file=None,
                        **overrides,
                    )

    def test_backtest_numeric_values_reject_non_integers_and_overflow(self) -> None:
        invalid_values = (True, 1.5, "", "1.5", "true", str(2**63))

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    Settings(
                        api_token=API_TOKEN,
                        cursor_signing_key=CURSOR_SIGNING_KEY,
                        database_password="test-secret",
                        _env_file=None,
                        backtest_max_workers=value,
                    )

        with patch.dict(
            os.environ,
            {
                "QF_BACKTEST_MAX_WORKERS": "",
                "QF_API_TOKEN": API_TOKEN,
                "QF_CURSOR_SIGNING_KEY": CURSOR_SIGNING_KEY,
                "QF_DATABASE_PASSWORD": "test-secret",
            },
            clear=True,
        ):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)

    def test_database_password_is_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError):
                Settings(
                    api_token=API_TOKEN,
                    cursor_signing_key=CURSOR_SIGNING_KEY,
                    _env_file=None,
                )

    def test_api_token_is_required_and_rejects_short_values(self) -> None:
        invalid_tokens = (None, "too-short")

        for api_token in invalid_tokens:
            with self.subTest(api_token=api_token):
                arguments = {"api_token": api_token} if api_token is not None else {}
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaises(ValidationError):
                        Settings(
                            cursor_signing_key=CURSOR_SIGNING_KEY,
                            database_password="test-secret",
                            _env_file=None,
                            **arguments,
                        )

    def test_cursor_signing_key_is_required_and_rejects_short_values(self) -> None:
        invalid_keys = (None, "too-short")

        for cursor_signing_key in invalid_keys:
            with self.subTest(cursor_signing_key=cursor_signing_key):
                arguments = (
                    {"cursor_signing_key": cursor_signing_key}
                    if cursor_signing_key is not None
                    else {}
                )
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaises(ValidationError):
                        Settings(
                            api_token=API_TOKEN,
                            database_password="test-secret",
                            _env_file=None,
                            **arguments,
                        )

    def test_database_url_encodes_credentials(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            cursor_signing_key=CURSOR_SIGNING_KEY,
            database_user="user@example.com",
            database_password="p@ss/word",
            _env_file=None,
        )

        self.assertEqual(
            settings.database_url.render_as_string(hide_password=False),
            "postgresql+psycopg://user%40example.com:p%40ss%2Fword@127.0.0.1:5432/quant_foundry",
        )


if __name__ == "__main__":
    unittest.main()
