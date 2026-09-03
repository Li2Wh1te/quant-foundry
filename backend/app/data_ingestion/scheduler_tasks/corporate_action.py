"""Scheduler registrations for ETF corporate-action ingestion."""

from datetime import date, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.data_ingestion.constants import TUSHARE_SOURCE
from app.data_ingestion.models.etf import EtfCode
from app.data_ingestion.services.corporate_action import (
    sync_fund_div,
    sync_fund_div_full,
)
from app.scheduling.registry import TaskContext, TaskDefinition, TaskRegistry


logger = structlog.get_logger(__name__)


class CorporateActionSyncParameters(BaseModel):
    """Validated date window shared by the three dividend task variants."""

    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    reconciliation_days: int = 30
    request_interval_ms: int = 200


def _require_context(context: TaskContext) -> tuple[Any, Any, Any]:
    """Reject an unbound handler instead of reporting a false empty success."""

    if context.client is None or context.session is None or context.checkpoint_repo is None:
        raise RuntimeError("ingestion task requires client, session, and checkpoint repository")
    if not context.sync_key:
        raise RuntimeError("ingestion task requires a checkpoint sync key")
    return context.client, context.session, context.checkpoint_repo


def _instrument_map(session: Any) -> dict[str, Any]:
    """Resolve source ETF codes to stable instrument identities in one query."""

    rows = session.execute(
        select(EtfCode.ts_code, EtfCode.etf_id).where(EtfCode.source == TUSHARE_SOURCE)
    ).all()
    return {ts_code: etf_id for ts_code, etf_id in rows}


def _window(parameters: CorporateActionSyncParameters, context: TaskContext) -> tuple[date | None, date | None]:
    """Use the checkpoint to make an omitted incremental window resumable."""

    start_date = parameters.start_date
    end_date = parameters.end_date
    checkpoint = context.checkpoint_repo.get(context.sync_key, "fund_div")
    if start_date is None and checkpoint is not None:
        value = checkpoint.cursor.get("synced_through_date")
        if isinstance(value, str):
            start_date = date.fromisoformat(value) + timedelta(days=1)
    if end_date is None and parameters.reconciliation_days:
        end_date = date.today()
    if parameters.reconciliation_days and parameters.start_date is None and context.task_type and context.task_type.endswith("reconciliation"):
        end_date = end_date or date.today()
        start_date = end_date - timedelta(days=parameters.reconciliation_days)
    return start_date, end_date


def _run_sync(
    client: Any,
    session: Any,
    mapping: dict[str, Any],
    checkpoint_repo: Any,
    context: TaskContext,
    checkpoint: Any | None,
    start_date: date | None,
    end_date: date | None,
    *,
    is_full: bool,
    is_reconciliation: bool,
) -> dict[str, Any]:
    """Select the correct source query without owning the outer transaction."""

    if is_full:
        return sync_fund_div_full(
            client,
            ts_codes=tuple(mapping),
            session=session,
            instrument_map=mapping,
            checkpoint_repo=checkpoint_repo,
            sync_key=context.sync_key,
            checkpoint_version=checkpoint.version if checkpoint else None,
            checkpoint_cursor={"synced_through_date": end_date.isoformat()} if end_date else {},
            start_date=start_date.isoformat() if start_date else None,
            end_date=end_date.isoformat() if end_date else None,
        )
    if start_date is not None and end_date is not None and start_date > end_date:
        return {
            "fetched": 0,
            "changed": 0,
            "unchanged": 0,
            "failed": 0,
            "checkpoint_advanced": False,
            "checkpoint_after": None,
        }
    return sync_fund_div(
        client,
        start_date=start_date.isoformat() if start_date else None,
        end_date=end_date.isoformat() if end_date else None,
        session=session,
        instrument_map=mapping,
        checkpoint_repo=None if is_reconciliation else checkpoint_repo,
        sync_key=None if is_reconciliation else context.sync_key,
        checkpoint_cursor={"synced_through_date": end_date.isoformat()} if end_date else {},
        checkpoint_version=checkpoint.version if checkpoint else None,
    )


