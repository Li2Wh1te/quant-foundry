"""Permanent deletion must retain every referenced account across run scopes."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.backtest_service.accounts.types import AccountProfileStatus
from app.backtest_service.history.storage_models import BacktestAccountProfileRecord, BacktestAccountProfileVersionRecord, BacktestRunRecord
from app.backtest_service.accounts.service import AccountProfileService, AccountProfileReferencedError, AccountProfileVersionConflictError, AccountProfileNotFoundError
from app.core.config import get_settings
from tests.test_backtesting_account_profile_storage import fee_schedule_payload

pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")


def create_account(session):
    return AccountProfileService(session).create(name="delete-test-" + uuid4().hex,
        status=AccountProfileStatus.ACTIVE, fee_schedule=fee_schedule_payload(), metadata={})


def insert_run(session, profile_id, *, kind="backtest_run"):
    run_id = uuid4()
    session.execute(BacktestRunRecord.__table__.insert().values(id=run_id,
        run_kind=kind, profile="formal@1" if kind == "backtest_run" else "internal_link_acceptance@1",
        status="queued", idempotency_key=str(run_id), config_hash="a" * 64,
        tenant_id="different-owner", idempotency_scope="different-owner",
        account_profile_id=str(profile_id), account_profile_version="1"))
    return run_id


def test_unused_versions_can_only_be_deleted_through_explicit_service_path():
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            service = AccountProfileService(session)
            account = create_account(session)
            aid = account.id
            service.update(aid, expected_version=1, metadata={"v": "2"})
            assert not service.has_run_references(aid)
            with pytest.raises(AccountProfileVersionConflictError):
                service.permanently_delete(aid, expected_version=1)
            # The exception is explicit for whole unused catalogues; arbitrary
            # SQL deletion of even an unused individual version stays forbidden.
            with pytest.raises(DBAPIError):
                with session.begin_nested():
                    session.execute(delete(BacktestAccountProfileVersionRecord).where(BacktestAccountProfileVersionRecord.profile_id == aid))
            service.permanently_delete(aid, expected_version=2)
            assert session.scalar(select(BacktestAccountProfileRecord.id).where(BacktestAccountProfileRecord.id == aid)) is None
            assert session.scalar(select(BacktestAccountProfileVersionRecord.profile_id).where(BacktestAccountProfileVersionRecord.profile_id == aid)) is None
            with pytest.raises(AccountProfileNotFoundError):
                service.get_version(aid, 2)
            session.rollback()
    finally:
        engine.dispose()
@pytest.mark.parametrize("kind", ["backtest_run", "internal_link_acceptance"])
def test_any_owner_or_run_kind_prevents_deletion_but_allows_retirement(kind):
    engine = create_engine(get_settings().database_url)
    try:
        with Session(engine) as session:
            service = AccountProfileService(session)
            account = create_account(session)
            insert_run(session, account.id, kind=kind)
            assert service.has_run_references(account.id)
            with pytest.raises(AccountProfileReferencedError):
                service.permanently_delete(account.id, expected_version=1)
            service.update(account.id, expected_version=1, status=AccountProfileStatus.RETIRED)
            assert len(service.versions(account.id)) == 2
            assert service.get_version(account.id, 1).name == account.name
            session.rollback()
    finally:
        engine.dispose()
