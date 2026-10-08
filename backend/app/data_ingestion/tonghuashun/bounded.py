"""One supervised, source-local ETF/index diagnosis through the official handler.

The supervisor never loads credentials or a database connection. Its sole child
owns the collector, and a fresh private directory stores operational evidence,
not provider rows. A killed child is not evidence that publication did not occur.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import UUID, uuid4

MAX_SECONDS = 180.0
MODULE = "app.data_ingestion.tonghuashun.bounded"
RESULT_LIMIT = 16 * 1024
DATASETS = ("index_daily", "etf_daily")
COUNT_KEYS = ("logical_requests", "http_attempts", "reused")
ROW_COUNT_KEYS = ("fetched_rows", "received", "changed", "succeeded", "failed", "skipped")
OUTCOMES = {"succeeded", "failed", "blocked", "cancelled", "budget", "worker_error"}
PROBLEMS = {"diagnosis_in_flight", "source_unavailable", "native_identity_unavailable",
            "native_identity_unverifiable", "variant_exists", "checkpoint_exists",
            "active_scope_unverifiable", "collector_in_flight", "no_completed_scope",
            "request_budget_violation", "worker_boundary_error", "active_run_limit",
            "baseline_changed", "baseline_unverifiable"}
LOCAL_REASONS = {
    "响应包含非有限数值。", "响应日期须为毫秒整数。", "响应日期超出支持范围。",
    "同花顺响应缺少合法记录列表。", "同花顺返回空记录，尚不能确认数据已就绪。",
    "历史数据包含重复日期或范围外记录。", "历史行情响应标的与请求不一致。",
    "分段行情的重叠日期不一致，未发布可能混合复权基准的版本。",
    "历史行情为空，尚不能确认该范围覆盖。",
    "前复权历史重采缺少已有日期，未发布不完整或混合基准版本。",
    "行情最高价、最低价与开收盘价不一致。",
    '股票单日修复不能混合复权基准。',
    '股票修复响应未实际返回确切日期键。',
    '本次真实目录未确认目标报告，未使用旧目录回退。',
    '本次目录出现未批准且未采集的其他报告。',
    '本次目录改变了未尝试历史报告的期间。',
    '持仓修复目录期间校验失败。', '持仓修复响应未通过整组完整校验。',
    '持仓修复目录主体或完整范围不一致。', '持仓修复响应主体与目标不一致。',
    '财报修复响应主体或完整窗口不一致。', '财报修复响应未命中原始确切期间。',
    '财报修复响应完整窗口校验失败。', '财报修复响应缺少完整且一致的实际期间。',
    '原默认版本的失败请求结构无法核对。', '原默认报告与声明目录无法核对。',
}


@dataclass(frozen=True)
class Scope:
    dataset: str
    subject: str
    start_date: date
    end_date: date

    def parameters(self):
        """Validate the native identity and the exact registered task schema."""
        from app.data_ingestion.tonghuashun.contracts import CODE
        from app.scheduling.registry import task_registry
        if self.dataset not in DATASETS or CODE.fullmatch(self.subject) is None:
            raise ValueError("invalid_scope")
        return task_registry.require(self.task_type).parameters_model.model_validate({
            "subjects": [self.subject], "asset_types": [self.asset_type],
            "start_date": self.start_date, "end_date": self.end_date,
            "mode": "incremental", "refresh_today": False, "batch_size": 1,
            "max_requests": 1, "max_seconds": 180,
        })

    @property
    def task_type(self):
        return f"data.ths.{self.dataset}"

    @property
    def asset_type(self):
        return "fund-etf" if self.dataset == "etf_daily" else "a-share-index"

    @property
    def variant(self):
        return f"{self.start_date.isoformat()}_{self.end_date.isoformat()}"

    def as_dict(self):
        return dict(dataset=self.dataset, subject=self.subject, asset_type=self.asset_type,
                    start_date=self.start_date.isoformat(), end_date=self.end_date.isoformat(),
                    variant=self.variant)

    max_requests = 1
    max_http_attempts = 3


@dataclass(frozen=True)
class RepairScope:
    """A typed selection of an existing default head, with no variant knob."""
    selection: Any

    @classmethod
    def from_dict(cls, value):
        from app.data_ingestion.tonghuashun.control import DefaultRepair
        return cls(DefaultRepair.from_dict(value))

    @property
    def dataset(self):
        return self.selection.dataset

    @property
    def subject(self):
        return self.selection.subject

    @property
    def asset_type(self):
        return self.selection.asset_type

    @property
    def start_date(self):
        return self.selection.day or self.selection.start_date or self.selection.end_date

    @property
    def end_date(self):
        return self.selection.day or self.selection.end_date

    @property
    def task_type(self):
        return f'data.ths.{self.dataset}'

    @property
    def variant(self):
        return 'default'

    @property
    def max_requests(self):
        return self.selection.request_limit

    @property
    def max_http_attempts(self):
        return self.selection.request_limit

    def parameters(self):
        from app.data_ingestion.tonghuashun.control import DefaultRepair
        from app.scheduling.registry import task_registry
        if not isinstance(self.selection, DefaultRepair):
            raise ValueError('invalid_repair_selection')
        self.selection.validate()
        model = task_registry.require(self.task_type).parameters_model
        values = {'subjects': [self.subject], 'asset_types': [self.asset_type],
                  'refresh_today': False, 'batch_size': 1,
                  'max_requests': self.max_requests, 'max_seconds': 180}
        # Snapshot handlers do not advertise dates or mode. Their common
        # incremental default remains intact; the typed selector is not a new
        # provider filter or a new public scheduler parameter.
        if 'mode' in model.model_fields:
            values['mode'] = 'incremental'
        return model.model_validate(values)

    def as_dict(self):
        return {**self.selection.as_dict(), 'variant': 'default', 'mode': 'default_repair'}


class AdmissionRejected(Exception):
    """Fixed local codes, never provider or database exception messages."""


def _scope_hash(scope: Scope) -> str:
    from app.data_ingestion.tonghuashun.contracts import CollectionParameters
    from app.data_ingestion.tonghuashun.control import collection_scope_hash, default_repair
    parameters = CollectionParameters.model_validate(scope.parameters().model_dump())
    token = default_repair.set(scope.selection if isinstance(scope, RepairScope) else None)
    try:
        revision = scope.selection.revision if isinstance(scope, RepairScope) else 0
        return collection_scope_hash(scope.dataset, scope.subject, scope.variant, revision, parameters)
    finally:
        default_repair.reset(token)


@contextmanager
def admission(engine, scope: Scope):
    """Release the source gate after admission; retain only our scope lease.

    The session advisory lock excludes another bounded diagnosis, not ordinary
    collectors. Existing source-head CAS remains responsible for that race.
    No pending state, task, schedule, or successful receipt is manufactured.
    """
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session
    from app.data_sources.models import DataSourceConfig
    from app.data_sources.service import configured
    from app.data_ingestion.models.tonghuashun import (
        TonghuashunTicker, TonghuashunCollectionState, TonghuashunObservation,
        TonghuashunWorkUnit,
    )
    from app.data_ingestion.tonghuashun.control import check_execution
    from app.scheduling.models import TaskRun
    check_execution()
    key = int.from_bytes(hashlib.sha256(("ths-bounded:" + _scope_hash(scope)).encode()).digest()[:8], "big") & ((1 << 63) - 1)
    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        locked = False
        try:
            if postgres:
                connection.execute(text("SET statement_timeout = '5s'"))
                connection.execute(text("SET lock_timeout = '5s'"))
                locked = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
                connection.commit()
                if not locked:
                    raise AdmissionRejected("diagnosis_in_flight")
            with Session(bind=connection) as session:
                source = session.scalar(select(DataSourceConfig).where(
                    DataSourceConfig.key == "tonghuashun").with_for_update())
                if source is None or not source.enabled or not configured(source):
                    raise AdmissionRejected("source_unavailable")
                ticker = session.get(TonghuashunTicker, scope.subject)
                if ticker is None or ticker.asset_type != scope.asset_type:
                    raise AdmissionRejected("native_identity_unavailable")
                raw = json.loads(ticker.raw_json)
                if not isinstance(raw, dict) or raw.get("thscode") != scope.subject or raw.get("asset_type") != scope.asset_type:
                    raise AdmissionRejected("native_identity_unverifiable")
                identity = (scope.dataset, scope.subject, scope.variant)
                repair = isinstance(scope, RepairScope)
                if repair:
                    from app.data_ingestion.tonghuashun.contracts import CollectionConflict, CollectionError
                    try:
                        scope.selection.baseline(session)
                    except CollectionConflict:
                        raise AdmissionRejected('baseline_changed') from None
                    except CollectionError:
                        raise AdmissionRejected('baseline_unverifiable') from None
                elif session.get(TonghuashunCollectionState, identity) is not None:
                    raise AdmissionRejected("variant_exists")
                if not repair and session.scalar(select(TonghuashunObservation.id).where(
                    TonghuashunObservation.dataset == scope.dataset,
                    TonghuashunObservation.subject == scope.subject,
                    TonghuashunObservation.variant == scope.variant).limit(1)) is not None:
                    raise AdmissionRejected("variant_exists")
                if session.scalar(select(TonghuashunWorkUnit.scope).where(
                    TonghuashunWorkUnit.scope == _scope_hash(scope)).limit(1)) is not None:
                    raise AdmissionRejected("checkpoint_exists")
                active = list(session.scalars(select(TaskRun.parameters).where(
                    TaskRun.task_type == scope.task_type, TaskRun.status.in_(("queued", "running"))).limit(201)))
                if len(active) > 200:
                    raise AdmissionRejected("active_run_limit")
                for parameters in active:
                    if not isinstance(parameters, dict):
                        raise AdmissionRejected("active_scope_unverifiable")
                    start, end = parameters.get("start_date"), parameters.get("end_date")
                    if repair:
                        # Ordinary default work overlaps this repair, unlike a
                        # new dated diagnosis. The advisory is still only ours;
                        # publication CAS handles collectors queued after here.
                        if start is not None or end is not None:
                            continue
                        assets = parameters.get('asset_types')
                        if assets is not None:
                            if not isinstance(assets, list) or not assets or any(type(a) is not str for a in assets):
                                raise AdmissionRejected('active_scope_unverifiable')
                            if scope.asset_type not in assets:
                                continue
                    else:
                        if start is None and end is None:
                            continue
                        if (start, end) != (scope.start_date.isoformat(), scope.end_date.isoformat()):
                            continue
                    subjects = parameters.get("subjects")
                    if subjects is None or not isinstance(subjects, list) or not subjects or any(type(s) is not str for s in subjects) or scope.subject in subjects:
                        raise AdmissionRejected("collector_in_flight")
                proof = dict(source_version=source.version, native_asset_type=ticker.asset_type,
                             native_identity_sha256=hashlib.sha256(ticker.raw_json.encode()).hexdigest(),
                             new_explicit_variant=not repair, original_default_variant=repair,
                             scope_lease="postgresql" if postgres else "fixture")
                check_execution()
                session.commit()
            yield proof
        finally:
            # A killed process releases its connection-owned advisory lock.
            # Normal cleanup never changes the global vendor pacing row.
            connection.rollback()
            if locked:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()


def _counts(limit) -> dict[str, int | None]:
    values = getattr(limit, "counts", {})
    return {key: values[key] if type(values.get(key)) is int and values[key] >= 0 else None for key in COUNT_KEYS}


def _row_counts(limit) -> dict[str, int | None]:
    values = getattr(limit, "detail", {})
    return {key: values[key] if type(values.get(key)) is int and values[key] >= 0 else None for key in ROW_COUNT_KEYS}


def _response_evidence(limit):
    """Whitelisted shape/digest evidence is not an actual returned-key receipt."""
    value = getattr(limit, "response_evidence", None)
    if not isinstance(value, dict):
        return None
    safe: dict[str, Any] = {}
    for key in ("digest", "sha256"):
        if type(value.get(key)) is str and re.fullmatch(r"[0-9a-f]{64}", value[key]):
            safe[key] = value[key]
    if type(value.get("row_count")) is int and value["row_count"] >= 0:
        safe["row_count"] = value["row_count"]
    if type(value.get("response_bytes")) is int and value["response_bytes"] >= 0:
        safe["response_bytes"] = value["response_bytes"]
    for key in ("shape", "type"):
        if value.get(key) in ("object", "array", "dict", "list", "null", "invalid", "missing", "object_with_item_list", "other"):
            safe[key] = value[key]
    # Keep only numeric type/text pairs from the collector's bounded projection.
    # Text must itself be a short number; reflected strings cannot enter it.
    if isinstance(value.get("numeric_samples"), list):
        types = []
        for sample in value["numeric_samples"][:5]:
            if not isinstance(sample, dict) or sample.get("shape") not in ("object", "non_object"):
                continue
            item = {"shape": sample["shape"], "numeric_fields": {}}
            fields = sample.get("numeric_fields", {})
            if isinstance(fields, dict):
                for name in ("date_ms", "open_price", "high_price", "low_price", "close_price", "volume", "turnover"):
                    field = fields.get(name)
                    if isinstance(field, dict) and field.get("type") in ("null", "bool", "int", "decimal", "other"):
                        safe_field = {"type": field["type"], "value_omitted": True}
                        number = field.get("value_text")
                        pattern = r"[+-]?\d+" if field["type"] == "int" else r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
                        if field["type"] in ("int", "decimal") and type(number) is str and len(number) <= 64 and re.fullmatch(pattern, number, flags=re.ASCII):
                            safe_field = {"type": field["type"], "value_text": number}
                        item["numeric_fields"][name] = safe_field
            if type(sample.get("subject_matches_request")) is bool:
                item["subject_matches_request"] = sample["subject_matches_request"]
            types.append(item)
        safe.update(numeric_samples=types, sample_limit=5, full_payload_exported=False)
    return safe or None


def _publication(engine, scope: Scope, owned_observation_id=None):
    from sqlalchemy.orm import Session
    from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState, TonghuashunObservation
    with Session(engine) as session:
        state = session.get(TonghuashunCollectionState, (scope.dataset, scope.subject, scope.variant))
        if state is None:
            return {"status": "none"}
        if state.status not in ("pending", "failed", "partial", "succeeded", "empty") or type(state.revision) is not int or state.revision < 0:
            return {"status": "unknown"}
        result = dict(status="none", state_revision=state.revision, state_status=state.status)
        if state.observation_id:
            if isinstance(scope, RepairScope):
                if owned_observation_id is None:
                    # An unchanged old head is not a new repair. A different
                    # head without our committed ID remains attribution-unknown.
                    return result if state.observation_id == scope.selection.observation_id else {'status': 'unknown'}
                if (str(state.observation_id) != owned_observation_id
                        or state.revision != scope.selection.revision + 1):
                    return {'status': 'unknown'}
            observation = session.get(TonghuashunObservation, state.observation_id)
            if observation is None or (observation.dataset, observation.subject, observation.variant) != (scope.dataset, scope.subject, scope.variant):
                return {"status": "unknown"}
            if type(observation.row_count) is not int or observation.row_count < 0 or re.fullmatch(r"[0-9a-f]{64}", observation.content_hash or "") is None:
                return {"status": "unknown"}
            result.update(status="verified", observation_id=str(observation.id),
                          row_count=observation.row_count, content_sha256=observation.content_hash)
        return result


def _write(path: Path, value: dict):
    """Atomically replace small, private, explicitly assembled evidence only."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()
    if len(encoded) > RESULT_LIMIT:
        raise ValueError("evidence_too_large")
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
    os.replace(temporary, path)


