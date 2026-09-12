"""Watchlist isolation, price meaning and supplementary fact regression tests."""

from dataclasses import replace, asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.core.auth import AuthenticatedPrincipal
from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.data_ingestion.repositories.etf_watchlist import EtfWatchlistRepository
from app.data_ingestion.schemas.etf_daily import canonical_row_revision
from app.data_ingestion.watchlist_router import add_watchlist, present_item, remove_watchlist
from tests.test_etf_daily_repository import make_bar
from tests.test_auth import API_TOKEN, request_status
from app.core.config import Settings
from app.db.session import get_db_session
from app.main import create_app
from app.data_ingestion.services.etf_daily import normalize_etf_daily
from tests.test_etf_daily import make_dataframe


def quote(**changes):
    return dict(ts_code="510300.SH", csname="沪深300ETF", extname=None, cname=None,
                exchange="SH", created_at=datetime.now(UTC), trade_date=date(2026, 8, 28),
                close=Decimal("1.049"), pre_close=Decimal("1.043"),
                change=Decimal("0.006"), pct_chg=Decimal("0.5753"), **changes)


def test_price_uses_official_change_and_explicit_asof():
    item = present_item(quote())
    assert item.pct_chg == Decimal("0.5753")
    assert item.price_basis == "raw_daily_close"
    assert "2026-08-28" in item.message and "非实时" in item.message


def test_watchlist_route_requires_authentication_and_is_in_openapi():
    app = create_app(Settings(api_token=API_TOKEN, database_password="test-secret", _env_file=None))
    session = Mock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    app.dependency_overrides[get_db_session] = lambda: session
    with patch("app.core.request_logging.logger"):
        assert asyncio.run(request_status(app, "/api/admin/etf-watchlist")) == 401
        session.execute.assert_not_called()
        assert asyncio.run(request_status(app, "/api/admin/etf-watchlist", API_TOKEN)) == 200
    paths = app.openapi()["paths"]
    assert {"put", "delete"} <= paths["/api/admin/etf-watchlist/{ts_code}"].keys()


@pytest.mark.parametrize("field,value", [
    ("pre_close", None), ("pre_close", Decimal(0)),
    ("pct_chg", None), ("change", Decimal("NaN")),
    ("close", Decimal(-1)), ("close", None),
])
def test_missing_or_invalid_price_never_fabricates_change(field, value):
    row = quote()
    row[field] = value
    item = present_item(row)
    assert item.change is None and item.pct_chg is None
    if field == "close":
        assert item.close is None


def test_flat_market_is_not_missing():
    row = quote()
    row.update(change=Decimal(0), pct_chg=Decimal(0))
    assert present_item(row).pct_chg == 0


def test_no_bars_keeps_saved_instrument_visible():
    row = quote()
    row.update(trade_date=None, close=None, pre_close=None, change=None, pct_chg=None)
    assert "尚未采集" in present_item(row).message


def test_batch_query_is_scoped_and_does_not_skip_invalid_latest_rows():
    session = Mock()
    session.execute.return_value.mappings.return_value.all.return_value = []
    assert EtfWatchlistRepository(session, "owner-a").list_entries(limit=51, offset=50) == []
    statement = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "owner-a" in statement.params.values()
    assert "LEFT OUTER JOIN LATERAL" in str(statement)
    assert "trade_date DESC" in str(statement)
    assert "close IS NOT NULL" not in str(statement)
    assert session.execute.call_count == 1


def test_idempotent_insert_and_owner_scoped_removal():
    session = Mock()
    session.execute.return_value.scalar_one_or_none.return_value = "510300.SH"
    repo = EtfWatchlistRepository(session, "owner-a")
    assert repo.add("510300.SH")
    insert = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "ON CONFLICT DO NOTHING" in str(insert)
    assert insert.params["owner_scope"] == "owner-a"
    assert repo.remove("510300.SH")
    delete = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "owner-a" in delete.params.values()
    assert "etf_daily_bars" not in str(delete)
    session.execute.return_value.scalar_one_or_none.return_value = None
    assert not repo.remove("510300.SH")


def test_unknown_etf_cannot_be_saved():
    session = Mock()
    session.scalar.return_value = None
    with pytest.raises(HTTPException) as error:
        add_watchlist("510300.SH", session, AuthenticatedPrincipal("owner-a"))
    assert error.value.status_code == 404
    session.commit.assert_not_called()
    session.execute.assert_not_called()


def test_delete_commits_only_authenticated_relation():
    session = Mock()
    session.execute.return_value.scalar_one_or_none.return_value = None
    result = remove_watchlist("510300.SH", session, AuthenticatedPrincipal("owner-b"))
    assert result.changed is False
    assert "不在自选" in result.message
    session.commit.assert_called_once()


def test_display_backfill_preserves_bar_revision_and_original_timestamp():
    original = make_bar()
    enriched = replace(original, pre_close=Decimal("3.70"), change=Decimal("0.04"), pct_chg=Decimal("1.0811"))
    revision = canonical_row_revision(original, source="tushare")
    assert canonical_row_revision(enriched, source="tushare") == revision
    session = Mock()
    session.get.return_value = SimpleNamespace(**asdict(original), source_revision=revision)
    result = EtfDailyBarRepository(session).upsert_bars([enriched], source="tushare")
    assert result.changed == 1 and result.metadata_backfilled == 1
    assert session.execute.call_count == 1
    statement = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "updated_at=etf_daily_bars.updated_at" in str(statement)
    assert "source_revision" not in statement.params
    session.get.return_value = SimpleNamespace(**asdict(enriched), source_revision=revision)
    session.execute.reset_mock()
    result = EtfDailyBarRepository(session).upsert_bars([enriched], source="tushare")
    assert result.unchanged == 1
    session.execute.assert_not_called()


def test_provider_change_facts_are_read_without_recalculation():
    frame = make_dataframe()
    frame.to_dict.return_value[0].update(pre_close="3.70", change="0.04", pct_chg="1.0810811")
    bar = normalize_etf_daily(frame, expected_trade_date=date(2026, 8, 14))[0]
    assert bar.pre_close == Decimal("3.70")
    assert bar.change == Decimal("0.04")
    assert bar.pct_chg == Decimal("1.0810811")
    stored = replace(bar, pct_chg=Decimal("1.081081"))
    session = Mock()
    session.get.return_value = SimpleNamespace(**asdict(stored), source_revision=canonical_row_revision(bar, source="tushare"))
    result = EtfDailyBarRepository(session).upsert_bars([bar], source="tushare")
    assert result.unchanged == 1
    session.execute.assert_not_called()
