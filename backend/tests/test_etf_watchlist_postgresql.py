"""Real PostgreSQL coverage for lateral lookups, persistence and retry races."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.models.etf_watchlist import EtfWatchlistEntry
from app.data_ingestion.repositories.etf_watchlist import EtfWatchlistRepository

pytestmark = pytest.mark.skipif(os.getenv("POSTGRES_TEST_ENABLED") != "1", reason="requires disposable PostgreSQL")


@pytest.fixture
def database():
    engine = create_engine(get_settings().database_url)
    owner = "test:" + uuid4().hex
    code = "T" + uuid4().hex[:10]
    try:
        yield engine, owner, code
    finally:
        with Session(engine) as session:
            session.execute(delete(EtfWatchlistEntry).where(EtfWatchlistEntry.ts_code == code))
            session.execute(delete(EtfDailyBar).where(EtfDailyBar.ts_code == code))
            session.commit()
        engine.dispose()


def test_concurrent_add_is_idempotent_and_owner_deletion_is_isolated(database):
    engine, owner, code = database
    def add():
        with Session(engine) as session:
            changed = EtfWatchlistRepository(session, owner).add(code)
            session.commit()
            return changed
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: add(), range(4)))
    assert sum(results) == 1
    with Session(engine) as session:
        other = EtfWatchlistRepository(session, owner + ":other")
        assert other.add(code)
        assert EtfWatchlistRepository(session, owner).remove(code)
        assert not EtfWatchlistRepository(session, owner).remove(code)
        session.commit()
    with Session(engine) as session:
        assert len(EtfWatchlistRepository(session, owner + ":other").list_entries(limit=50, offset=0)) == 1
        assert EtfWatchlistRepository(session, owner).list_entries(limit=50, offset=0) == []


def test_latest_invalid_bar_is_not_replaced_by_older_or_other_source_quote(database):
    engine, owner, code = database
    with Session(engine) as session:
        EtfWatchlistRepository(session, owner).add(code)
        session.add_all([
            EtfDailyBar(source="tushare", ts_code=code, trade_date=date(2026, 8, 27), close=Decimal("1.2")),
            EtfDailyBar(source="tushare", ts_code=code, trade_date=date(2026, 8, 28), close=None),
            EtfDailyBar(source="other", ts_code=code, trade_date=date(2026, 8, 29), close=Decimal("9")),
        ])
        session.commit()
    with Session(engine) as session:
        repo = EtfWatchlistRepository(session, owner)
        rows = repo.list_entries(limit=50, offset=0)
        assert len(rows) == 1
        assert rows[0]["trade_date"] == date(2026, 8, 28)
        assert rows[0]["close"] is None
        assert repo.list_entries(limit=50, offset=1) == []
        assert repo.remove(code)
        session.commit()
        assert len(session.scalars(select(EtfDailyBar).where(EtfDailyBar.ts_code == code)).all()) == 3
