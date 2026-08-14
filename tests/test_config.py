import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import Settings


class SettingsTestCase(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.app_name, "Quant Foundry API")
        self.assertEqual(settings.environment, "local")
        self.assertFalse(settings.debug)
        self.assertEqual(str(settings.server_host), "127.0.0.1")
        self.assertEqual(settings.server_port, 8000)
        self.assertEqual(
            str(settings.database_url),
            "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/quant_foundry",
        )

    def test_environment_variables_override_defaults(self) -> None:
        environment = {
            "QF_APP_NAME": "Test API",
            "QF_ENVIRONMENT": "test",
            "QF_DEBUG": "true",
            "QF_SERVER_HOST": "0.0.0.0",
            "QF_SERVER_PORT": "9000",
            "QF_DATABASE_URL": "postgresql+psycopg://app:secret@db:5432/quant_test",
        }

        with patch.dict(os.environ, environment, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.app_name, "Test API")
        self.assertEqual(settings.environment, "test")
        self.assertTrue(settings.debug)
        self.assertEqual(str(settings.server_host), "0.0.0.0")
        self.assertEqual(settings.server_port, 9000)
        self.assertEqual(
            str(settings.database_url),
            "postgresql+psycopg://app:secret@db:5432/quant_test",
        )

    def test_invalid_environment_is_rejected(self) -> None:
        with patch.dict(os.environ, {"QF_ENVIRONMENT": "invalid"}, clear=True):
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
                        Settings(_env_file=None)

    def test_non_postgresql_database_url_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"QF_DATABASE_URL": "sqlite:///local.db"},
            clear=True,
        ):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)

    def test_database_url_without_psycopg_driver_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"QF_DATABASE_URL": "postgresql://app:secret@db:5432/quant_test"},
            clear=True,
        ):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)


if __name__ == "__main__":
    unittest.main()
