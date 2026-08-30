from datetime import date
from pydantic import BaseModel, ConfigDict
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry
from app.data_ingestion.services.trading_status import sync_suspend_d

class TradingStatusSyncParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date | None = None
    end_date: date | None = None

def _handler(context: TaskContext, parameters: TradingStatusSyncParameters):
    result = {}
    client = getattr(context, "client", None)
    if client is not None:
        result = sync_suspend_d(client, start_date=parameters.start_date.isoformat() if parameters.start_date else None,
                                end_date=parameters.end_date.isoformat() if parameters.end_date else None)
    start, end = parameters.start_date or "不适用", parameters.end_date or "不适用"
    advanced = bool(result.get("checkpoint_advanced"))
    checkpoint_after = result.get("checkpoint_after")
    checkpoint_text = f"checkpoint {'已推进至 ' + str(checkpoint_after) if advanced else '未推进（after=' + str(checkpoint_after) + '）'}"
    result.update(task_id=str(context.task_id), run_id=str(context.run_id),
                  task_type="data.sync_trading_status", source="tushare", data_type="停牌交易状态",
                  message=f"采集停牌交易状态：{start}至{end}，拉取{result.get('fetched',0)}条、变更{result.get('changed',0)}条、未变更{result.get('unchanged',0)}条、失败{result.get('failed',0)}条，{checkpoint_text}。")
    result.update(fetched_count=result.get("fetched", 0), changed_count=result.get("changed", 0),
                  unchanged_count=result.get("unchanged", 0), failed_count=result.get("failed", 0),
                  checkpoint_scope="trading_status", checkpoint_before=None, checkpoint_after=checkpoint_after,
                  checkpoint_advanced=advanced)
    return result

def register_tasks(registry: TaskRegistry):
    registry.register(TaskDefinition(key="data.sync_trading_status", name="停牌交易状态采集",
        english_name="Trading status and suspension ingestion", parameters_model=TradingStatusSyncParameters, handler=_handler))
