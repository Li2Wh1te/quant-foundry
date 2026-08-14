import unittest
from unittest.mock import Mock, patch

from app.core.config import Settings
from app.db.session import dispose_engine, get_db_session, get_engine
from app.main import check_database_ready


API_TOKEN = "a" * 64


class DatabaseTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        get_engine.cache_clear()

    def test_engine_uses_configured_database_url(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_host="db",
            database_user="app",
            database_password="secret",
            database_name="quant_test",
            _env_file=None,
        )

        with patch("app.db.session.get_settings", return_value=settings):
            engine = get_engine()

        self.assertEqual(engine.url, settings.database_url)
        engine.dispose()

    def test_session_dependency_closes_session(self) -> None:
        session = Mock()
        context_manager = Mock()
        context_manager.__enter__ = Mock(return_value=session)
        context_manager.__exit__ = Mock(return_value=False)

        with patch("app.db.session.Session", return_value=context_manager):
            dependency = get_db_session()
            self.assertIs(next(dependency), session)
            dependency.close()

        context_manager.__exit__.assert_called_once()

    def test_dispose_engine_does_not_create_unused_engine(self) -> None:
        with patch("app.db.session.create_engine") as create_engine:
            dispose_engine()

        create_engine.assert_not_called()

    def test_readiness_checks_database_connection(self) -> None:
        session = Mock()

        response = check_database_ready(session)

        statement = session.execute.call_args.args[0]
        self.assertEqual(str(statement), "SELECT 1")
        self.assertEqual(response, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
