"""Resumable subject acquisition with short publication transactions.

Network calls never hold catalogue or version-head row locks. Only the explicit
provider request-budget transaction spans network I/O, serializing worker
processes without imposing a lock on readers of already published data.
"""

from datetime import UTC, datetime, timedelta
import time

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
import structlog

from app.data_ingestion.clients.tonghuashun import TonghuashunError
from app.data_ingestion.models.tonghuashun import TonghuashunRequestBudget
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.contracts import CollectionError, CollectionParameters, DATASETS, SHANGHAI
from app.data_ingestion.tonghuashun.repository import CollectionRepository

logger = structlog.get_logger(__name__)


def aware(value):
    # SQLite fixtures omit tzinfo; production columns always use timestamptz.
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class BudgetedClient:
    def __init__(self, client, engine):
        self.client, self.engine = client, engine

    def request(self, interface, params):
        if self.engine.dialect.name != "postgresql":
            return self.client.request(interface, params)
        failure = None
        with Session(self.engine) as session:
            session.execute(text("SET LOCAL lock_timeout = '60s'"))
            session.execute(insert(TonghuashunRequestBudget).values(key="global",
                next_allowed_at=datetime.now(UTC)).on_conflict_do_nothing())
            budget = session.scalar(select(TonghuashunRequestBudget).where(
                TonghuashunRequestBudget.key == "global").with_for_update())
            delay = max(0, (budget.next_allowed_at - datetime.now(UTC)).total_seconds())
            if delay > 60:
                raise TonghuashunError("rate_limited", retry_after=delay)
            if delay:
                time.sleep(delay)
            try:
                result = self.client.request(interface, params)
            except TonghuashunError as exc:
                failure = exc
            # Commit a vendor cooldown even when the request failed. This is
            # shared by different scheduler processes, unlike the local gate.
            cooldown = max(self.client.interval_ms / 1000,
                max(1, failure.retry_after) if failure and failure.kind == "rate_limited" else 0)
            budget.next_allowed_at = datetime.now(UTC) + timedelta(seconds=cooldown)
            session.commit()
        if failure:
            raise failure from None
        return result


def due(spec, previous, parameters, now):
    if parameters.refresh_today or parameters.subjects or parameters.start_date or parameters.end_date:
        return True
    if parameters.mode == "reconcile" and previous.data is None:
        return False  # Bootstrap belongs to the independent acquisition queue.
    if previous.status in ("pending", "failed", "partial"):
        return True
    if parameters.mode == "backfill":
        return previous.data is None
    last = aware(previous.reconciled_at if parameters.mode == "reconcile" else previous.succeeded_at)
    local = now.astimezone(SHANGHAI)
    boundary = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if parameters.mode == "reconcile":
        # ETF adjusted history is audited weekly; other histories monthly.
        boundary = (boundary - timedelta(days=(local.weekday() + 1) % 7)
                    if spec.key == "etf_daily" else boundary.replace(day=1))
        boundary = boundary.replace(hour=3)
        if local < boundary:
            boundary = (boundary - timedelta(days=7) if spec.key == "etf_daily" else
                        (boundary - timedelta(days=1)).replace(day=1))
    elif spec.frequency == "weekly":
        boundary -= timedelta(days=(local.weekday() + 1) % 7)
    else:
        hour, minute = (22, 30) if spec.kind == "nav" else (20, 30) if spec.kind == "bars" else (20, 0)
        boundary = boundary.replace(hour=hour, minute=minute)
        if spec.kind == "nav" and local.hour >= 9 and local < boundary:
            boundary = boundary.replace(hour=9, minute=0)
        if local < boundary:
            boundary -= timedelta(days=1)
    return last is None or last < boundary


