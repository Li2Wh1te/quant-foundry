from datetime import date
from pydantic import BaseModel, ConfigDict
from app.scheduling.registry import TaskDefinition, TaskRegistry, TaskContext
from app.data_ingestion.services.corporate_action import sync_fund_div

class CorporateActionSyncParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None
    reconciliation_days: int = 30
    request_interval_ms: int = 200

def _handler(context: TaskContext, parameters: CorporateActionSyncParameters):
    # The orchestration layer supplies the provider/repository transaction.  Keep
    # this handler's result structured and operator-readable even when no rows
    # are available for the requested window.
    mode = "近期复核" if parameters.reconciliation_days else "同步"
    result = {}
    client = getattr(context, "client", None)
    if client is not None:
        result = sync_fund_div(client, ann_date=(parameters.start_date.isoformat() if parameters.start_date else None),
                               end_date=(parameters.end_date.isoformat() if parameters.end_date else None),
                               session=getattr(context, "session", None),
                               checkpoint_repo=getattr(context, "checkpoint_repo", None),
                               sync_key=getattr(context, "sync_key", None))
    return {
        "task_id": str(context.task_id), "run_id": str(context.run_id),
        "status": "completed", "fetched": result.get("fetched", 0), "changed": result.get("changed", 0),
        "unchanged": result.get("unchanged", 0), "failed": result.get("failed", 0), "skipped_non_target": result.get("skipped_non_target", 0),
        "checkpoint_advanced": result.get("checkpoint_advanced", False),
        "message": f"ETF现金分红{mode}（{parameters.start_date or '起始'}至{parameters.end_date or '当前'}）抓取{result.get('fetched',0)}条、变更{result.get('changed',0)}条、失败{result.get('failed',0)}条，checkpoint{'已推进' if result.get('checkpoint_advanced') else '未推进'}。",
    }

def register_tasks(registry: TaskRegistry):
    for key, name, en in [
        ("data.sync_etf_cash_dividend_full", "ETF现金分红全量采集", "Full Tushare ETF cash dividends"),
        ("data.sync_etf_cash_dividend_incremental", "ETF现金分红增量采集", "Incremental Tushare ETF cash dividends"),
        ("data.sync_etf_cash_dividend_reconciliation", "ETF现金分红近期校验", "Reconcile recent Tushare ETF cash dividends"),
    ]:
        registry.register(TaskDefinition(key=key, name=name, english_name=en, parameters_model=CorporateActionSyncParameters, handler=_handler))
