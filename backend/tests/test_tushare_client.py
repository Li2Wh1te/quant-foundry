import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from app.core.config import Settings
from app.data_ingestion.clients.tushare import TushareClient
from app.data_sources.providers import SourceError


API_TOKEN = "a" * 64


class TushareClientTestCase(unittest.TestCase):
    @patch("app.data_ingestion.clients.tushare.get_engine")
    @patch("app.data_ingestion.clients.tushare.Session")
    @patch("app.data_ingestion.clients.tushare.runtime_credentials")
    @patch("app.data_ingestion.clients.tushare.ts.pro_api")
    def test_database_credentials_and_urls_are_isolated_per_client(self, pro_api_mock, credentials, session, engine) -> None:
        pro_api_mock.side_effect = [SimpleNamespace(query=Mock()), SimpleNamespace(query=Mock())]
        credentials.side_effect = [({"api_url": "https://first.example"}, {"token": "first"}),
                                   ({"api_url": "https://second.example"}, {"token": "second"})]
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            tushare_token="tushare-secret",
            tushare_api_url="https://tu.brze.top",
            _env_file=None,
        )
        client = TushareClient(settings)

        other = TushareClient(settings)
        self.assertEqual(client.pro._DataApi__http_url, "https://first.example")
        self.assertEqual(other.pro._DataApi__http_url, "https://second.example")
        self.assertEqual([call.args[0] for call in pro_api_mock.call_args_list], ["first", "second"])

    @patch("app.data_ingestion.clients.tushare.get_engine")
    @patch("app.data_ingestion.clients.tushare.Session")
    @patch("app.data_ingestion.clients.tushare.runtime_credentials", return_value=({"api_url": "https://example.test"}, {"token": "private-token"}))
    @patch("app.data_ingestion.clients.tushare.ts.pro_api")
    def test_vendor_error_cannot_leak_a_credential_to_operational_logs(self, pro_api, credentials, session, engine):
        import traceback
        pro_api.return_value.query.side_effect = ValueError("vendor reflected private-token")
        client = TushareClient(Settings(api_token=API_TOKEN, database_password="test", _env_file=None))
        try:
            client.pro.query("trade_cal")
        except SourceError:
            self.assertNotIn("private-token", traceback.format_exc())
        else:
            self.fail("the vendor failure must remain a failed request")

    @patch("app.data_ingestion.clients.tushare.get_engine")
    @patch("app.data_ingestion.clients.tushare.Session")
    @patch("app.data_ingestion.clients.tushare.runtime_credentials", side_effect=SourceError("请先在数据源页面完成连接配置。"))
    def test_never_falls_back_to_legacy_environment(self, credentials, session, engine) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            tushare_token="legacy-credential",
            _env_file=None,
        )

        with self.assertRaisesRegex(SourceError, "数据源页面"):
            TushareClient(settings)