def _handler(context: TaskContext, parameters: CorporateActionSyncParameters) -> dict[str, Any]:
    """Fetch source rows and persist source snapshots, facts, and checkpoint atomically."""

    client, session, checkpoint_repo = _require_context(context)
    mapping = _instrument_map(session)
    if not mapping:
        raise RuntimeError("ETF code catalogue is empty; cannot ingest corporate actions")

    checkpoint = checkpoint_repo.get(context.sync_key, "fund_div")
    start_date, end_date = _window(parameters, context)
    start_text = start_date.isoformat() if start_date else "不适用"
    end_text = end_date.isoformat() if end_date else "不适用"
    task_type = context.task_type or "data.sync_etf_cash_dividend_incremental"
    is_full = task_type.endswith("_full")
    is_reconciliation = task_type.endswith("_reconciliation")

    common = {
        "task_id": str(context.task_id),
        "run_id": str(context.run_id),
        "task_type": task_type,
        "source": TUSHARE_SOURCE,
        "data_type": "ETF现金分红",
        "start_date": start_text,
        "end_date": end_text,
        "checkpoint_scope": "fund_div",
        "checkpoint_before": checkpoint.cursor if checkpoint else None,
    }
    logger.info(
        "corporate_action_collection_started",
        message=f"开始采集ETF现金分红：{start_text}至{end_text}，checkpoint未推进。",
        **common,
        fetched_count=0,
        changed_count=0,
        unchanged_count=0,
        failed_count=0,
        checkpoint_after=common["checkpoint_before"],
        checkpoint_advanced=False,
    )

    try:
        result = _run_sync(
            client,
            session,
            mapping,
            checkpoint_repo,
            context,
            checkpoint,
            start_date,
            end_date,
            is_full=is_full,
            is_reconciliation=is_reconciliation,
        )
    except Exception as exc:
        logger.error(
            "corporate_action_collection_failed",
            message=f"ETF现金分红采集失败：{start_text}至{end_text}，拉取0条、变更0条、未变更0条、失败1条，checkpoint未推进。",
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
        f"ETF现金分红{'校验' if is_reconciliation else '采集'}完成：{start_text}至{end_text}，"
        f"拉取{fetched}条、变更{changed}条、未变更{unchanged}条、失败{failed}条，"
        f"checkpoint{'已推进至 ' + str(after) if advanced else '未推进'}。"
    )
    logger.info(
        "corporate_action_collection_succeeded",
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
        "task_id": str(context.task_id),
        "run_id": str(context.run_id),
        "task_type": task_type,
        "data_type": "ETF现金分红",
        "start_date": start_text,
        "end_date": end_text,
        "fetched_count": fetched,
        "changed_count": changed,
        "unchanged_count": unchanged,
        "failed_count": failed,
        "checkpoint_scope": "fund_div",
        "checkpoint_before": checkpoint.cursor if checkpoint else None,
        "checkpoint_after": after,
        "checkpoint_advanced": advanced,
        "message": message,
    }


def register_tasks(registry: TaskRegistry) -> None:
    """Register all ETF cash-dividend ingestion variants."""

    for key, name, english_name in (
        ("data.sync_etf_cash_dividend_full", "ETF现金分红全量采集", "Full Tushare ETF cash dividends"),
        ("data.sync_etf_cash_dividend_incremental", "ETF现金分红增量采集", "Incremental Tushare ETF cash dividends"),
        ("data.sync_etf_cash_dividend_reconciliation", "ETF现金分红近期校验", "Reconcile recent Tushare ETF cash dividends"),
    ):
        registry.register(
            TaskDefinition(
                key=key,
                name=name,
                english_name=english_name,
                parameters_model=CorporateActionSyncParameters,
                handler=_handler,
            )
        )