def collect(dataset: str, parameters: CollectionParameters, client, engine,
            *, now: datetime | None = None) -> dict:
    spec = DATASETS[dataset]
    if spec.kind.startswith("m3_"):
        from app.data_ingestion.tonghuashun.research_service import collect_research
        return collect_research(dataset, parameters, client, engine, now=now)
    if spec.kind == "dump":
        from app.data_ingestion.tonghuashun.dump_service import collect_dump
        return collect_dump(dataset, parameters, client, engine, now=now)
    now = now or datetime.now(UTC)
    # Explicit historical queries cannot mutate the default rolling collection.
    # Their independent version heads are discoverable through the states API.
    variant = (f"{parameters.start_date or 'auto'}_{parameters.end_date or 'auto'}"
               if parameters.start_date or parameters.end_date else "default")
    if parameters.start_date or parameters.end_date:
        if spec.kind not in ("bars", "financials", "indicators", "reports"):
            raise CollectionError("该数据集仅提供快照或固定最近窗口，不支持指定日期回补。")
    with Session(engine) as session:
        repo = CollectionRepository(session)
        if spec.kind == "directory":
            subjects = list(parameters.asset_types)
            if parameters.subjects:
                raise CollectionError("目录采集按资产类型执行，不接受单标的筛选。")
        elif spec.kind == "calendar":
            subjects = ["CN"]
            if parameters.subjects:
                raise CollectionError("交易日历没有标的参数。")
        elif spec.kind == "index_catalog":
            subjects = ["cn_concept", "region", "tszs", "industry"]
            if parameters.subjects:
                if set(parameters.subjects) - set(subjects):
                    raise CollectionError("指数分类标签不符合接口要求。")
                subjects = parameters.subjects
        else:
            assets = tuple(a for a in spec.assets if a in parameters.asset_types)
            subjects = (repo.related(spec.identity) if spec.kind in ("company", "manager")
                        else repo.subjects(assets))
            if parameters.subjects:
                if set(parameters.subjects) - set(subjects):
                    raise CollectionError("指定标的尚未进入同花顺目录或关联资料，或不在本接口适用范围。")
                subjects = parameters.subjects
        if not subjects:
            raise CollectionError("没有可采集对象，请先完成同花顺标的目录及所需基金基础资料。")
    summary = {"source": "tonghuashun", "dataset": dataset, "subjects": len(subjects),
               "succeeded": 0, "failed": 0, "skipped": 0, "pending": 0, "received": 0,
               "changed": 0, "unchanged": 0, "removed": 0}
    client = BudgetedClient(client, engine)
    # Rotate attempts rather than repeatedly selecting the first failing codes.
    # Bounded batches release scheduler slots so routine updates can run before
    # the next low-priority bootstrap batch.
    from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState
    with Session(engine) as session:
        attempts = dict(session.execute(select(TonghuashunCollectionState.subject,
            TonghuashunCollectionState.attempted_at).where(TonghuashunCollectionState.dataset == dataset,
            TonghuashunCollectionState.variant == variant)).all())
    subjects.sort(key=lambda subject: (aware(attempts.get(subject)) or datetime.min.replace(tzinfo=UTC), subject))
    for subject in subjects:
        with Session(engine) as session:
            previous = CollectionRepository(session).read(dataset, subject, variant, with_data=False)
        if not due(spec, previous, parameters, now):
            summary["skipped"] += 1
            continue
        if summary["succeeded"] + summary["failed"] >= parameters.batch_size:
            summary["pending"] += 1
            continue
        with Session(engine) as session:
            previous = CollectionRepository(session).read(dataset, subject, variant)
        if not due(spec, previous, parameters, now):
            summary["skipped"] += 1
            continue
        acquisition = Acquisition(client)
        try:
            data = acquisition.fetch(spec, subject, parameters, previous.data, now)
            if acquisition.failures:
                data = {**data, "failed_requests": acquisition.failures}
            published_at = datetime.now(UTC)
            with Session(engine) as session:
                result = CollectionRepository(session).publish(dataset, subject, variant,
                    expected=previous.revision, data=data, requests=acquisition.requests,
                    now=published_at, ticker_rows=data["item"] if spec.kind == "directory" else None,
                    reconcile=parameters.mode == "reconcile")
                session.commit()
            summary["failed" if acquisition.failures else "succeeded"] += 1
            result["fetched_count"] = acquisition.fetched_count
            for field in ("received", "changed", "unchanged", "removed"):
                summary[field] += result[field]
            log_result(spec, subject, parameters, data, result, not acquisition.failures,
                       "partial_reports" if acquisition.failures else None)
        except (CollectionError, TonghuashunError) as exc:
            kind = exc.kind if isinstance(exc, TonghuashunError) else "invalid_data"
            with Session(engine) as session:
                try:
                    CollectionRepository(session).fail(dataset, subject, variant,
                        expected=previous.revision, kind=kind, now=datetime.now(UTC))
                    session.commit()
                except CollectionError:
                    session.rollback()  # A newer run owns the visible state.
            summary["failed"] += 1
            log_result(spec, subject, parameters, None, {"fetched_count": acquisition.fetched_count}, False, kind,
                       error_message=str(exc))
            if kind in ("unauthenticated", "forbidden", "rate_limited"):
                # Account-wide failures must not repeat thousands of times.
                break
    if summary["failed"]:
        raise CollectionError(
            f"{spec.name}部分失败：成功 {summary['succeeded']} 个，失败 {summary['failed']} 个，"
            f"已跳过 {summary['skipped']} 个，剩余 {len(subjects)-summary['succeeded']-summary['failed']-summary['skipped']} 个未执行；"
            f"拉取 {summary['received']} 条，变更 {summary['changed']} 条，未变更 {summary['unchanged']} 条；"
            "成功标的完成标记已推进，失败及未执行标的未推进，下次可续采。")
    summary["event"] = "tonghuashun_collection_completed"
    summary["message"] = (f"{spec.name}本批采集完成：日期范围按接口及任务参数，成功 {summary['succeeded']} 个，"
        f"已跳过 {summary['skipped']} 个，读取版本记录 {summary['received']} 条，变更 {summary['changed']} 条，"
        f"未变更 {summary['unchanged']} 条，失败 0 个，待后续采集 {summary['pending']} 个；"
        + ("本次成功标的完成标记已推进。" if summary["succeeded"] else "本期范围已完成，完成标记未重复推进。"))
    return summary


