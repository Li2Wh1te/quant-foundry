import unittest

from app.data_ingestion.models.etf import EtfCode


class EtfCodeModelTestCase(unittest.TestCase):
    def test_uses_source_and_tushare_trading_code_as_the_listing_key(self) -> None:
        self.assertEqual(
            [column.name for column in EtfCode.__table__.primary_key.columns],
            ["source", "ts_code"],
        )

    def test_indexes_entity_status_exchange_and_tracked_index(self) -> None:
        indexes = {index.name for index in EtfCode.__table__.indexes}

        self.assertTrue(
            {
                "ix_etf_codes_entity",
                "ix_etf_codes_status_exchange",
                "ix_etf_codes_index_code",
            }
            <= indexes
        )
