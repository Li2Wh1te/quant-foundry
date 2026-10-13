"""Protect the shared source-scoped watchlist code lookup."""

import unittest
from unittest.mock import Mock

from app.data_ingestion.repositories.etf_detail import EtfDetailQueryRepository


class EtfDetailQueryRepositoryTestCase(unittest.TestCase):
    """Keep SQL predicates stable for the ETF detail endpoints."""

    def test_get_code_is_scoped_to_the_configured_source(self) -> None:
        session = Mock()

        EtfDetailQueryRepository(session).get_code("510300.SH")

        query = str(session.scalar.call_args.args[0])
        self.assertIn("etf_codes.source", query)
        self.assertIn("etf_codes.ts_code", query)


if __name__ == "__main__":
    unittest.main()
