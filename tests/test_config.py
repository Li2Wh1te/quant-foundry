import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import Settings


class SettingsTestCase(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(database_password="test-secret", _env_file=None)

        self.assertEqual(settings.app_name, "Quant Foundry API")
        self.assertEqual(settings.environment, "local")
        self.assertFalse(settings.debug)
        self.assertEqual(str(settings.server_host), "127.0.0.1")
        self.assertEqual(settings.server_port, 8000)
        self.assertEqual(settings.log_dir.name, "logs")
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(settings.log_retention_days, 30)
        self.assertEqual(settings.log_queue_size, 10_000)
        self.assertEqual(settings.log_query_max_files, 32)
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
            "QF_LOG_DIR": "var/logs",
            "QF_LOG_LEVEL": "WARNING",
            "QF_LOG_RETENTION_DAYS": "14",
            "QF_LOG_QUEUE_SIZE": "5000",
            "QF_LOG_QUERY_MAX_FILES": "16",
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
        self.assertTrue(settings.log_dir.is_absolute())
        self.assertEqual(settings.log_dir.parts[-2:], ("var", "logs"))
        self.assertEqual(settings.log_level, "WARNING")
        self.assertEqual(settings.log_retention_days, 14)
        self.assertEqual(settings.log_queue_size, 5000)
        self.assertEqual(settings.log_query_max_files, 16)
        self.assertEqual(
            settings.database_url.render_as_string(hide_password=False),
            "postgresql+psycopg://app:secret@db:5433/quant_test",
        )

    def test_invalid_environment_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"QF_ENVIRONMENT": "invalid", "QF_DATABASE_PASSWORD": "test-secret"},
            clear=True,
        ):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)

    def test_invalid_server_settings_are_rejected(self) -> None:
        invalid_settings = (
            {"QF_SERVER_HOST": "not-an-ip"},
            {"QF_SERVER_PORT": "0"},
            {"QF_SERVER_PORT": "65536"},
        )

        for environment in invalid_settings:
            with self.subTest(environment=environment):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValidationError):
                        Settings(database_password="test-secret", _env_file=None)

    def test_database_password_is_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)

    def test_database_url_encodes_credentials(self) -> None:
        settings = Settings(
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
