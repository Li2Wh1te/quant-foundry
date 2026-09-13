"""Validate the workbench HTTP contract before repository execution."""
import asyncio
from unittest.mock import patch

from fastapi import FastAPI
import pytest

from app.backtesting.run_router import router
from app.db.session import get_db_session


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "status=unknown", "strategy_id=bad", "search=" + "x" * 201])
def test_invalid_workspace_query_never_reaches_repository(query):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: object()

    async def invoke():
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                   "method": "GET", "scheme": "http", "path": "/api/admin/backtest-runs/workspace",
                   "root_path": "", "query_string": query.encode(), "headers": [],
                   "server": ("test", 80), "client": ("127.0.0.1", 1234)}, receive, send)
        return next(m["status"] for m in messages if m["type"] == "http.response.start")
    with patch("app.backtesting.run_router.DatabaseRunRepository") as repository:
        assert asyncio.run(invoke()) == 422
        repository.assert_not_called()
