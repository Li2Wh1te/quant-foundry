import os


# Importing app.main constructs the production ASGI application immediately.
# These deterministic, non-sensitive values keep test discovery independent of
# a developer's local .env file while individual tests still override settings.
os.environ.setdefault("QF_API_TOKEN", "test-api-token-" + "a" * 48)
os.environ.setdefault("QF_DATABASE_PASSWORD", "test-database-password")
os.environ.setdefault("QF_CURSOR_SIGNING_KEY", "test-cursor-signing-key-" + "b" * 40)
