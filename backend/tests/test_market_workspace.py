"""Protect shared native watchlist catalogue queries with synthetic rows.

Local column mirrors follow the existing overview tests because checkpoint
constraints use PostgreSQL JSON operators. PostgreSQL uses the migrated schema;
fixtures are isolated by source/exchange and rolled back after every test.
"""

import os
import unittest
from datetime import UTC, date, datetime
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import Column, JSON, MetaData, Table, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data_ingestion.models.etf import EtfCode, EtfEntity
from app.data_ingestion.repositories.etf_query import EtfQueryRepository
from app.data_ingestion.router import list_etfs
from app.instruments.models import Instrument


NOW = datetime(2026, 9, 10, tzinfo=UTC)


class MarketWorkspaceTest(unittest.TestCase):
    def setUp(self):
        postgres = os.getenv("POSTGRES_TEST_ENABLED") == "1"
        self.engine = create_engine(get_settings().database_url if postgres else "sqlite://")
        if not postgres:
            metadata = MetaData()
            for model in (Instrument, EtfEntity, EtfCode):
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

    def test_picker_returns_persisted_identity_and_filters_without_cross_source_leaks(self):
        first = self.etf("510001.SH", csname="沪深300ETF")
        self.etf("510002.SH", csname="其他ETF")
        self.etf("510003.SH", etf_id=first.etf_id, source=self.source + "x")
        page = self.query(instrument_id=first.etf_id)
        self.assertEqual(page.total, 1)
        self.assertEqual(page.items[0].instrument_id, first.etf_id)
        wire = page.model_dump(mode="json")
        self.assertEqual(wire["items"][0]["instrument_id"], str(first.etf_id))
        self.assertNotIn("etf_id", wire["items"][0])
        self.assertEqual(self.query(instrument_id=first.etf_id, keyword="不存在").total, 0)
        self.assertEqual(self.query(instrument_id=uuid4()).total, 0)

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


if __name__ == "__main__":
    unittest.main()
