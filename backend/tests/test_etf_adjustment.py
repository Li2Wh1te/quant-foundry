"""Tests for ETF adjustment-factor retrieval and current-value persistence."""

import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from sqlalchemy.dialects import postgresql

from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.repositories.etf_adjustment import (
    EtfAdjustmentFactorRepository,
)
from app.backtesting.data.errors import InstrumentCalendarUnresolvedError
from app.data_ingestion.schemas.etf_adjustment import (
    EtfAdjustmentFactorInput,
    EtfAdjustmentFactorUpsertResult,
    EtfAdjustmentSyncResult,
)
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.data_ingestion.services.etf_adjustment import (
    ETF_ADJUSTMENT_INCREMENTAL_SYNC_KEY,
    ETF_ADJUSTMENT_PAGE_SIZE,
    ETF_ADJUSTMENT_SCOPE_KEY,
    _fetch_market_factors_for_trade_date,
    fetch_etf_adjustment_factors,
    fetch_etf_adjustment_factors_for_trade_date,
    normalize_etf_adjustment_factors,
    sync_etf_adjustment_incremental,
    sync_etf_adjustment_factors,
)


def make_factor() -> EtfAdjustmentFactorInput:
    return EtfAdjustmentFactorInput(
        ts_code="513100.SH",
        trade_date=date(2026, 8, 14),
        adj_factor=Decimal("1.123456789012"),
    )


class EtfAdjustmentFactorModelTestCase(unittest.TestCase):
    def test_uses_source_code_and_trade_date_as_the_primary_key(self) -> None:
        self.assertEqual(
            [column.name for column in EtfAdjustmentFactor.__table__.primary_key.columns],
            ["source", "ts_code", "trade_date"],
        )

    def test_indexes_trade_date_for_cross_etf_queries(self) -> None:
        self.assertIn(
            "ix_etf_adjustment_factors_trade_date_code",
            {index.name for index in EtfAdjustmentFactor.__table__.indexes},
        )


class FetchEtfAdjustmentFactorsTestCase(unittest.TestCase):
    def test_forwards_the_documented_tushare_parameters(self) -> None:
        client = Mock()

        result = fetch_etf_adjustment_factors(
            client,
            ts_code="513100.SH",
            start_date="20190101",
            end_date="20190926",
        )

        self.assertIs(result, client.pro.fund_adj.return_value)
        client.pro.fund_adj.assert_called_once_with(
            ts_code="513100.SH",
            start_date="20190101",
            end_date="20190926",
        )

    def test_fetches_one_whole_market_page_by_trade_date(self) -> None:
        client = Mock()

        result = fetch_etf_adjustment_factors_for_trade_date(
            client,
            trade_date="20260814",
            offset=2_000,
        )

        self.assertIs(result, client.pro.fund_adj.return_value)
        client.pro.fund_adj.assert_called_once_with(
            trade_date="20260814", offset=2_000, limit=ETF_ADJUSTMENT_PAGE_SIZE
        )

    def test_normalizes_factor_rows(self) -> None:
        dataframe = Mock()
        dataframe.to_dict.return_value = [
            {
                "ts_code": "513100.SH",
                "trade_date": "20260814",
                "adj_factor": 1.123456789012,
            }
        ]

        self.assertEqual(
            normalize_etf_adjustment_factors(
                dataframe, expected_ts_code="513100.SH"
            ),
            [make_factor()],
        )

    def test_rejects_non_positive_factor(self) -> None:
        dataframe = Mock()
        dataframe.to_dict.return_value = [
            {"ts_code": "513100.SH", "trade_date": "20260814", "adj_factor": 0}
        ]

        with self.assertRaisesRegex(ValueError, "invalid adj_factor"):
            normalize_etf_adjustment_factors(
                dataframe, expected_ts_code="513100.SH"
            )


class EtfAdjustmentFactorRepositoryTestCase(unittest.TestCase):
    def test_upsert_updates_only_changed_factors(self) -> None:
        session = Mock()
        session.execute.return_value.all.return_value = [("513100.SH",)]

        result = EtfAdjustmentFactorRepository(session).upsert_factors(
            [make_factor()], source="tushare"
        )

        self.assertEqual(
            (result.received, result.changed, result.unchanged), (1, 1, 0)
        )
        statement = session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn(
            "ON CONFLICT (source, ts_code, trade_date) DO UPDATE", sql
        )
        self.assertIn("IS DISTINCT FROM excluded.adj_factor", sql)

    def test_rejects_duplicate_factor_keys_before_writing(self) -> None:
        session = Mock()

        with self.assertRaisesRegex(ValueError, "duplicate ts_code and trade_date"):
            EtfAdjustmentFactorRepository(session).upsert_factors(
                [make_factor(), make_factor()], source="tushare"
            )

        session.execute.assert_not_called()


