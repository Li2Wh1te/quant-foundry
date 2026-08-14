import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.database_env import ensure_database_environment


class DatabaseEnvironmentTestCase(unittest.TestCase):
    def test_generates_password_and_preserves_it_on_subsequent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            template = root / ".env.example"
            env = root / ".env"
            template.write_text(
                "QF_APP_NAME=Test\nQF_DATABASE_PASSWORD=\n",
                encoding="utf-8",
            )

            with patch("scripts.database_env.secrets.token_hex", return_value="a" * 64):
                generated = ensure_database_environment(env, template)
            first_content = env.read_text(encoding="utf-8")
            generated_again = ensure_database_environment(env, template)

            self.assertTrue(generated)
            self.assertFalse(generated_again)
            self.assertEqual(first_content, env.read_text(encoding="utf-8"))
            self.assertIn(f"QF_DATABASE_PASSWORD={'a' * 64}", first_content)
            self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)

    def test_replaces_weak_password_and_removes_legacy_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            template = root / ".env.example"
            env = root / ".env"
            template.write_text("", encoding="utf-8")
            env.write_text(
                "QF_DATABASE_URL=postgresql+psycopg://postgres:postgres@db/test\n"
                "QF_DATABASE_PASSWORD=postgres\n",
                encoding="utf-8",
            )

            with patch("scripts.database_env.secrets.token_hex", return_value="b" * 64):
                generated = ensure_database_environment(env, template)
            content = env.read_text(encoding="utf-8")

            self.assertTrue(generated)
            self.assertNotIn("QF_DATABASE_URL", content)
            self.assertIn(f"QF_DATABASE_PASSWORD={'b' * 64}", content)


if __name__ == "__main__":
    unittest.main()
