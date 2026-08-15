import asyncio
import unittest
from unittest.mock import patch

from app.__main__ import main
from app.core.config import Settings
from app.main import create_app


API_TOKEN = "a" * 64


class ServerTestCase(unittest.TestCase):
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
        ):
            asyncio.run(run_lifespan())

        configure_logging.assert_called_once_with(settings)
        configure_logging.return_value.stop.assert_called_once_with()
        dispose_engine.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
