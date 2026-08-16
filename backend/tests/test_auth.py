import asyncio
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.core.auth import require_api_token
from app.core.config import Settings
from app.db.session import get_db_session
from app.main import create_app


API_TOKEN = "a" * 64


async def request_status(app, path: str, api_token: str | None = None) -> int:
    headers = []
    if api_token is not None:
        headers.append((b"authorization", f"Bearer {api_token}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("test-client", 12345),
        "server": ("test-server", 80),
        "root_path": "",
    }
    request_sent = False
    messages = []

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Future()

    async def send(message: dict) -> None:
        messages.append(message)

    await app(scope, receive, send)
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    return response_start["status"]


class AuthenticationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app(
            Settings(
                api_token=API_TOKEN,
                database_password="test-secret",
                _env_file=None,
            )
        )
        self.request = Request({"type": "http", "app": self.app, "headers": []})

    def test_http_requests_enforce_token_while_docs_remain_public(self) -> None:
        self.app.dependency_overrides[get_db_session] = lambda: Mock()
        try:
            with patch("app.core.request_logging.logger"):
                self.assertEqual(asyncio.run(request_status(self.app, "/api")), 401)
                self.assertEqual(
                    asyncio.run(request_status(self.app, "/api", API_TOKEN)),
                    200,
                )
                self.assertEqual(
                    asyncio.run(
                        request_status(self.app, "/api/auth/verify", API_TOKEN)
                    ),
                    204,
                )
                self.assertEqual(
                    asyncio.run(
                        request_status(self.app, "/api/system/version", API_TOKEN)
                    ),
                    200,
                )
                self.assertEqual(asyncio.run(request_status(self.app, "/docs")), 200)
                self.assertEqual(asyncio.run(request_status(self.app, "/readyz")), 200)
        finally:
            self.app.dependency_overrides.clear()

    def test_accepts_matching_bearer_token_with_constant_time_comparison(self) -> None:
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=API_TOKEN,
        )

        with patch("app.core.auth.secrets.compare_digest", return_value=True) as compare:
            require_api_token(self.request, credentials)

        compare.assert_called_once_with(API_TOKEN.encode(), API_TOKEN.encode())

    def test_rejects_missing_malformed_and_invalid_tokens(self) -> None:
        cases = (
            None,
            HTTPAuthorizationCredentials(scheme="Basic", credentials=API_TOKEN),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token"),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="错误-token"),
        )

        for credentials in cases:
            with self.subTest(credentials=credentials):
                with self.assertRaises(HTTPException) as raised:
                    require_api_token(self.request, credentials)

                self.assertEqual(raised.exception.status_code, 401)
                self.assertEqual(
                    raised.exception.headers,
                    {"WWW-Authenticate": "Bearer"},
                )

    def test_protects_business_routes_and_exempts_readiness(self) -> None:
        protected_operations = (
            ("/api", "get"),
            ("/api/auth/verify", "get"),
            ("/api/system/version", "get"),
            ("/api/admin/logs", "get"),
            ("/api/admin/logs/clear", "post"),
            ("/api/admin/task-types", "get"),
            ("/api/admin/tasks", "get"),
            ("/api/admin/tasks", "post"),
            ("/api/admin/task-runs", "get"),
            ("/api/admin/data-collections/trading-calendar", "get"),
            ("/api/admin/data-collections/trading-calendar/overview", "get"),
            ("/api/admin/data-collections/etfs", "get"),
            ("/api/admin/data-collections/etfs/overview", "get"),
            ("/api/admin/data-collections/etfs/{ts_code}", "get"),
            ("/api/admin/data-collections/etfs/{ts_code}/daily-bars", "get"),
            ("/api/admin/data-collections/etfs/{ts_code}/adjustment-factors", "get"),
        )
        schema = self.app.openapi()

        for path, method in protected_operations:
            self.assertEqual(
                schema["paths"][path][method]["security"],
                [{"API Token": []}],
            )

        readiness_route = next(
            route for route in self.app.routes if getattr(route, "path", None) == "/readyz"
        )
        readiness_dependency_calls = {
            dependency.call for dependency in readiness_route.dependant.dependencies
        }
        self.assertNotIn(require_api_token, readiness_dependency_calls)

    def test_openapi_declares_bearer_authentication(self) -> None:
        schema = self.app.openapi()

        self.assertEqual(
            schema["components"]["securitySchemes"]["API Token"],
            {
                "type": "http",
                "description": "API token from the QF_API_TOKEN environment setting.",
                "scheme": "bearer",
                "bearerFormat": "opaque token",
            },
        )
        self.assertEqual(
            schema["paths"]["/api/admin/logs"]["get"]["security"],
            [{"API Token": []}],
        )


if __name__ == "__main__":
    unittest.main()
