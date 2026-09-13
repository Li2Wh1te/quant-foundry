"""Global workbench pagination must retain isolation and legacy run bindings."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.backtesting.models import BacktestRunRecord
from app.backtesting.run_repository import DatabaseRunRepository
from app.backtesting.run_router import run_workspace
from app.core.auth import AuthenticatedPrincipal
from app.core.config import get_settings
from app.strategies.service import StrategyStorageService

pytestmark = pytest.mark.skipif(
    os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL"
)


def test_workspace_filters_before_pagination_and_keeps_legacy_bindings():
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            # Fixture rows and immutable revisions are rolled back together.
            strategy = StrategyStorageService(session).create_strategy(
                name="轮动_% Literal", source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n",
            )
            revision = StrategyStorageService(session).publish_revision(strategy.id, expected_draft_version=1)
            owner = "workspace:" + uuid4().hex
            ids = []
            for index, (binding, scope, kind, status) in enumerate([
                (str(revision.id), owner, "backtest_run", "queued"),
                (str(revision.id), owner, "backtest_run", "starting"),
                ("legacy-not-a-uuid", owner, "backtest_run", "queued"),
                (None, owner, "backtest_run", "queued"),
                (str(revision.id), owner + ":other", "backtest_run", "queued"),
                (str(revision.id), owner, "internal_link_acceptance", "queued"),
            ]):
                run_id = uuid4()
                ids.append(run_id)
                created = datetime.now(timezone.utc) - timedelta(days=10-index)
                session.execute(BacktestRunRecord.__table__.insert().values(
                    id=run_id, run_kind=kind, profile="formal@1" if kind == "backtest_run" else "internal_link_acceptance@1",
                    status=status, idempotency_key=str(run_id), config_hash="a" * 64,
                    tenant_id=scope, idempotency_scope=scope, strategy_revision_id=binding,
                    created_at=created,
                ))
            repo = DatabaseRunRepository(session)
            rows, total = repo.workspace_page(owner_scope=owner, limit=2)
            assert total == 4 and [r[0].id for r in rows] == [ids[3], ids[2]]
            assert rows[0].strategy_name is None
            rows, total = repo.workspace_page(owner_scope=owner, limit=2, offset=2)
            assert total == 4 and [r[0].id for r in rows] == [ids[1], ids[0]]
            assert rows[0].strategy_name == strategy.name and rows[0].revision_number == 1
            assert repo.workspace_page(owner_scope=owner, offset=10) == ([], 4)
            for term in ("轮动", "_%", "literal"):
                assert repo.workspace_page(owner_scope=owner, search=term)[1] == 2
            assert repo.workspace_page(owner_scope=owner, search="missing")[1] == 0
            assert repo.workspace_page(owner_scope=owner, search=str(ids[2])[:12])[1] == 1
            assert repo.workspace_page(owner_scope=owner, search="轮动", status="queued")[1] == 1
            assert repo.workspace_page(owner_scope=owner, strategy_id=strategy.id)[1] == 2
            assert repo.workspace_page(owner_scope=owner + ":absent")[1] == 0
            request = Request({"type": "http", "headers": [], "state": {
                "authenticated_principal": AuthenticatedPrincipal(owner),
            }})
            page = run_workspace(session=session, request=request, limit=2, offset=0, search=None)
            assert page.has_more and page.total == 4
            assert page.items[0].strategy_revision_id is None
            page = run_workspace(session=session, request=request, limit=2, offset=2, search=None)
            assert not page.has_more and page.items[0].strategy_id == strategy.id
            session.rollback()
    finally:
        engine.dispose()