def _base(scope: Scope, operation_id: str):
    return dict(protocol="ths-bounded@1", operation_id=operation_id, scope=scope.as_dict(),
                provenance=dict(handler=scope.task_type, max_requests=scope.max_requests,
                                max_http_attempts=scope.max_http_attempts,
                                max_seconds=180, storage="existing_source_local"))


def _failure_request(scope: Scope, context):
    if isinstance(scope, RepairScope):
        if (not isinstance(context, dict) or context.get('stage') not in ('request', 'response_validation', 'history_validation')
                or (context.get('interface'), context.get('parameters')) not in scope.selection.allowed_requests()):
            return None
        return {key: context[key] for key in ('interface', 'parameters', 'stage')}
    from app.data_ingestion.tonghuashun.contracts import DATASETS as specs
    from app.data_ingestion.tonghuashun.service import bounded_bar_failure
    return bounded_bar_failure(specs[scope.dataset], scope.subject, context)


def _reason(kind, message):
    from app.data_ingestion.clients.tonghuashun import ERRORS
    if type(kind) is str and kind in ERRORS:
        return ERRORS[kind][0]
    if kind != "invalid_data" or type(message) is not str:
        return None
    if message in LOCAL_REASONS:
        return message
    match = re.fullmatch(r"行情或净值存在缺失、负值或非法数值：日期 (\d{4}-\d{2}-\d{2})，字段 (open_price|high_price|low_price|close_price|volume|turnover)，原因 (缺失|布尔类型|非数值类型|非有限数值|负值)。", message, flags=re.ASCII)
    if match:
        try:
            date.fromisoformat(match[1])
            return message
        except ValueError:
            pass
    return None


