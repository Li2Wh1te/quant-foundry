"""Tests for ETF reference-date queries used by daily-bar synchronization."""

import unittest
from datetime import date
from unittest.mock import Mock

from sqlalchemy.dialects import postgresql

from app.data_ingestion.repositories.etf import EtfCodeRepository


class EtfCodeRepositoryDateQueryTestCase(unittest.TestCase):
    def test_reads_the_earliest_known_listing_date_for_one_source(self) -> None:
        session = Mock()
        session.scalar.return_value = date(2005, 2, 23)

        result = EtfCodeRepository(session).earliest_list_date(source="tushare")

        self.assertEqual(result, date(2005, 2, 23))
        statement = session.scalar.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn("min(etf_codes.list_date)", sql)
        self.assertIn("etf_codes.source =", sql)
        self.assertIn("etf_codes.list_date IS NOT NULL", sql)