class SyncEtfAdjustmentFactorsTestCase(unittest.TestCase):
    @patch("app.data_ingestion.services.etf_adjustment.logger")
    @patch(
        "app.data_ingestion.services.etf_adjustment._commit_etf_adjustment_factors"
    )
    @patch(
        "app.data_ingestion.services.etf_adjustment.normalize_etf_adjustment_factors"
    )
    @patch("app.data_ingestion.services.etf_adjustment.fetch_etf_adjustment_factors")
    @patch("app.data_ingestion.services.etf_adjustment.tushare_request_pacer")
    @patch("app.data_ingestion.services.etf_adjustment.get_settings")
    def test_fetches_and_commits_one_range(
        self,
        get_settings_mock,
        pacer_mock,
        fetch_mock,
        normalize_mock,
        commit_mock,
        logger_mock,
    ) -> None:
        get_settings_mock.return_value = SimpleNamespace(
            ingestion_request_interval_ms=1_000
        )
        factor = make_factor()
        normalize_mock.return_value = [factor]
        commit_mock.return_value = EtfAdjustmentFactorUpsertResult(
            received=1, changed=1, unchanged=0
        )

        client = Mock()
        result = sync_etf_adjustment_factors(
            client,
            ts_code="513100.SH",
            start_date="20260801",
            end_date="20260814",
            request_interval_ms=1_500,
        )

        self.assertEqual(result.changed, 1)
        pacer_mock.wait_for_turn.assert_called_once_with(1_500)
        fetch_mock.assert_called_once_with(
            client,
            ts_code="513100.SH",
            start_date="20260801",
            end_date="20260814",
        )
        commit_mock.assert_called_once_with([factor])
        self.assertEqual(
            [call.args[0] for call in logger_mock.info.call_args_list],
            ["etf_adjustment_sync_started", "etf_adjustment_sync_succeeded"],
        )


class WholeMarketEtfAdjustmentSyncTestCase(unittest.TestCase):
    def test_reads_all_pages_for_one_market_date_before_writing(self) -> None:
        first_page = [
            EtfAdjustmentFactorInput(
                ts_code=f"{index:06d}.SH",
                trade_date=date(2026, 8, 14),
                adj_factor=Decimal("1"),
            )
            for index in range(ETF_ADJUSTMENT_PAGE_SIZE)
        ]
        final_page = [make_factor()]
        client = Mock()
        with (
            patch(
                "app.data_ingestion.services.etf_adjustment.fetch_etf_adjustment_factors_for_trade_date",
                side_effect=[Mock(), Mock()],
            ) as fetch_mock,
            patch(
                "app.data_ingestion.services.etf_adjustment.normalize_etf_adjustment_factors",
                side_effect=[first_page, final_page],
            ),
            patch("app.data_ingestion.services.etf_adjustment.tushare_request_pacer") as pacer_mock,
        ):
            factors = _fetch_market_factors_for_trade_date(
                client,
                trade_date=date(2026, 8, 14),
                request_interval_ms=1_000,
            )

        self.assertEqual(len(factors), ETF_ADJUSTMENT_PAGE_SIZE + 1)
        pacer_mock.wait_for_turn.assert_has_calls([call(1_000), call(1_000)])
        fetch_mock.assert_has_calls(
            [
                call(client, trade_date="20260814", offset=0),
                call(
                    client,
                    trade_date="20260814",
                    offset=ETF_ADJUSTMENT_PAGE_SIZE,
                ),
            ]
        )

    @patch("app.data_ingestion.services.etf_adjustment._sync_market_dates")
    @patch("app.data_ingestion.services.etf_adjustment._load_checkpoint")
    def test_no_cutoff_blocks_before_the_single_market_cursor(
        self, load_checkpoint_mock, sync_market_dates_mock
    ) -> None:
        with self.assertRaises(InstrumentCalendarUnresolvedError):
            sync_etf_adjustment_incremental(
                Mock(), as_of_date=date(2026, 8, 14)
            )

        load_checkpoint_mock.assert_not_called()
        sync_market_dates_mock.assert_not_called()
