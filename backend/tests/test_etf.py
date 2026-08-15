import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.data_ingestion.schemas.etf import EtfBasicUpsertResult, EtfInstrumentInput
from app.data_ingestion.schemas.trading_calendar import DataSyncCheckpointState
from app.data_ingestion.services.etf import (
    ETF_BASIC_FIELDS,
    _commit_etf_basics,
    fetch_etf_basics,
    normalize_etf_basics,
    sync_etf_basics,
)


def make_instrument(ts_code: str = "159526.SZ") -> EtfInstrumentInput:
    return EtfInstrumentInput(
        ts_code=ts_code,
        csname="嘉实中证A500ETF",
        extname="嘉实A500",
        cname="嘉实中证A500交易型开放式指数证券投资基金",
        index_code="000510.SH",
        index_name="中证A500指数",
        setup_date=date(2024, 9, 20),
        list_date=date(2024, 9, 26),
        list_status="L",
        exchange="SZ",
        mgr_name="嘉实基金",
        custod_name="中国银行",
        mgt_fee=Decimal("0.15"),
        etf_type="境内",
    )


class FetchEtfBasicsTestCase(unittest.TestCase):
    def test_requests_all_documented_fields_without_a_listing_status_filter(self) -> None:
        client = Mock()

        result = fetch_etf_basics(client)

        self.assertIs(result, client.pro.etf_basic.return_value)
        client.pro.etf_basic.assert_called_once_with(fields=ETF_BASIC_FIELDS)

    def test_normalizes_complete_tushare_rows(self) -> None:
        dataframe = Mock()
        dataframe.to_dict.return_value = [
            {
                "ts_code": "159526.SZ",
                "csname": "嘉实中证A500ETF",
                "extname": "嘉实A500",
                "cname": "嘉实中证A500交易型开放式指数证券投资基金",
                "index_code": "000510.SH",
                "index_name": "中证A500指数",
                "setup_date": "20240920",
                "list_date": "20240926",
                "list_status": "L",
                "exchange": "SZ",
                "mgr_name": "嘉实基金",
                "custod_name": "中国银行",
                "mgt_fee": 0.15,
                "etf_type": "境内",
            }
        ]

        self.assertEqual(normalize_etf_basics(dataframe), [make_instrument()])

    @patch("app.data_ingestion.services.etf.logger")
    @patch("app.data_ingestion.services.etf._commit_etf_basics")
    @patch("app.data_ingestion.services.etf.normalize_etf_basics")
    @patch("app.data_ingestion.services.etf.fetch_etf_basics")
    @patch("app.data_ingestion.services.etf.tushare_request_pacer")
    @patch("app.data_ingestion.services.etf._load_checkpoint")
    @patch("app.data_ingestion.services.etf.get_settings")
    def test_sync_uses_full_snapshot_upsert_and_advances_checkpoint(
        self,
        get_settings_mock,
        load_checkpoint_mock,
        pacer_mock,
        fetch_mock,
        normalize_mock,
        commit_mock,
        logger_mock,
    ) -> None:
        get_settings_mock.return_value = SimpleNamespace(
            ingestion_request_interval_ms=1_000
        )
        load_checkpoint_mock.return_value = None
        instrument = make_instrument()
        normalize_mock.return_value = [instrument]
        completed_at = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
        checkpoint = DataSyncCheckpointState(
            sync_key="tushare.etf_basic",
            scope_key="market=CN",
            cursor={"refreshed_at": completed_at.isoformat()},
            cursor_version=1,
            version=1,
        )
        commit_mock.return_value = (
            EtfBasicUpsertResult(received=1, changed=1, unchanged=0), checkpoint
        )
        client = Mock()

        result = sync_etf_basics(
            client, request_interval_ms=1_500, refreshed_at=completed_at
        )

        self.assertEqual(result.changed, 1)
        pacer_mock.wait_for_turn.assert_called_once_with(1_500)
        fetch_mock.assert_called_once_with(client)
        commit_mock.assert_called_once_with(
            instruments=[instrument], expected_checkpoint=None, refreshed_at=completed_at
        )
        self.assertEqual(
            [item.args[0] for item in logger_mock.info.call_args_list],
            ["etf_basic_sync_started", "etf_basic_sync_succeeded"],
        )
        logger_mock.exception.assert_not_called()

    @patch("app.data_ingestion.services.etf.DataSyncCheckpointRepository")
    @patch("app.data_ingestion.services.etf.EtfCodeRepository")
    @patch("app.data_ingestion.services.etf.get_engine")
    @patch("app.data_ingestion.services.etf.Session")
    def test_commits_reference_data_and_checkpoint_in_one_transaction(
        self,
        session_class,
        get_engine_mock,
        repository_class,
        checkpoint_repository_class,
    ) -> None:
        session = session_class.return_value.__enter__.return_value
        expected = EtfBasicUpsertResult(received=1, changed=1, unchanged=0)
        repository_class.return_value.upsert_codes.return_value = expected
        refreshed_at = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
        checkpoint = DataSyncCheckpointState(
            sync_key="tushare.etf_basic",
            scope_key="market=CN",
            cursor={"refreshed_at": refreshed_at.isoformat()},
            cursor_version=1,
            version=1,
        )
        checkpoint_repository_class.return_value.advance.return_value = checkpoint

        result, actual_checkpoint = _commit_etf_basics(
            instruments=[make_instrument()],
            expected_checkpoint=None,
            refreshed_at=refreshed_at,
        )

        self.assertIs(result, expected)
        self.assertIs(actual_checkpoint, checkpoint)
        repository_class.return_value.upsert_codes.assert_called_once_with(
            [make_instrument()], source="tushare", observed_at=refreshed_at
        )
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()