def _message(scope: Scope, record: dict):
    names = {"succeeded": "采集完成", "failed": "采集失败", "blocked": "准入或完成检查未通过",
             "cancelled": "已取消", "budget": "预算用尽", "worker_error": "执行未完整确认",
             "timed_out": "达到硬上限", "supervisor_error": "监督未完整确认"}
    rows = record.get("row_counts", {})
    def number(key):
        return str(rows[key]) if type(rows.get(key)) is int else "未确认"
    publication = record.get("publication", {}).get("status")
    repair = isinstance(scope, RepairScope)
    checkpoint = ("已观察到本次原默认源版本" if repair else "已观察到该显式范围源版本") if publication == "verified" else "未完整确认" if publication == "unknown" else "未推进源版本"
    from app.data_ingestion.tonghuashun.contracts import DATASETS as specs
    label = specs[scope.dataset].name
    action = '原默认目标修复' if repair else '单范围诊断'
    return (f"{label}{action}：标的 {scope.subject}，日期 {scope.start_date} 至 {scope.end_date}，"
            f"{names.get(record.get('outcome'), '未完整确认')}；拉取 {number('fetched_rows')} 条，"
            f"变更 {number('changed')} 条，失败 {number('failed')} 个；checkpoint {checkpoint}；"
            "未推进全量核对完成标记或底座检查点。")


