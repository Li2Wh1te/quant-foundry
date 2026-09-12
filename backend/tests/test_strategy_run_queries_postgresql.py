"""Regression coverage for UUID strategy revisions joined to textual run bindings."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.strategies.service import StrategyStorageService
from app.backtesting.models import BacktestRunRecord
from app.backtesting.run_repository import DatabaseRunRepository

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL"
)


def test_strategy_run_queries_preserve_scope_and_cursor_with_text_bindings():
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            # All fixture writes roll back, including immutable revision records.
            service = StrategyStorageService(session)
            strategy = service.create_strategy(
                name="UUID query regression", source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n"
            )
            revision = service.publish_revision(strategy.id, expected_draft_version=1)
            other = service.create_strategy(
                name="Other strategy", source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n"
            )
            other_revision = service.publish_revision(other.id, expected_draft_version=1)
            owner = "test:" + uuid4().hex
            matching = []
            for binding, scope in [
                (str(revision.id), owner), (str(revision.id), owner),
                (str(other_revision.id), owner), (str(revision.id), owner + ":other"),
                ("legacy-not-a-uuid", owner), (None, owner),
            ]:
                run_id = uuid4()
                session.execute(BacktestRunRecord.__table__.insert().values(
                    id=run_id, run_kind="backtest_run", profile="formal@1",
                    status="queued", idempotency_key=str(run_id), config_hash="a" * 64,
                    tenant_id=scope, idempotency_scope=scope, strategy_revision_id=binding,
                ))
                if binding == str(revision.id) and scope == owner:
                    matching.append(run_id)
            session.flush()
            repo = DatabaseRunRepository(session)
            assert {r.id for r in repo.list(strategy_id=str(strategy.id), owner_scope=owner)} == set(matching)
            assert len(repo.list(strategy_id=str(strategy.id), owner_scope=owner, limit=1, offset=1)) == 1
            args = dict(strategy_id=str(strategy.id), owner_scope=owner, limit=1, signing_key="test-cursor-key-at-least-thirty-two-characters")
            first = repo.list_page(**args)
            assert first.has_more and first.next_cursor
            second = repo.list_page(**args, cursor=first.next_cursor)
            assert not second.has_more
            assert {first.items[0].id, second.items[0].id} == set(matching)
            assert repo.list(strategy_id=str(uuid4()), owner_scope=owner) == []
            assert not repo.list_page(**{**args, "strategy_id": str(uuid4())}).items
            # Exercise the whole HTTP response, not only repository SQL.
            from fastapi import FastAPI
            import asyncio
            import json
            from app.strategies.router import router
            from app.db.session import get_db_session
            from app.backtesting.run_router import _cursor_signing_key
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db_session] = lambda: session
            app.dependency_overrides[_cursor_signing_key] = lambda: args["signing_key"]
            async def request_workspace():
                messages = []
                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}
                async def send(message):
                    messages.append(message)
                await app({
                    "type": "http", "asgi": {"version": "3.0"},
                    "http_version": "1.1", "method": "GET", "scheme": "http",
                    "path": f"/api/admin/strategies/{strategy.id}/backtests",
                    "root_path": "", "query_string": b"", "headers": [],
                    "server": ("test", 80), "client": ("127.0.0.1", 1234),
                }, receive, send)
                assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 200
                return json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
            payload = asyncio.run(request_workspace())
            assert payload["published_revisions"][0]["id"] == str(revision.id)
            assert payload["component_options"]["execution_model"][0]["parameter_schema"]["properties"]
            session.rollback()
    finally:
        engine.dispose()
