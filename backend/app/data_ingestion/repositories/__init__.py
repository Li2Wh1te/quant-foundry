"""Persistence adapters for ingested data."""

from app.data_ingestion.repositories.etf import EtfCodeRepository
from app.data_ingestion.repositories.etf_query import EtfQueryRepository
from app.data_ingestion.repositories.trading_calendar import TradingCalendarRepository
from app.data_ingestion.repositories.sync_checkpoint import DataSyncCheckpointRepository
from app.data_ingestion.repositories.trading_calendar_query import TradingCalendarQueryRepository
from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.data_ingestion.repositories.etf_adjustment import EtfAdjustmentFactorRepository
from app.data_ingestion.repositories.corporate_action import CorporateActionRepository

__all__ = [
    "DataSyncCheckpointRepository",
    "EtfDailyBarRepository",
    "EtfAdjustmentFactorRepository",
    "EtfCodeRepository",
    "EtfQueryRepository",
    "TradingCalendarQueryRepository",
    "TradingCalendarRepository",
    "CorporateActionRepository",
]
