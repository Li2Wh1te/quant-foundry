"""Run market workspace queries against real rows, using PostgreSQL in CI.

Local column mirrors follow the existing overview tests because checkpoint
constraints use PostgreSQL JSON operators. PostgreSQL uses the migrated schema;
fixtures are isolated by source/exchange and rolled back after every test.
"""

import os
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import Column, JSON, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data_ingestion.models.etf import EtfCode, EtfEntity
from app.data_ingestion.models.sync_checkpoint import DataSyncCheckpoint
from app.data_ingestion.models.trading_calendar import TradingCalendarDay
from app.data_ingestion.repositories.etf_query import EtfQueryRepository
from app.data_ingestion.router import (
    get_etf_overview,
    get_trading_calendar_day,
    list_etfs,
    list_trading_calendar_days,
)
from app.instruments.models import Instrument


NOW = datetime(2026, 9, 10, tzinfo=UTC)


class MarketWorkspaceTest(unittest.TestCase):
    def setUp(self):
        postgres = os.getenv("POSTGRES_TEST_ENABLED") == "1"
        self.engine = create_engine(get_settings().database_url if postgres else "sqlite://")
        if not postgres:
            metadata = MetaData()
            for model in (Instrument, EtfEntity, EtfCode, TradingCalendarDay, DataSyncCheckpoint):
                Table(model.__tablename__, metadata, *[
                    Column(column.name, JSON() if isinstance(column.type, JSONB) else column.type,
                           primary_key=column.primary_key)
                    for column in model.__table__.columns
                ])
            metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session = Session(self.connection)
        self.source = "market-test." + uuid4().hex[:16]
        self.exchange = "T" + uuid4().hex[:10]
        self.repository = EtfQueryRepository(self.session, source=self.source)

    def tearDown(self):
        self.session.close()
        if self.transaction.is_active:
            self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def etf(self, code, **fields):
        identity = uuid4()
        self.session.add(Instrument(id=identity, asset_class="etf", status="active", created_at=NOW))
        self.session.flush()
        self.session.add(EtfEntity(id=identity, created_at=NOW))
        self.session.flush()
        values = dict(source=self.source, ts_code=code, etf_id=identity,
                      exchange="SH", list_status="L", list_date=date(2026, 9, 1),
                      created_at=NOW, updated_at=NOW, first_seen_at=NOW, last_seen_at=NOW)
        values.update(fields)
        result = EtfCode(**values)
        self.session.add(result)
        self.session.flush()
        return result

    def query(self, **filters):
        options = dict(keyword=None, exchange=None, list_status=None, limit=50, offset=0)
        options.update(filters)
        # Exercise response serialization and the actual query while isolating
        # fixture totals from any other source in the disposable PostgreSQL DB.
        with patch("app.data_ingestion.router.EtfQueryRepository", return_value=self.repository):
            return list_etfs(session=self.session, **options)

    def calendar(self, day, *, open=False, exchange=None, previous=None):
        result = TradingCalendarDay(exchange=exchange or self.exchange, calendar_date=day,
            is_open=open, previous_trading_date=previous, created_at=NOW, updated_at=NOW)
        self.session.add(result)
        self.session.flush()
        return result

    def detail(self, day):
        return get_trading_calendar_day(self.exchange, day, self.session)

    def test_search_matches_index_manager_and_existing_name_fields(self):
        self.etf("510001.SH", csname="基金简称", extname="扩展名称", cname="基金完整名称",
                 index_name="红利质量", index_code="CSI.DIV", mgr_name="测试管理人")
        self.etf("510002.SH", csname="无关基金")
        self.etf("510003.SH", source=self.source + "x", index_name="红利质量")
        for query in ("510001.sh", "基金简称", "扩展名称", "完整名称", " 红利质量 ", "csi.div", "测试管理人"):
            with self.subTest(query=query):
                result = self.query(keyword=query)
                self.assertEqual(result.total, 1)
                self.assertEqual([item.ts_code for item in result.items], ["510001.SH"])

    def test_search_keeps_exchange_status_and_paginated_totals_consistent(self):
        self.etf("510001.SH", index_name="红利", exchange="SH", list_status="P")
        self.etf("510002.SH", index_name="红利", exchange="SSE", list_status="P")
        self.etf("159001.SZ", index_name="红利", exchange="SZ", list_status="P")
        self.etf("510003.SH", index_name="红利", exchange="SH", list_status="D")
        result = self.query(keyword="红利", exchange="SSE", list_status="P", limit=1, offset=1)
        self.assertEqual((result.total, result.limit, result.offset), (2, 1, 1))
        self.assertEqual([item.ts_code for item in result.items], ["510002.SH"])
        self.assertEqual(self.query(keyword="不存在").total, 0)

    def test_search_treats_percent_underscore_and_escape_as_literal_text(self):
        self.etf("510001.SH", index_name="A%_B/C")
        self.etf("510002.SH", index_name="AxxB")
        for keyword in ("%", "_", "/", "%_B/"):
            with self.subTest(keyword=keyword):
                self.assertEqual([item.ts_code for item in self.query(keyword=keyword).items], ["510001.SH"])

    def test_overview_returns_normalized_source_scoped_exchanges(self):
        for index, exchange in enumerate(("SH", "SSE", "SZ", "SZSE")):
            self.etf(f"{index:06}.SH", exchange=exchange, list_status="L" if index < 2 else "P")
        self.etf("999999.SH", source=self.source + "x", exchange="OTHER")
        with patch("app.data_ingestion.router.EtfQueryRepository", return_value=self.repository):
            result = get_etf_overview(self.session)
        self.assertEqual(result.exchanges, ["SSE", "SZSE"])
        self.assertEqual((result.total_records, result.listed_count, result.exchange_count), (4, 2, 2))
        empty = EtfQueryRepository(self.session, source="empty-" + uuid4().hex[:16]).overview()
        self.assertEqual((empty.total_records, empty.exchanges, empty.exchange_count), (0, [], 0))
        self.assertIsNone(empty.first_list_date)

    def test_calendar_details_cross_holidays_months_and_years(self):
        for start, distance in ((date(2026, 9, 30), 9), (date(2026, 12, 31), 4)):
            with self.subTest(start=start):
                self.calendar(start, open=True, previous=start - timedelta(days=1))
                for offset in range(1, distance):
                    self.calendar(start + timedelta(days=offset), previous=start)
                next_day = start + timedelta(days=distance)
                self.calendar(next_day, open=True, previous=start)
                first = self.detail(start)
                self.assertEqual(first.previous_trading_date, start - timedelta(days=1))
                self.assertEqual(first.next_trading_date, next_day)
                closed = self.detail(start + timedelta(days=1))
                self.assertFalse(closed.is_open)
                self.assertEqual(closed.next_trading_date, next_day)
                self.assertIn("updated_at", closed.model_dump())

    def test_calendar_missing_coverage_does_not_guess_next_trading_day(self):
        start = date(2026, 10, 8)
        self.calendar(start, open=True)
        self.calendar(start + timedelta(days=2), open=True)
        # Another exchange's row cannot fill the selected exchange's gap.
        self.calendar(start + timedelta(days=1), exchange="X" + self.exchange)
        self.assertIsNone(self.detail(start).next_trading_date)
        self.calendar(start + timedelta(days=1))
        # This fixture marks Saturday open: persisted facts must beat weekdays.
        self.assertEqual(self.detail(start).next_trading_date, date(2026, 10, 10))
        self.assertIsNone(self.detail(date(2026, 10, 10)).next_trading_date)
        with self.assertRaises(HTTPException) as raised:
            self.detail(date(2026, 10, 11))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("尚未采集", raised.exception.detail)

    def test_calendar_list_keeps_missing_dates_absent_and_validates_ranges(self):
        self.calendar(date(2026, 9, 1), open=True)
        self.calendar(date(2026, 9, 3))
        filters = dict(session=self.session, exchange=self.exchange, is_open=None,
                       start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), limit=42, offset=0)
        result = list_trading_calendar_days(**filters)
        self.assertEqual(result.total, 2)
        self.assertEqual([item.calendar_date.day for item in result.items], [3, 1])
        with self.assertRaises(HTTPException) as raised:
            list_trading_calendar_days(**{**filters, "start_date": date(2026, 10, 1)})
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
