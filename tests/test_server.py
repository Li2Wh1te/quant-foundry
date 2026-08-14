import unittest
from unittest.mock import patch

from app.__main__ import main
from app.core.config import Settings


class ServerTestCase(unittest.TestCase):
    def test_server_uses_configured_host_and_port(self) -> None:
        settings = Settings(
            server_host="0.0.0.0",
            server_port=9000,
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
        )


if __name__ == "__main__":
    unittest.main()
