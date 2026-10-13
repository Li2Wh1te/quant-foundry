from datetime import UTC, datetime
import unittest
from unittest.mock import Mock

from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.repositories.etf_query import EtfQueryRepository


class EtfQueryRepositoryTestCase(unittest.TestCase):
    def test_lists_source_scoped_etfs_with_matching_filters_and_total(self) -> None:
        code = EtfCode(
            source="tushare",
            ts_code="510300.SH",
            etf_id="00000000-0000-0000-0000-000000000001",
            list_status="L",
            exchange="SSE",
            first_seen_at=datetime(2026, 8, 16, tzinfo=UTC),
            last_seen_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
        session = Mock()
        session.scalars.return_value.all.return_value = [code]
        session.scalar.return_value = 1

        items, total = EtfQueryRepository(session).list_codes(
            keyword="沪深300",
            exchange="SSE",
            list_status="L",
            limit=50,
            offset=0,
        )

        self.assertEqual(items, [code])
        self.assertEqual(total, 1)
        page_query = str(session.scalars.call_args.args[0])
        count_query = str(session.scalar.call_args.args[0])
        for query in (page_query, count_query):
            self.assertIn("etf_codes.source", query)
            self.assertIn("etf_codes.exchange", query)
            self.assertIn(" IN ", query)
            self.assertIn("etf_codes.list_status", query)
            self.assertIn("lower(etf_codes.ts_code) LIKE", query)

    def test_expands_exchange_filters_for_existing_tushare_code_variants(self) -> None:
        self.assertEqual(EtfQueryRepository._exchange_codes("SSE"), ("SSE", "SH"))
        self.assertEqual(EtfQueryRepository._exchange_codes("SH"), ("SSE", "SH"))
        self.assertEqual(EtfQueryRepository._exchange_codes("SZSE"), ("SZSE", "SZ"))
        self.assertEqual(EtfQueryRepository._exchange_codes("SZ"), ("SZSE", "SZ"))


if __name__ == "__main__":
    unittest.main()