def _tag_engine(engine, operation_id: str):
    """Give owned PG sessions a non-secret tag for external cleanup verification."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy import event
    def connecting(_dialect, _record, _args, parameters):
        parameters["connect_timeout"] = 5
        parameters["application_name"] = "ths-bounded:" + operation_id
        # These are owned connection settings, not ALTER DATABASE/ROLE or a
        # persistent provider policy. Preserve unrelated existing options.
        parameters["options"] = (parameters.get("options", "") + " -cstatement_timeout=5000 -clock_timeout=5000").strip()
    def connected(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('application_name', %s, false)",
                           ("ths-bounded:" + operation_id,))
        connection.commit()
    event.listen(engine, "connect", connected)
    event.listen(engine, "do_connect", connecting)


def run_worker(scope: Scope, output: Path, operation_id: str, deadline: float) -> int:
    """Internal child protocol: credentials remain inside the formal handler."""
    import structlog
    from app.db.session import get_engine
    from app.data_ingestion.tonghuashun.control import CollectionYield, ExecutionDeadline, execution_deadline, default_repair
    from app.scheduling.registry import TaskContext, task_registry
    scope.parameters()  # Reject malformed selectors before installing signals or opening a DB session.
    cancelled = threading.Event()
    old_signal = signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    limit = ExecutionDeadline(deadline=min(deadline, time.monotonic() + MAX_SECONDS), cancelled=cancelled.is_set,
                              max_http_attempts=scope.max_http_attempts)
    token = execution_deadline.set(limit)
    repair_token = default_repair.set(scope.selection if isinstance(scope, RepairScope) else None)
    record = _base(scope, operation_id)
    record.update(phase="starting", outcome="worker_error", publication={"status": "not_attempted"},
                  counts=_counts(limit), counts_complete=True)
    old_logging = structlog.get_config()
    # Drop all logs at the child boundary. Only a revalidated event/code below
    # survives: no exception chain, request headers, URL, message, or row values.
    diagnostic: dict[str, Any] = {}
    def capture(_logger, _method, event):
        if event.get("data_type") == scope.dataset and event.get("subject") == scope.subject:
            from app.data_ingestion.clients.tonghuashun import BUSINESS_ERRORS, ERRORS
            kind, code = event.get("error_type"), event.get("supplier_business_code")
            if kind in set(ERRORS) | {"invalid_data", "partial_reports"}:
                diagnostic["error_kind"] = kind
                if type(code) is int and BUSINESS_ERRORS.get(code) == kind:
                    diagnostic["supplier_business_code"] = code
            context = _failure_request(scope, event.get("failure_request"))
            if context is not None:
                diagnostic.update(failure_stage=context["stage"], failure_request=context)
            reason = _reason(kind, event.get("error_message"))
            if reason is not None:
                diagnostic["reason"] = reason
        raise structlog.DropEvent
    structlog.configure(processors=[capture], cache_logger_on_first_use=False)
    engine = None
    entered = False
    owned_observation_id = None
    try:
        _write(output / "worker.json", record)
        parameters = scope.parameters()
        engine = get_engine()
        _tag_engine(engine, operation_id)
        record["provenance"]["db_application_name"] = "ths-bounded:" + operation_id
        with admission(engine, scope) as proof:
            record["provenance"].update(proof)
            record.update(phase="handler", publication={"status": "unknown"}, counts_complete=False,
                          counts={key: None for key in COUNT_KEYS})
            _write(output / "worker.json", record)
            entered = True
            try:
                result = task_registry.require(scope.task_type).handler(
                    TaskContext(task_id=None, run_id=None, task_type=scope.task_type), parameters)
                if isinstance(scope, RepairScope) and isinstance(result, dict):
                    owned_observation_id = result.get('repair_publication_id')
                reason = result.get("yield_reason") if isinstance(result, dict) else None
                record["outcome"] = "cancelled" if reason == "stopped" else "budget" if reason == "budget" else "succeeded"
                if reason is None and (not isinstance(result, dict) or result.get("succeeded") != 1 or result.get("failed") != 0):
                    record.update(outcome="blocked", problem="no_completed_scope")
            except CollectionYield as exc:
                record["outcome"] = "cancelled" if exc.reason == "stopped" else "budget"
            except Exception:
                # The fixed event or verified state establishes source failure;
                # an arbitrary exception is never copied or inferred successful.
                record["outcome"] = "cancelled" if cancelled.is_set() else "failed" if diagnostic else "worker_error"
            record.update(counts=_counts(limit), counts_complete=all(v is not None for v in _counts(limit).values()),
                          row_counts=_row_counts(limit), diagnostic=diagnostic,
                          response_evidence=_response_evidence(limit))
            current = getattr(limit, "detail", {}).get("current_request")
            if isinstance(current, dict):
                from app.data_ingestion.tonghuashun.contracts import DATASETS as specs
                interface = limit.detail.get('current_interface') if isinstance(scope, RepairScope) else specs[scope.dataset].interface
                stage = limit.detail.get('request_stage', 'request') if isinstance(scope, RepairScope) else 'request'
                generated = _failure_request(scope, {"interface": interface,
                    "parameters": current, "stage": stage})
                if generated is not None:
                    record["provenance"]["last_generated_request"] = generated
                    if diagnostic and isinstance(scope, RepairScope):
                        diagnostic.update(failure_stage=stage, failure_request=generated)
            if (record["counts"]["logical_requests"] or 0) > scope.max_requests or (record["counts"]["http_attempts"] or 0) > scope.max_http_attempts:
                record.update(outcome="worker_error", problem="request_budget_violation")
            try:
                record["publication"] = _publication(engine, scope, owned_observation_id) if isinstance(scope, RepairScope) else _publication(engine, scope)
            except Exception:
                record["publication"] = {"status": "unknown"}
            if isinstance(scope, RepairScope) and record['outcome'] == 'worker_error' and owned_observation_id is None:
                # Unexpected handler/database errors can interrupt commit
                # acknowledgement. A read of the old head does not justify
                # assuming that an in-flight server transaction never committed.
                record['publication'] = {'status': 'unknown'}
            if record["outcome"] == "succeeded" and record["publication"]["status"] != "verified":
                record["outcome"] = "worker_error"
    except AdmissionRejected as exc:
        record.update(outcome="blocked", problem=exc.args[0])
    except CollectionYield as exc:
        record["outcome"] = "cancelled" if exc.reason == "stopped" else "budget"
    except Exception:
        record.update(outcome="cancelled" if cancelled.is_set() else "worker_error", problem="worker_boundary_error")
        if entered and record["publication"]["status"] not in ("verified", "none"):
            record["publication"] = {"status": "unknown"}
    finally:
        record.update(phase="terminal", unknown_publication=record["publication"]["status"] == "unknown")
        record["message"] = _message(scope, record)
        _write(output / "worker.json", record)
        execution_deadline.reset(token)
        default_repair.reset(repair_token)
        structlog.configure(**old_logging)
        signal.signal(signal.SIGTERM, old_signal)
        if engine is not None:
            engine.dispose()
    return 0 if record["outcome"] == "succeeded" and not record["unknown_publication"] else 2


def _signal_owned(process, *, kill=False) -> bool:
    """An unreaped Popen child cannot have its PID reused underneath us."""
    if process.poll() is not None:
        return False
    try:
        if process.pid <= 0 or process.pid == os.getpid() or os.getpgid(process.pid) != process.pid:
            return False
        os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


def _read_worker(path: Path, scope: Scope, operation_id: str):
    """Revalidate child IPC instead of passing through arbitrary nested JSON."""
    from types import SimpleNamespace
    from app.data_ingestion.clients.tonghuashun import BUSINESS_ERRORS, ERRORS
    if not path.is_file() or path.is_symlink() or path.stat().st_size > RESULT_LIMIT:
        return None
    child = json.loads(path.read_text())
    if not isinstance(child, dict) or child.get("protocol") != "ths-bounded@1" or child.get("operation_id") != operation_id or child.get("scope") != scope.as_dict():
        return None
    safe = {"phase": child.get("phase"), "outcome": child.get("outcome")}
    publication = child.get("publication", {})
    if not isinstance(publication, dict) or publication.get("status") not in ("unknown", "not_attempted", "none", "verified"):
        return None
    safe["publication"] = {"status": publication["status"]}
    for key in ("state_revision", "row_count"):
        if key in publication:
            if type(publication[key]) is not int or publication[key] < 0:
                return None
            safe["publication"][key] = publication[key]
    if "state_status" in publication:
        if publication["state_status"] not in ("pending", "failed", "partial", "succeeded", "empty"):
            return None
        safe["publication"]["state_status"] = publication["state_status"]
    if publication["status"] == "verified":
        identifier = publication.get("observation_id")
        digest = publication.get("content_sha256")
        if type(identifier) is not str or str(UUID(identifier)) != identifier or type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None or "row_count" not in publication:
            return None
        safe["publication"].update(observation_id=identifier, content_sha256=digest)
    for name, keys in (("counts", COUNT_KEYS), ("row_counts", ROW_COUNT_KEYS)):
        values = child.get(name, {})
        if not isinstance(values, dict):
            return None
        safe[name] = {key: values[key] if type(values.get(key)) is int and values[key] >= 0 else None for key in keys}
    safe["counts_complete"] = child.get("counts_complete") is True and all(value is not None for value in safe["counts"].values())
    safe["provenance"] = _base(scope, operation_id)["provenance"]
    proof = child.get("provenance", {})
    if not isinstance(proof, dict):
        return None
    if type(proof.get("source_version")) is int and proof["source_version"] >= 0:
        safe["provenance"]["source_version"] = proof["source_version"]
    if proof.get("native_asset_type") == scope.asset_type:
        safe["provenance"]["native_asset_type"] = scope.asset_type
    digest = proof.get("native_identity_sha256")
    if type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest):
        safe["provenance"]["native_identity_sha256"] = digest
    if proof.get("new_explicit_variant") is True:
        safe["provenance"]["new_explicit_variant"] = True
    if isinstance(scope, RepairScope) and proof.get('original_default_variant') is True:
        safe['provenance']['original_default_variant'] = True
    if proof.get("scope_lease") in ("postgresql", "fixture"):
        safe["provenance"]["scope_lease"] = proof["scope_lease"]
    if proof.get("db_application_name") == "ths-bounded:" + operation_id:
        safe["provenance"]["db_application_name"] = proof["db_application_name"]
    generated = _failure_request(scope, proof.get("last_generated_request"))
    if generated is not None:
        safe["provenance"]["last_generated_request"] = generated
    diagnostic = child.get("diagnostic", {})
    safe["diagnostic"] = {}
    if isinstance(diagnostic, dict):
        kind = diagnostic.get("error_kind")
        if kind in set(ERRORS) | {"invalid_data", "partial_reports"}:
            safe["diagnostic"]["error_kind"] = kind
            code = diagnostic.get("supplier_business_code")
            if type(code) is int and BUSINESS_ERRORS.get(code) == kind:
                safe["diagnostic"]["supplier_business_code"] = code
        if diagnostic.get("failure_stage") in ("request", "response_validation", "history_validation"):
            safe["diagnostic"]["failure_stage"] = diagnostic["failure_stage"]
        failure = _failure_request(scope, diagnostic.get("failure_request"))
        if failure is not None:
            safe["diagnostic"]["failure_request"] = failure
        reason = _reason(kind, diagnostic.get("reason"))
        if reason is not None:
            safe["diagnostic"]["reason"] = reason
    safe["response_evidence"] = _response_evidence(SimpleNamespace(response_evidence=child.get("response_evidence")))
    if child.get("problem") in PROBLEMS:
        safe["problem"] = child["problem"]
    return safe


def supervise(scope: Scope, output: Path, *, _budget=MAX_SECONDS, _popen=None,
              _deadline=None) -> dict:
    """One attempt; reserve termination/reaping inside the absolute wall limit."""
    if not math.isfinite(_budget) or not 0 < _budget <= MAX_SECONDS:
        raise ValueError("invalid_budget")
    started = time.monotonic()
    deadline = started + _budget
    if _deadline is not None:
        if not math.isfinite(_deadline):
            raise ValueError("invalid_deadline")
        # A containing batch passes one unchanged absolute deadline. Starting
        # the next scope must never reset the batch's wall-clock allowance.
        deadline = min(deadline, _deadline)
    available_budget = max(0.0, deadline - started)
    scope.parameters()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    operation_id = str(uuid4())
    record = _base(scope, operation_id)
    record.update(outcome="worker_error", publication={"status": "unknown"}, unknown_publication=True,
                  counts={key: None for key in COUNT_KEYS}, counts_complete=False,
                  process_exited=False, exit_code=None, termination_sent=False, kill_sent=False)
    record["process_started"] = False
    command = [sys.executable, "-m", MODULE, "--dataset", scope.dataset, "--subject", scope.subject,
               "--start-date", scope.start_date.isoformat(), "--end-date", scope.end_date.isoformat(),
               "--output", str(output.resolve()), "--_worker", "--_operation-id", operation_id,
               "--_deadline", str(deadline)]
    if isinstance(scope, RepairScope):
        command = [sys.executable, '-m', MODULE, '--_repair-json', json.dumps(scope.selection.as_dict()),
                   '--output', str(output.resolve()), '--_worker', '--_operation-id', operation_id,
                   '--_deadline', str(deadline)]
    process = None
    old_handlers = {}
    interrupted = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, lambda *_: interrupted.set())
    try:
        if time.monotonic() >= deadline:
            record.update(outcome="timed_out", publication={"status": "not_attempted"}, unknown_publication=False)
            record.update(elapsed_seconds=round(time.monotonic() - started, 6), resources_verified=False)
            record["message"] = _message(scope, record)
            _write(output / "result.json", record)
            return record
        process = (_popen or subprocess.Popen)(command, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        record["child_pid"] = process.pid
        record["process_started"] = True
        # A fixed reserve also scales for short, supplier-free fixture tests.
        term_at = deadline - min(10.0, available_budget * .10) - min(5.0, available_budget * .05)
        kill_at = deadline - min(5.0, available_budget * .05)
        while process.poll() is None and time.monotonic() < deadline:
            now = time.monotonic()
            if (interrupted.is_set() or now >= term_at) and not record["termination_sent"]:
                record["termination_sent"] = _signal_owned(process)
            if now >= kill_at and not record["kill_sent"]:
                record["kill_sent"] = _signal_owned(process, kill=True)
            try:
                process.wait(timeout=max(0, min(.05, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        record["exit_code"] = process.poll()
        record["process_exited"] = record["exit_code"] is not None
        path = output / "worker.json"
        if record["process_exited"]:
            child = _read_worker(path, scope, operation_id)
            if child is not None:
                # A terminal result with mismatching exit status is incomplete.
                record.update({key: child[key] for key in (
                    "provenance", "publication", "counts", "counts_complete", "row_counts",
                    "diagnostic", "response_evidence", "problem") if key in child})
                if child.get("phase") == "terminal" and child.get("outcome") in OUTCOMES:
                    record["outcome"] = child["outcome"]
                    record["worker_outcome"] = child["outcome"]
                    if record["outcome"] == "succeeded" and record["exit_code"] != 0:
                        record["outcome"] = "worker_error"
        if record["termination_sent"] or record["kill_sent"] or not record["process_exited"]:
            record["outcome"] = "cancelled" if interrupted.is_set() else "timed_out"
        record["unknown_publication"] = record["publication"].get("status") == "unknown"
    except Exception:
        record["outcome"] = "supervisor_error"
        if process is not None and process.poll() is None:
            record["kill_sent"] = _signal_owned(process, kill=True)
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            record.update(exit_code=process.poll(), process_exited=process.poll() is not None)
        elif process is None:
            record.update(publication={"status": "not_attempted"}, unknown_publication=False)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    record["elapsed_seconds"] = round(time.monotonic() - started, 6)
    record["resources_verified"] = False  # Process exit cannot prove server-side DB cleanup.
    record["message"] = _message(scope, record)
    _write(output / "result.json", record)
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="单次同花顺ETF/指数正式scope诊断（硬上限180秒）")
    parser.add_argument("--dataset", choices=DATASETS)
    parser.add_argument("--subject")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument('--repair-plan', type=Path, help='One exact original-default selection with pinned native evidence')
    parser.add_argument('--_repair-json', help=argparse.SUPPRESS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_operation-id", help=argparse.SUPPRESS)
    parser.add_argument("--_deadline", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.repair_plan is not None or args._repair_json is not None:
            if any(value is not None for value in (args.dataset, args.subject, args.start_date, args.end_date)):
                raise ValueError('mixed_scope_modes')
            if args._repair_json is not None:
                if not args._worker or args.repair_plan is not None or len(args._repair_json.encode()) > 4096:
                    raise ValueError('invalid_worker_protocol')
                value = json.loads(args._repair_json)
            else:
                if args._worker or args.repair_plan.is_symlink() or args.repair_plan.stat().st_size > 4096:
                    raise ValueError('invalid_repair_plan')
                value = json.loads(args.repair_plan.read_text())
            scope = RepairScope.from_dict(value)
        else:
            if any(value is None for value in (args.dataset, args.subject, args.start_date, args.end_date)):
                raise ValueError('incomplete_scope')
            scope = Scope(args.dataset, args.subject, args.start_date, args.end_date)
        if args._worker:
            UUID(args._operation_id)
            if not math.isfinite(args._deadline) or args._deadline > time.monotonic() + MAX_SECONDS:
                raise ValueError("invalid_deadline")
            return run_worker(scope, args.output, args._operation_id, args._deadline)
        if args._operation_id is not None or args._deadline is not None:
            raise ValueError("invalid_worker_protocol")
        record = supervise(scope, args.output)
        print(json.dumps({"operation_id": record["operation_id"], "outcome": record["outcome"],
                          "unknown_publication": record["unknown_publication"],
                          "process_exited": record["process_exited"], "message": record["message"]}, ensure_ascii=False))
        return 0 if record["outcome"] == "succeeded" and not record["unknown_publication"] else 2
    except Exception:
        print('{"outcome":"blocked","problem":"invalid_input_or_output"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
