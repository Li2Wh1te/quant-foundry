"""Tushare Pro SDK client setup."""

import tushare as ts
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_engine
from app.data_sources.service import runtime_credentials
from app.data_sources.providers import SourceError

FUND_DIV_FIELDS = (
    "ts_code,ann_date,imp_anndate,base_date,div_proc,record_date,ex_date,"
    "pay_date,earpay_date,net_ex_date,div_cash,base_unit,ear_distr,ear_amount,"
    "account_date,base_year"
)
SUSPEND_FIELDS = "ts_code,trade_date,suspend_type,suspend_timing"


class TushareClient:
    """Configure and expose the official Tushare Pro SDK client."""

    def __init__(self, settings: Settings) -> None:
        with Session(get_engine()) as session:
            values, secrets = runtime_credentials(session, settings, "tushare")
        self.pro = ts.pro_api(secrets["token"])
        # The SDK has a class-level default URL. Shadow it on this instance so
        # a page save cannot redirect an already running job to another server.
        self.pro._DataApi__http_url = values["api_url"]
        original_query = self.pro.query

        def safe_query(*args, **kwargs):
            # Vendor/proxy error messages can echo authentication payloads.
            # Suppress the exception chain before ingestion logs persist it.
            try:
                return original_query(*args, **kwargs)
            except Exception:
                raise SourceError("Tushare 数据请求失败，请检查连接、接口权限与调用额度。", status_code=502) from None

        self.pro.query = safe_query

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
