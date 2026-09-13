"""Account workspace aggregation against a disposable PostgreSQL database."""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.account_profiles import AccountProfileStatus
from app.backtesting.account_queries import AccountProfileQueries
from app.backtesting.models import BacktestRunRecord
from app.backtesting.schemas import AccountProfileOverviewResponse, AccountProfileUsageResponse
from app.backtesting.service import AccountProfileService, AccountProfileVersionConflictError
from app.core.config import get_settings
from app.strategies.service import StrategyStorageService
from tests.test_backtesting_account_profile_storage import fee_schedule_payload

pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")


def test_catalogue_counts_history_scope_and_literal_pagination():
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            query = AccountProfileQueries(session)
            owner = "account-test:" + uuid4().hex
            before = query.overview(owner_scope=owner)
            assert before["related_strategy_versions"] == before["used_accounts"] == 0
            service = AccountProfileService(session)
            prefix = uuid4().hex
            accounts = []
            for index, state in enumerate(AccountProfileStatus):
                schedule = fee_schedule_payload()
                schedule["key"] = prefix + "_fee%" + str(index)
                accounts.append(service.create(name=prefix + " account " + str(index),
                    status=state, fee_schedule=schedule, metadata={}))
            account = accounts[0]
            empty = query.usage(account.id, owner_scope=owner, limit=1, offset=0)
            assert empty["items"] == [] and empty["total"] == empty["total_runs"] == 0
            service.update(account.id, expected_version=1, metadata={"owner": "new"})
            assert service.get_version(account.id, 1).profile_metadata == {}
            assert service.get_version(account.id, 2).profile_metadata == {"owner": "new"}
            with pytest.raises(AccountProfileVersionConflictError):
                service.update(account.id, expected_version=1, name="stale")
            strategies = StrategyStorageService(session)
            strategy = strategies.create_strategy(name=prefix, source_code="def run(context, parameters):\n    return {'mode': 'hold'}\n")
            revision = strategies.publish_revision(strategy.id, expected_draft_version=1)
            now = datetime.now(timezone.utc)
            latest = None
            fixtures = [
                (account.id, "1", str(revision.id), owner, "backtest_run", "queued"),
                (account.id, "1", str(revision.id), owner, "backtest_run", "failed"),
                (account.id, "2", str(revision.id), owner, "backtest_run", "cancelled"),
                (account.id, "legacy", "not-a-uuid", owner, "backtest_run", "queued"),
                (account.id, None, None, owner, "backtest_run", "queued"),
                (account.id, "1", str(revision.id), owner + "other", "backtest_run", "queued"),
                (account.id, "1", str(revision.id), owner, "internal_link_acceptance", "queued"),
                (uuid4(), "1", "orphan", owner, "backtest_run", "queued"),
            ]
            for index, (aid, version, rid, scope, kind, state) in enumerate(fixtures):
                run_id = uuid4()
                if index == 1:
                    latest = run_id
                terminal = state in {"failed", "cancelled"}
                session.execute(BacktestRunRecord.__table__.insert().values(
                    id=run_id, run_kind=kind, profile="formal@1" if kind == "backtest_run" else "internal_link_acceptance@1",
                    status=state, terminal_status=state if terminal else None,
                    finished_at=now + timedelta(seconds=index) if terminal else None,
                    created_at=now + timedelta(seconds=index), idempotency_key=str(run_id), config_hash="a" * 64,
                    tenant_id=scope, idempotency_scope=scope, strategy_revision_id=rid,
                    account_profile_id=str(aid), account_profile_version=version,
                ))
            overview = AccountProfileOverviewResponse.model_validate(query.overview(owner_scope=owner))
            assert overview.total_accounts == before["total_accounts"] + 3
            assert overview.total_fee_rules == before["total_fee_rules"] + 3
            for state in ("active", "inactive", "retired"):
                assert getattr(overview, state + "_accounts") == before[state + "_accounts"] + 1
            assert overview.related_strategy_versions == 2 and overview.used_accounts == 1
            usage = AccountProfileUsageResponse.model_validate(query.usage(account.id, owner_scope=owner, limit=10, offset=0))
            assert usage.total == 4 and usage.total_runs == 5 and usage.related_strategy_versions == 2
            first_version = next(row for row in usage.items if row.account_profile_version == "1")
            assert first_version.run_count == 2 and first_version.latest_run_id == latest
            assert first_version.strategy_name == prefix and first_version.revision_number == 1
            assert first_version.latest_run_status == "failed"
            unresolved = next(row for row in usage.items if row.account_profile_version == "legacy")
            assert unresolved.strategy_id is None and unresolved.strategy_revision_id == "not-a-uuid"
            paged = query.usage(account.id, owner_scope=owner, limit=1, offset=1)
            assert paged["total"] == 4 and paged["items"][0]["latest_run_id"] == usage.items[1].latest_run_id
            for keyword in (prefix.upper(), prefix + "_fee%"):
                rows, total = query.page(status=None, keyword=keyword, limit=1, offset=1)
                assert total == 3 and len(rows) == 1
            rows, total = query.page(status="inactive", keyword=prefix, limit=10, offset=0)
            assert total == 1 and rows[0].status == "inactive"
            assert query.page(status=None, keyword=prefix + "%", limit=10, offset=0)[1] == 0
            assert query.page(status=None, keyword=prefix, limit=1, offset=99) == ([], 3)
            # Exercise route matching, authenticated scope, response models and
            # pagination validation through ASGI without an external HTTP client.
            import asyncio
            import json
            from fastapi import FastAPI
            from app.backtesting.router import router
            from app.core.auth import AuthenticatedPrincipal
            from app.db.session import get_db_session
            app = FastAPI()
            app.include_router(router)
            app.dependency_overrides[get_db_session] = lambda: session

            async def get(path, query_string=b""):
                messages = []
                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}
                async def send(message):
                    messages.append(message)
                await app({
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "GET", "scheme": "http", "path": path, "root_path": "",
                    "query_string": query_string, "headers": [],
                    "server": ("test", 80), "client": ("127.0.0.1", 1234),
                    "state": {"authenticated_principal": AuthenticatedPrincipal(owner)},
                }, receive, send)
                status = next(m["status"] for m in messages if m["type"] == "http.response.start")
                body = json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
                return status, body

            base = "/api/admin/backtest-account-profiles"
            status, body = asyncio.run(get(base + "/overview"))
            assert status == 200 and body["related_strategy_versions"] == 2
            status, body = asyncio.run(get(base + "/page", ("keyword=" + prefix + "&limit=1").encode()))
            assert status == 200 and body["total"] == 3 and len(body["items"]) == 1
            status, body = asyncio.run(get(base + f"/{account.id}/usage", b"limit=1&offset=1"))
            assert status == 200 and body["total_runs"] == 5 and len(body["items"]) == 1
            assert asyncio.run(get(base + f"/{uuid4()}/usage"))[0] == 404
            assert asyncio.run(get(base + "/page", b"status=draft"))[0] == 422
            assert asyncio.run(get(base + "/page", b"limit=0"))[0] == 422
            session.rollback()
    finally:
        engine.dispose()
