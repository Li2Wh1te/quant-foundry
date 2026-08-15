import unittest
from unittest.mock import patch

from app.core.config import Settings
from app.data_ingestion.clients.tushare import TushareClient


API_TOKEN = "a" * 64


class TushareClientTestCase(unittest.TestCase):
    @patch("app.data_ingestion.clients.tushare.ts.pro_api")
    @patch("app.data_ingestion.clients.tushare._ts_client.DataApi")
    def test_configures_official_sdk(self, data_api_class, pro_api_mock) -> None:
        pro_client = pro_api_mock.return_value
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            tushare_token="tushare-secret",
            tushare_api_url="https://tu.brze.top",
            _env_file=None,
        )
        client = TushareClient(settings)

        self.assertIs(client.pro, pro_client)
        pro_api_mock.assert_called_once_with("tushare-secret")
        self.assertEqual(data_api_class._DataApi__http_url, "https://tu.brze.top")

    def test_requires_tushare_token(self) -> None:
        settings = Settings(
            api_token=API_TOKEN,
            database_password="test-secret",
            _env_file=None,
        )

        with self.assertRaisesRegex(ValueError, "QF_TUSHARE_TOKEN"):
            TushareClient(settings)
