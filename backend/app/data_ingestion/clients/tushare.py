"""Tushare Pro SDK client setup."""

import tushare as ts
from tushare.pro import client as _ts_client

from app.core.config import Settings


class TushareClient:
    """Configure and expose the official Tushare Pro SDK client."""

    def __init__(self, settings: Settings) -> None:
        if settings.tushare_token is None:
            raise ValueError(
                "QF_TUSHARE_TOKEN must be configured before using the Tushare client"
            )
        _ts_client.DataApi._DataApi__http_url = settings.tushare_api_url
        self.pro = ts.pro_api(settings.tushare_token.get_secret_value())
