"""Validate migrated comparison tables, ownership and real comparison projection."""
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.backtesting.models import BacktestRunRecord
from app.backtesting.result_router import compare_runs
from app.backtesting.comparison import BacktestComparison
from app.backtesting.saved_comparisons import ComparisonStore, CreateComparison, UpdateComparison
from app.core.auth import AuthenticatedPrincipal
from app.core.config import get_settings
from app.strategies.service import StrategyStorageService

pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")


def test_saved_comparison_uses_real_owned_runs_and_preserves_revision_alias():
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            owner = "comparison:" + uuid4().hex
            strategy = StrategyStorageService(session).create_strategy(name="比较测试", source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n")
            revision = StrategyStorageService(session).publish_revision(strategy.id, expected_draft_version=1, alias="优化止损")
            ids = [uuid4(), uuid4(), uuid4()]
            for i, identity in enumerate(ids):
                now = datetime.now(timezone.utc)
                session.execute(BacktestRunRecord.__table__.insert().values(id=identity, run_kind="backtest_run", profile="formal@1", status="succeeded", terminal_status="succeeded", finished_at=now, created_at=now, updated_at=now, idempotency_key=str(identity), config_hash="a"*64, tenant_id=owner, idempotency_scope=owner if i < 2 else owner + ":other", strategy_revision_id=str(revision.id)))
            store = ComparisonStore(session, owner)
            with pytest.raises(HTTPException) as error:
                store.create(CreateComparison(name="越权", run_ids=[ids[0], ids[2]], baseline_run_id=ids[0]))
            assert error.value.status_code == 404
            saved = store.create(CreateComparison(name="版本比较", run_ids=ids[:2], baseline_run_id=ids[1]))
            assert store.page(20, 0).total == 1
            assert ComparisonStore(session, owner + ":other").page(20, 0).total == 0
            request = Request({"type": "http", "headers": [], "state": {"authenticated_principal": AuthenticatedPrincipal(owner)}})
            comparison = BacktestComparison.model_validate(compare_runs({"run_ids": [str(i) for i in ids[:2]], "baseline_run_id": str(ids[1])}, session, request))
            assert comparison.baseline_run_id == str(ids[1])
            assert comparison.run_summaries[0].strategy_name == "比较测试"
            assert comparison.run_summaries[0].revision_alias == "优化止损"
            updated = store.update(saved.id, UpdateComparison(version=1, name="重命名"))
            assert updated.version == 2
            store.remove(saved.id, 2)
            assert store.page(20, 0).total == 0
            session.rollback()
    finally:
        engine.dispose()
