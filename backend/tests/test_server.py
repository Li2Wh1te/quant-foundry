import asyncio
import unittest
from unittest.mock import patch

from app.__main__ import main
from app.core.config import Settings
from app.core.version import get_release_version
from app.main import create_app


API_TOKEN = "a" * 64


class ServerTestCase(unittest.TestCase):
    def test_openapi_uses_the_canonical_release_version(self) -> None:
        app = create_app(
            Settings(
                api_token=API_TOKEN,
                database_password="test-secret",
                _env_file=None,
            )
        )

        self.assertEqual(app.version, get_release_version())
        self.assertEqual(app.openapi()["info"]["version"], get_release_version())

    def test_server_uses_configured_host_and_port(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            server_host="0.0.0.0",
            server_port=9000,
            database_password="test-secret",
            _env_file=None,
        )

        with (
            patch("app.__main__.get_settings", return_value=settings),
            patch("app.__main__.uvicorn.run") as run,
        ):
            main()

        run.assert_called_once_with(
            "app.main:app",
            host="0.0.0.0",
            port=9000,
            access_log=False,
        )

    def test_lifespan_configures_and_stops_logging(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            _env_file=None,
        )
        app = create_app(settings)

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                pass

        with (
            patch("app.main.configure_logging") as configure_logging,
            patch("app.main.dispose_engine") as dispose_engine,
            patch("app.main.SchedulerRuntime") as scheduler_runtime,
        ):
            asyncio.run(run_lifespan())

        configure_logging.assert_called_once_with(settings)
        configure_logging.return_value.stop.assert_called_once_with()
        scheduler_runtime.assert_called_once_with(settings)
        scheduler_runtime.return_value.start.assert_called_once_with()
        scheduler_runtime.return_value.stop.assert_called_once_with()
        dispose_engine.assert_called_once_with()

    def test_lifespan_cleans_up_when_scheduler_start_fails(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            _env_file=None,
        )
        app = create_app(settings)

        async def run_lifespan() -> None:
            async with app.router.lifespan_context(app):
                self.fail("lifespan should not yield after scheduler startup failure")

        with (
            patch("app.main.configure_logging") as configure_logging,
            patch("app.main.dispose_engine") as dispose_engine,
            patch("app.main.SchedulerRuntime") as scheduler_runtime,
        ):
            scheduler_runtime.return_value.start.side_effect = RuntimeError(
                "database is not migrated"
            )
            with self.assertRaisesRegex(RuntimeError, "database is not migrated"):
                asyncio.run(run_lifespan())

        scheduler_runtime.return_value.stop.assert_called_once_with()
        dispose_engine.assert_called_once_with()
        configure_logging.return_value.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
