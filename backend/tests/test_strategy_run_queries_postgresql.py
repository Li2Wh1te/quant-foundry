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
            session.rollback()
    finally:
        engine.dispose()
