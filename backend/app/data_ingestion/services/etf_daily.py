"""ETF daily-bar retrieval through the official Tushare Pro endpoint."""

from typing import TYPE_CHECKING

from app.data_ingestion.clients.tushare import TushareClient

if TYPE_CHECKING:
    from pandas import DataFrame


# Keep this aligned with Tushare's fund_daily example so the returned frame has
# exactly the fields needed for a first read-only ETF daily-bar integration.
ETF_DAILY_FIELDS = "trade_date,open,high,low,close,vol,amount"


def fetch_etf_daily(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> "DataFrame":
    """Fetch daily ETF market data for one Tushare code and compact date range."""
    return client.pro.fund_daily(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        fields=ETF_DAILY_FIELDS,
    )
