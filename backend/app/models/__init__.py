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
from app.instruments.models import (
    Instrument,
    InstrumentCodeMappingRecord,
    InstrumentDisplayFactRecord,
    DisplayResolutionHead,
    InstrumentIdentityMergeAuditRecord,
    InstrumentIdentityFactRecord,
    MappingResolutionHead,
)
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
from app.backtesting.models import (
    BacktestAccountProfileRecord,
    BacktestQueueGuardRecord,
    BacktestRunRecord,
)
from app.backtesting.calendar_models import (
    CalendarRegistryRecord,
    CalendarSourcePriorityRecord,
    CalendarDefinitionRecord,
    CalendarSessionFactRecord,
    CalendarExchangeBindingRecord,
    CalendarCapabilityDeclarationRecord,
    CalendarResolutionHeadRecord,
    CalendarReconciliationRangeRecord,
)
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
    "BacktestRunRecord",
    "BacktestQueueGuardRecord",
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
    "InstrumentDisplayFactRecord",
    "DisplayResolutionHead",
    "InstrumentIdentityMergeAuditRecord",
    "InstrumentIdentityFactRecord",
    "MappingResolutionHead",
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
    "CalendarRegistryRecord",
    "CalendarSourcePriorityRecord",
    "CalendarDefinitionRecord",
    "CalendarSessionFactRecord",
    "CalendarExchangeBindingRecord",
    "CalendarCapabilityDeclarationRecord",
    "CalendarResolutionHeadRecord",
    "CalendarReconciliationRangeRecord",
]