def log_result(spec, subject, parameters, data, result, succeeded, kind=None, error_message=None):
    start = (data or {}).get("requested_start") or (data or {}).get("observed_start") or (parameters.start_date.isoformat() if parameters.start_date else None)
    end = (data or {}).get("requested_end") or (data or {}).get("observed_end") or (parameters.end_date.isoformat() if parameters.end_date else None)
    date_label = f"{start or '接口可用起点'} 至 {end or '接口可用终点'}" if start or end else "接口返回范围（快照按采集时间记录）"
    counts = {"fetched_count": result.get("fetched_count", 0), "changed_count": result.get("changed", 0),
              "unchanged_count": result.get("unchanged", 0), "failed_count": 0 if succeeded else 1}
    checkpoint_message = ("已推进至版本 " + result["version_id"] if succeeded else
        "完整范围未推进，已保存成功报告，失败报告待补采" if kind == "partial_reports" else
        "未推进，保留已有成功版本")
    write = logger.info if succeeded else logger.warning
    write("tonghuashun_collection_completed" if succeeded else "tonghuashun_collection_failed",
        title=f"{spec.name}{'采集完成' if succeeded else '采集失败'}",
        message=(f"{spec.name}采集{'完成' if succeeded else '失败'}：标的 {subject}，日期范围 {date_label}，"
                 f"拉取 {counts['fetched_count']} 条，变更 {counts['changed_count']} 条，"
                 f"未变更 {counts['unchanged_count']} 条，失败 {counts['failed_count']} 个；"
                 f"checkpoint {checkpoint_message}。"),
        source="tonghuashun", data_type=spec.key, subject=subject,
        start_date=start, end_date=end, checkpoint_advanced=succeeded,
        version_id=result.get("version_id"), error_type=kind,
        error_message=error_message, exc_info=error_message is not None, **counts)
