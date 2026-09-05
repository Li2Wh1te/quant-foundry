"""Stable identifiers shared by ingestion write and operator read paths."""

TRADE_CALENDAR_SYNC_KEY = "tushare.trade_calendar"
# The legacy table is only an audited migration input.  Keeping a dedicated
# checkpoint prevents a one-off backfill from sharing the live ingestion
# cursor while still scoping progress by canonical ``calendar_id``.
LEGACY_TRADE_CALENDAR_BACKFILL_SYNC_KEY = "migration.trade_calendar_days.backfill"
ETF_DAILY_INCREMENTAL_SYNC_KEY = "tushare.fund_daily.incremental"
ETF_DAILY_FULL_SYNC_KEY = "tushare.fund_daily.full"
ETF_BASIC_SYNC_KEY = "tushare.etf_basic"
ETF_ADJUSTMENT_INCREMENTAL_SYNC_KEY = "tushare.fund_adj.incremental"
ETF_ADJUSTMENT_FULL_SYNC_KEY = "tushare.fund_adj.full"
TUSHARE_SOURCE = "tushare"
