"""Tushare Pro SDK client setup."""

import tushare as ts
from tushare.pro import client as _ts_client

from app.core.config import Settings

FUND_DIV_FIELDS = (
    "ts_code,ann_date,imp_anndate,base_date,div_proc,record_date,ex_date,"
    "pay_date,earpay_date,net_ex_date,div_cash,base_unit,ear_distr,ear_amount,"
    "account_date,base_year"
)
SUSPEND_FIELDS = "ts_code,trade_date,suspend_type,suspend_timing"


class TushareClient:
    """Configure and expose the official Tushare Pro SDK client."""

    def __init__(self, settings: Settings) -> None:
        if settings.tushare_token is None:
            raise ValueError(
                "QF_TUSHARE_TOKEN must be configured before using the Tushare client"
            )
        _ts_client.DataApi._DataApi__http_url = settings.tushare_api_url
        self.pro = ts.pro_api(settings.tushare_token.get_secret_value())

    def fund_div(self, *, ann_date: str | None = None, ts_code: str | None = None,
                 start_date: str | None = None, end_date: str | None = None,
                 offset: int = 0, limit: int = 5000):
        """Fetch ETF fund dividend records with an explicit, bounded query."""
        return self.pro.fund_div(ts_code=ts_code, ann_date=ann_date,
                                 start_date=start_date, end_date=end_date,
                                 offset=offset, limit=limit,
                                 fields=FUND_DIV_FIELDS)

    def suspend_d(self, *, ts_code: str | None = None, trade_date: str | None = None,
                  start_date: str | None = None, end_date: str | None = None,
                  offset: int = 0, limit: int = 5000):
        """Fetch daily suspension/trading-status facts from Tushare."""
        return self.pro.suspend_d(ts_code=ts_code, trade_date=trade_date,
                                  start_date=start_date, end_date=end_date,
                                  offset=offset, limit=limit, fields=SUSPEND_FIELDS)
