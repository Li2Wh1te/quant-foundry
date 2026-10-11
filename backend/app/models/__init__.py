"""Import SQLAlchemy model modules here so Alembic can discover them."""

from app.data_sources.models import DataSourceConfig
from app.data_ingestion.models.tonghuashun import (
    TonghuashunTicker, TonghuashunObservation, TonghuashunCollectionState, TonghuashunRequestBudget,
    TonghuashunDumpImport, TonghuashunDumpStage,
)
from app.data_ingestion.models.etf_watchlist import EtfWatchlistEntry

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
from app.backtest_service.history.storage_models import (
    BacktestAccountProfileRecord,
    BacktestQueueGuardRecord,
    BacktestRunRecord,
)
from app.calendars.models import (
    CalendarRegistryRecord,
    CalendarSourcePriorityRecord,
    CalendarDefinitionRecord,
    CalendarSessionFactRecord,
    CalendarExchangeBindingRecord,
    CalendarCapabilityDeclarationRecord,
    CalendarResolutionHeadRecord,
    CalendarReconciliationRangeRecord,
)
from app.backtest_service.history.result_records import (
    RESULT_TABLE_NAMES,
    BacktestAnalysisSummaryRecord,
    BacktestDataChunkRecord,
    BacktestDataPreflightResultRecord,
    BacktestDecisionRecord,
    BacktestEventResultRecord,
    BacktestEquityCurveRecord,
    BacktestFillResultRecord,
    BacktestMetricRecord,
    BacktestOrderResultRecord,
    BacktestOrderUpdateRecord,
    BacktestPositionResultRecord,
    BacktestStepRecord,
)


__all__ = [
    "TonghuashunTicker", "TonghuashunObservation", "TonghuashunCollectionState", "TonghuashunRequestBudget",
    "TonghuashunDumpImport", "TonghuashunDumpStage",
    "EtfWatchlistEntry",
    "DataSourceConfig",
    "DataSyncCheckpoint",
    "BacktestAccountProfileRecord",
    "BacktestRunRecord",
    "BacktestQueueGuardRecord",
    "BacktestAnalysisSummaryRecord",
    "BacktestDataChunkRecord",
    "BacktestDataPreflightResultRecord",
    "BacktestDecisionRecord",
    "BacktestEventResultRecord",
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

from app.backtest_service.accounts.fee_catalog import FeeScheduleVersionRecord  # noqa: F401

from app.backtest_service.history.saved_comparisons import SavedComparison, SavedComparisonMember  # noqa: F401
from app.backtest_service.models import Run, RunBatch, RunResult  # noqa: F401
