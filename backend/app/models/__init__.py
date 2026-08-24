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
from app.instruments.models import Instrument, InstrumentCodeMappingRecord
from app.instruments.rule_exceptions_models import (
    InstrumentRuleExceptionEntryRecord,
    InstrumentRuleExceptionSetRecord,
)
from app.instruments.rule_facts_models import InstrumentRuleFactRecord
from app.instruments.rule_snapshots_models import (
    BacktestRunInstrumentRuleSnapshotRecord,
    BacktestRunRuleSnapshotRecord,
)
from app.scheduling.models import ScheduledTask, TaskRun
from app.strategies.models import Strategy, StrategyDraft, StrategyRevision
from app.backtesting.models import BacktestAccountProfileRecord
from app.backtesting.result_records import (
    RESULT_TABLE_NAMES,
    BacktestAnalysisSummaryRecord,
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
    "BacktestAnalysisSummaryRecord",
    "BacktestDataChunkRecord",
    "BacktestDataPreflightResultRecord",
    "BacktestDecisionRecord",
    "BacktestEquityCurveRecord",
    "BacktestFillResultRecord",
    "BacktestMetricRecord",
    "BacktestOrderResultRecord",
    "BacktestOrderUpdateRecord",
    "BacktestPositionResultRecord",
    "BacktestRunInstrumentRuleSnapshotRecord",
    "BacktestRunRuleSnapshotRecord",
    "BacktestStepRecord",
    "RESULT_TABLE_NAMES",
    "EtfCode",
    "EtfCodeMappingAudit",
    "EtfEntity",
    "Instrument",
    "InstrumentCodeMappingRecord",
    "InstrumentRuleExceptionEntryRecord",
    "InstrumentRuleExceptionSetRecord",
    "InstrumentRuleFactRecord",
    "EtfAdjustmentFactor",
    "EtfDailyBar",
    "ScheduledTask",
    "Strategy",
    "StrategyDraft",
    "StrategyRevision",
    "TaskRun",
    "TradingCalendarDay",
]
