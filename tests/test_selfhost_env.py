import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.selfhost_env import (
    API_TOKEN_KEY,
    DATABASE_PASSWORD_KEY,
    ensure_selfhost_environment,
)


class SelfhostEnvironmentTestCase(unittest.TestCase):
    def test_generates_secrets_and_preserves_them_on_subsequent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            template = root / ".env.example"
            env = root / ".env"
            template.write_text(
                "QF_APP_NAME=Test\nQF_API_TOKEN=\nQF_DATABASE_PASSWORD=\n",
                encoding="utf-8",
            )

            with patch(
                "scripts.selfhost_env.secrets.token_hex",
                side_effect=("a" * 64, "b" * 64),
            ):
                generated = ensure_selfhost_environment(env, template)
            first_content = env.read_text(encoding="utf-8")
            generated_again = ensure_selfhost_environment(env, template)

            self.assertEqual(generated, {DATABASE_PASSWORD_KEY, API_TOKEN_KEY})
            self.assertEqual(generated_again, set())
            self.assertEqual(first_content, env.read_text(encoding="utf-8"))
            self.assertIn(f"QF_DATABASE_PASSWORD={'a' * 64}", first_content)
            self.assertIn(f"QF_API_TOKEN={'b' * 64}", first_content)
            self.assertIn("QF_WEB_PORT=8080", first_content)
            self.assertEqual(os.stat(env).st_mode & 0o777, 0o600)

    def test_replaces_weak_secrets_and_removes_legacy_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            template = root / ".env.example"
            env = root / ".env"
            template.write_text("", encoding="utf-8")
            env.write_text(
                "QF_DATABASE_URL=postgresql+psycopg://postgres:postgres@db/test\n"
                "QF_DATABASE_PASSWORD=postgres\n"
                "QF_API_TOKEN=short-token\n"
                "QF_WEB_PORT=9090\n",
                encoding="utf-8",
            )

            with patch(
                "scripts.selfhost_env.secrets.token_hex",
                side_effect=("b" * 64, "c" * 64),
            ):
                generated = ensure_selfhost_environment(env, template)
            content = env.read_text(encoding="utf-8")

            self.assertEqual(generated, {DATABASE_PASSWORD_KEY, API_TOKEN_KEY})
            self.assertNotIn("QF_DATABASE_URL", content)
            self.assertIn(f"QF_DATABASE_PASSWORD={'b' * 64}", content)
            self.assertIn(f"QF_API_TOKEN={'c' * 64}", content)
            self.assertIn("QF_WEB_PORT=9090", content)


if __name__ == "__main__":
    unittest.main()
