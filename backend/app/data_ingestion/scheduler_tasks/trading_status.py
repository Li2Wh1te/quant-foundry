"""Scheduler registration for Tushare daily trading-status ingestion."""

from datetime import date, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.services.trading_status import sync_suspend_d
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class TradingStatusSyncParameters(BaseModel):
    """Validated date window for suspension/trading-status facts."""

    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    # A source response never proves that absent events mean no suspension;
    # operators must opt in only when the query was independently confirmed
    # complete for the requested interval.
    coverage_confirmed: bool = False


def _require_context(context: TaskContext) -> tuple[Any, Any, Any]:
    """Reject an unbound handler instead of reporting a false empty success."""

    if context.client is None or context.session is None or context.checkpoint_repo is None:
        raise RuntimeError("ingestion task requires client, session, and checkpoint repository")
    if not context.sync_key:
        raise RuntimeError("ingestion task requires a checkpoint sync key")
    return context.client, context.session, context.checkpoint_repo


def _instrument_map(session: Any) -> dict[str, Any]:
    """Resolve source codes to stable identities for status coverage facts."""

    execute = getattr(session, "execute", None)
    if not callable(execute):
        return {}
    rows = execute(
        select(EtfCode.ts_code, EtfCode.etf_id).where(
            EtfCode.source == "tushare"
        )
    ).all()
    return {ts_code: instrument_id for ts_code, instrument_id in rows}


def _resolve_window(
    parameters: TradingStatusSyncParameters, checkpoint: Any | None
) -> tuple[date, date]:
    """Continue after the last committed date unless the task supplies a window."""

    start_date = parameters.start_date
    end_date = parameters.end_date or date.today()
    if start_date is None and checkpoint is not None:
        value = checkpoint.cursor.get("synced_through_date")
        if isinstance(value, str):
            start_date = date.fromisoformat(value) + timedelta(days=1)
    return start_date or end_date, end_date


def _handler(context: TaskContext, parameters: TradingStatusSyncParameters) -> dict[str, Any]:
    """Persist normalized facts and advance the checkpoint in one transaction."""

    client, session, checkpoint_repo = _require_context(context)
    checkpoint = checkpoint_repo.get(context.sync_key, "trading_status")
    instrument_map = _instrument_map(session)
    start_date, end_date = _resolve_window(parameters, checkpoint)
    start_text, end_text = start_date.isoformat(), end_date.isoformat()
    common = {
        "task_id": str(context.task_id),
        "run_id": str(context.run_id),
        "task_type": context.task_type or "data.sync_trading_status",
        "source": "tushare",
        "data_type": "停牌交易状态",
        "start_date": start_text,
        "end_date": end_text,
        "checkpoint_scope": "trading_status",
        "checkpoint_before": checkpoint.cursor if checkpoint else None,
    }

    logger.info(
        "trading_status_collection_started",
        message=f"开始采集停牌交易状态：{start_text}至{end_text}，checkpoint未推进。",
        **common,
        fetched_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        checkpoint_after=common["checkpoint_before"],
        checkpoint_advanced=False,
    )
    try:
        if start_date > end_date:
            result: dict[str, Any] = {
                "items": (),
                "fetched": 0,
                "changed": 0,
                "unchanged": 0,
                "failed": 0,
                "checkpoint_advanced": False,
                "checkpoint_after": None,
            }
        else:
            result = sync_suspend_d(
                client,
                start_date=start_text,
                end_date=end_text,
                session=session,
                checkpoint_repo=checkpoint_repo,
                sync_key=context.sync_key,
                checkpoint_cursor={"synced_through_date": end_text},
                checkpoint_version=checkpoint.version if checkpoint else None,
                instrument_map=instrument_map or None,
                coverage_confirmed=parameters.coverage_confirmed,
            )
    except Exception as exc:
        logger.error(
            "trading_status_collection_failed",
            message=f"停牌交易状态采集失败：{start_text}至{end_text}，拉取0条、变更0条、未变更0条、失败1条，checkpoint未推进。",
            **common,
            fetched_count=0,
            changed_count=0,
            unchanged_count=0,
            failed_count=1,
            checkpoint_after=common["checkpoint_before"],
            checkpoint_advanced=False,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise

    fetched = int(result.get("fetched", 0) or 0)
    changed = int(result.get("changed", 0) or 0)
    unchanged = int(result.get("unchanged", 0) or 0)
    failed = int(result.get("failed", 0) or 0)
    advanced = bool(result.get("checkpoint_advanced"))
    after = result.get("checkpoint_after")
    message = (
        f"采集停牌交易状态：{start_text}至{end_text}，拉取{fetched}条、变更{changed}条、"
        f"未变更{unchanged}条、失败{failed}条，"
        f"checkpoint{'已推进至 ' + str(after) if advanced else '未推进'}。"
    )
    logger.info(
        "trading_status_collection_succeeded",
        message=message,
        **common,
        fetched_count=fetched,
        changed_count=changed,
        unchanged_count=unchanged,
        failed_count=failed,
        checkpoint_after=after,
        checkpoint_advanced=advanced,
    )
    return {
        **result,
        **common,
        "fetched_count": fetched,
        "changed_count": changed,
        "unchanged_count": unchanged,
        "failed_count": failed,
        "checkpoint_after": after,
        "checkpoint_advanced": advanced,
        "message": message,
    }


def register_tasks(registry: TaskRegistry) -> None:
    """Register daily trading-status ingestion."""

    registry.register(
        TaskDefinition(
            key="data.sync_trading_status",
            name="停牌交易状态采集",
            english_name="Trading status and suspension ingestion",
            parameters_model=TradingStatusSyncParameters,
            handler=_handler,
        )
    )
