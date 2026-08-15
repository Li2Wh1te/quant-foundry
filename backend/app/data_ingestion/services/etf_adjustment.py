"""ETF adjustment-factor retrieval through the official Tushare Pro SDK."""

from typing import TYPE_CHECKING

from app.data_ingestion.clients.tushare import TushareClient

if TYPE_CHECKING:
    from pandas import DataFrame


def fetch_etf_adjustment_factors(
    client: TushareClient,
    *,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> "DataFrame":
    """Fetch raw ETF adjustment factors for one Tushare code and date range.

    This intentionally mirrors Tushare's documented ``fund_adj`` example.  It
    performs one request only; persistence, pagination, and price adjustment
    calculations remain separate concerns for later ingestion workflows.
    """
    return client.pro.fund_adj(
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
    )
