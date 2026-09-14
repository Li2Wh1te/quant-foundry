"""Authenticated source-local inspection, including immutable version reads."""

from dataclasses import asdict
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_ingestion.models.tonghuashun import (
    TonghuashunTicker as Ticker, TonghuashunCollectionState as State,
    TonghuashunObservation as Observation,
)
from app.data_ingestion.tonghuashun.contracts import DATASETS
from app.db.session import get_db_session

router = APIRouter(prefix="/api/admin/data-collections/tonghuashun", tags=["tonghuashun-data"])


def require_dataset(key):
    if key not in DATASETS:
        raise HTTPException(404, "同花顺数据集不存在。")
    return DATASETS[key]


def schedule_templates(spec):
    """Templates use the existing scheduler API; reads never create jobs."""
    clock = "30 22" if spec.kind == "nav" else "30 20" if spec.kind == "bars" else "0 20"
    # Weekly resources are checked daily for new/failed subjects; the handler
    # refreshes existing successful subjects only after the weekly boundary.
    expression = f"{clock} * * *" if spec.kind in ("directory", "calendar", "index_catalog") else "*/10 * * * *"
    result = [{"name": spec.name, "task_type": f"data.ths.{spec.key}",
        "description": "每批有上限；按数据集日切/周切规则检查待采范围，已完成对象不重复请求。",
        "parameters": {"batch_size": 100}, "schedule": {"type": "cron", "expression": expression, "timezone": "Asia/Shanghai"},
        "priority": 10, "concurrency_limit": 1, "overlap_policy": "skip"}]
    if spec.kind not in ("calendar", "directory", "index_catalog"):
        result.append({**result[0], "name": spec.name + "首次回补及失败补采",
            "parameters": {"mode": "backfill", "batch_size": 100}, "priority": -10,
            "schedule": {"type": "cron", "expression": "*/10 * * * *", "timezone": "Asia/Shanghai"}})
    if spec.kind in ("bars", "nav", "financials", "indicators", "reports"):
        result.append({**result[0], "name": spec.name + "历史核对", "parameters": {"mode": "reconcile"},
            "description": "每10分钟续作一个批次；ETF历史每周日03:00起核对，其他历史每月1日03:00起核对，已完成对象不重复请求。",
            "priority": -10, "schedule": {"type": "cron", "expression": "*/10 * * * *", "timezone": "Asia/Shanghai"}})
    return result


@router.get("/datasets")
def datasets():
    return {"source": "tonghuashun", "items": [{**asdict(spec),
        "task_type": f"data.ths.{spec.key}", "schedule_templates": schedule_templates(spec),
        "storage": "source_observations", "cross_source_identity": False,
        "backtest_ready": False} for spec in DATASETS.values()]}


@router.get("/tickers")
def tickers(asset_type: str | None = None, keyword: str | None = Query(None, max_length=128),
            limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
            session: Session = Depends(get_db_session)):
    conditions = []
    if asset_type:
        conditions.append(Ticker.asset_type == asset_type)
    if keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(Ticker.thscode.ilike(f"%{escaped}%", escape="\\") | Ticker.name.ilike(f"%{escaped}%", escape="\\"))
    total = session.scalar(select(func.count()).select_from(Ticker).where(*conditions))
    rows = session.scalars(select(Ticker).where(*conditions).order_by(Ticker.thscode).limit(limit).offset(offset))
    return {"source": "tonghuashun", "total": total, "limit": limit, "offset": offset,
            "items": [{"thscode": row.thscode, "asset_type": row.asset_type, "name": row.name,
                "exchange": row.exchange, "list_status": None, "first_seen_at": row.first_seen_at,
                "last_seen_at": row.last_seen_at, "provider_fields": json.loads(row.raw_json, parse_float=str)} for row in rows]}


@router.get("/states")
def states(dataset: str | None = None, subject: str | None = None,
           status: str | None = None, limit: int = Query(50, ge=1, le=200),
           offset: int = Query(0, ge=0), session: Session = Depends(get_db_session)):
    conditions = []
    for field, value in ((State.dataset, dataset), (State.subject, subject), (State.status, status)):
        if value is not None:
            conditions.append(field == value)
    if dataset:
        require_dataset(dataset)
    total = session.scalar(select(func.count()).select_from(State).where(*conditions))
    rows = session.scalars(select(State).where(*conditions).order_by(State.dataset, State.subject, State.variant).limit(limit).offset(offset))
    return {"source": "tonghuashun", "total": total, "limit": limit, "offset": offset,
        "items": [{"dataset": r.dataset, "subject": r.subject, "variant": r.variant,
            "status": r.status, "revision": r.revision, "version_id": r.observation_id,
            "attempted_at": r.attempted_at, "succeeded_at": r.succeeded_at,
            "reconciled_at": r.reconciled_at, "error_kind": r.error_kind} for r in rows]}


def observation_page(row, limit, offset):
    # Floating-point JSON numbers are returned as decimal strings deliberately;
    # integral provider fields remain integers, null remains unknown.
    data = json.loads(row.data_json, parse_float=str)
    records = data.pop("item", [])
    return {"source": "tonghuashun", "dataset": row.dataset, "subject": row.subject,
        "version_id": row.id, "observed_at": row.observed_at,
        "decimal_encoding": "string", "backtest_ready": False,
        "metadata": data, "requests": json.loads(row.request_json),
        "total": len(records), "limit": limit, "offset": offset, "items": records[offset:offset + limit]}


@router.get("/versions/{version_id}")
def version(version_id: UUID, limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0),
            session: Session = Depends(get_db_session)):
    row = session.get(Observation, version_id)
    if row is None:
        raise HTTPException(404, "采集版本不存在。")
    return observation_page(row, limit, offset)


@router.get("/{dataset}/{subject}/versions")
def versions(dataset: str, subject: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
             session: Session = Depends(get_db_session)):
    require_dataset(dataset)
    conditions = (Observation.dataset == dataset, Observation.subject == subject)
    total = session.scalar(select(func.count()).select_from(Observation).where(*conditions))
    rows = session.scalars(select(Observation).where(*conditions).order_by(
        Observation.observed_at.desc(), Observation.id.desc()).limit(limit).offset(offset))
    return {"source": "tonghuashun", "total": total, "limit": limit, "offset": offset,
        "items": [{"version_id": r.id, "observed_at": r.observed_at, "row_count": r.row_count,
                   "content_hash": r.content_hash} for r in rows]}


@router.get("/{dataset}/{subject}")
def latest(dataset: str, subject: str, limit: int = Query(50, ge=1, le=1000), offset: int = Query(0, ge=0),
           session: Session = Depends(get_db_session)):
    require_dataset(dataset)
    state = session.get(State, (dataset, subject, "default"))
    if state is None or state.observation_id is None:
        raise HTTPException(404, "该范围尚无成功采集版本，请先查看采集状态。")
    result = observation_page(session.get(Observation, state.observation_id), limit, offset)
    return {**result, "latest_attempt_status": state.status, "error_kind": state.error_kind}
