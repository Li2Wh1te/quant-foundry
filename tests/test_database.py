import unittest
from unittest.mock import Mock, patch

from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.session import dispose_engine, get_db_session, get_engine


class DatabaseTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        get_engine.cache_clear()

    def test_engine_uses_configured_database_url(self) -> None:
        settings = Settings(
            database_url="postgresql+psycopg://app:secret@db:5432/quant_test",
            _env_file=None,
        )

        with patch("app.db.session.get_settings", return_value=settings):
            engine = get_engine()

        self.assertEqual(engine.url, make_url(str(settings.database_url)))
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


if __name__ == "__main__":
    unittest.main()
