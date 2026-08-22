"""Import SQLAlchemy model modules here so Alembic can discover them."""

from app.data_ingestion.models import (
    DataSyncCheckpoint,
    EtfCode,
    EtfCodeMappingAudit,
    EtfEntity,
    EtfAdjustmentFactor,
    EtfDailyBar,
    TradingCalendarDay,
)
from app.scheduling.models import ScheduledTask, TaskRun
from app.strategies.models import Strategy, StrategyDraft, StrategyRevision
from app.backtesting.models import BacktestAccountProfileRecord
from app.backtesting.result_records import (
    RESULT_TABLE_NAMES,
    BacktestDataChunkRecord,
    BacktestDataPreflightResultRecord,
    BacktestDecisionRecord,
    BacktestEquityCurveRecord,
    BacktestFillResultRecord,
    BacktestMetricRecord,
    BacktestOrderResultRecord,
    BacktestOrderUpdateRecord,
    BacktestPositionResultRecord,
    BacktestStepRecord,
)


__all__ = [
    "DataSyncCheckpoint",
    "BacktestAccountProfileRecord",
    "BacktestDataChunkRecord",
    "BacktestDataPreflightResultRecord",
    "BacktestDecisionRecord",
    "BacktestEquityCurveRecord",
    "BacktestFillResultRecord",
    "BacktestMetricRecord",
    "BacktestOrderResultRecord",
    "BacktestOrderUpdateRecord",
    "BacktestPositionResultRecord",
    "BacktestStepRecord",
    "RESULT_TABLE_NAMES",
    "EtfCode",
    "EtfCodeMappingAudit",
    "EtfEntity",
    "EtfAdjustmentFactor",
    "EtfDailyBar",
    "ScheduledTask",
    "Strategy",
    "StrategyDraft",
    "StrategyRevision",
    "TaskRun",
    "TradingCalendarDay",
]
