"""Cooperative collection budgets, durable request units and operator progress.

The journal contains source responses only, never download descriptors or keys.
Publication remains the sole operation that advances a collection head. A yield
leaves the head unchanged and lets another scheduler run reuse completed reads.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import math
import time
from typing import Callable

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.scheduling.models import TaskRun
from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit
from app.data_ingestion.tonghuashun.contracts import exact_json

active_control = ContextVar('tonghuashun_control', default=None)
execution_deadline = ContextVar('tonghuashun_execution_deadline', default=None)


@dataclass(frozen=True)
class ExecutionDeadline:
    """Process-local cancellation for a formally supervised collector attempt.

    This scope is deliberately separate from the logical request budget. A
    successful final allowed request may still publish, while a cancelled or
    expired execution must not begin another request or publication. Ordinary
    scheduler handlers retain their existing cooperative budgets when unset.
    """
    deadline: float
    cancelled: Callable[[], bool]
    counts: dict = field(default_factory=lambda: {'logical_requests': 0, 'http_attempts': 0, 'reused': 0})
    detail: dict = field(default_factory=dict)
    response_evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        if not math.isfinite(self.deadline) or not callable(self.cancelled):
            raise ValueError('A finite deadline and cancellation callback are required')


def check_execution():
    """Check only cancellation/time, never consume or recheck a request count."""
    limit = execution_deadline.get()
    if limit is not None:
        if limit.cancelled():
            raise CollectionYield('stopped')
        if time.monotonic() >= limit.deadline:
            raise CollectionYield('budget')


def note_execution(name):
    """Count actual formal-path events, never infer attempts from a maximum."""
    limit = execution_deadline.get()
    if limit is not None:
        if name not in limit.counts:
            raise ValueError('Unknown bounded collector counter')
        limit.counts[name] += 1


def note_response(data, parameters=None):
    """Keep a bounded true-return digest, shape and safe numeric projection.

    Reflected strings, credentials and arbitrary response fields cannot be
    copied into diagnostic artifacts. Existing validation and source publication still own the real
    payload. A received response digest is not a successful source receipt.
    """
    limit = execution_deadline.get()
    if limit is None:
        return
    encoded = exact_json(data).encode()
    records = data.get('item') if isinstance(data, dict) else None
    limit.response_evidence.update(sha256=hashlib.sha256(encoded).hexdigest(),
        response_bytes=len(encoded), row_count=len(records) if isinstance(records, list) else None,
        shape='object_with_item_list' if isinstance(records, list) else 'other')
    # At most five original numeric bar projections help diagnose a true bad
    # return while refusing reflected strings, metadata, URLs or credentials.
    # Explicit type/text pairs retain Decimal precision and invalid negatives;
    # this evidence is never installed as a source or confirmation receipt.
    fields = ('date_ms', 'open_price', 'high_price', 'low_price', 'close_price', 'volume', 'turnover')
    samples = []
    for row in (records[:5] if isinstance(records, list) else []):
        if not isinstance(row, dict):
            samples.append({'shape': 'non_object'})
            continue
        sample = {'shape': 'object', 'numeric_fields': {}}
        for name in fields:
            value = row.get(name)
            typename = ('null' if value is None else 'bool' if type(value) is bool else
                        'int' if type(value) is int else 'decimal' if isinstance(value, Decimal) else 'other')
            item = {'type': typename}
            if typename in ('int', 'decimal') and len(str(value)) <= 64 and (
                    not isinstance(value, Decimal) or value.is_finite()):
                item['value_text'] = str(value)
            else:
                item['value_omitted'] = True
            sample['numeric_fields'][name] = item
        if 'thscode' in row:
            sample['subject_matches_request'] = row['thscode'] == (parameters or {}).get('thscode')
        samples.append(sample)
    limit.response_evidence.update(numeric_samples=samples,
        sample_limit=5, full_payload_exported=False)


def wait_for_pacing(seconds):
    """Make the existing at-most-60-second provider cooldown cancellable."""
    if not 0 <= seconds <= 60:
        raise ValueError('Provider pacing must remain between zero and 60 seconds')
    if execution_deadline.get() is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while True:
        check_execution()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.1, remaining))


class CollectionYield(Exception):
    """Only raised at a boundary with no publication transaction in flight."""
    def __init__(self, reason):
        self.reason = reason


class CollectionControl:
    def __init__(self, engine, run_id, *, max_requests=40, max_seconds=180):
        self.engine, self.run_id = engine, run_id
        self.max_requests, self.deadline = max_requests, time.monotonic() + max_seconds
        self.scope = None
        self.cache = False
        self.initial_pending = None
        self.detail = dict(stage='准备采集', batch_total=None, processed=0, succeeded=0,
            failed=0, skipped=0, fetched_rows=0, received=0, changed=0, requests=0, reused=0,
            subject=None, last_advanced_at=None, coverage_total=None, coverage_pending=None)

    def emit(self, **values):
        if 'coverage_pending' in values and self.initial_pending is None:
            self.initial_pending = values['coverage_pending']
        self.detail.update(values)
        limit = execution_deadline.get()
        if limit is not None:
            limit.detail.update(self.detail)
        if self.run_id is None:
            return
        total = self.detail['batch_total']
        fraction = min(.99, self.detail['processed']/total) if total else 0
        with Session(self.engine) as session:
            session.execute(update(TaskRun).where(TaskRun.id == self.run_id,
                TaskRun.status == 'running').values(collection_progress=dict(self.detail),
                progress=fraction, current_step=self.detail['stage']))
            session.commit()

    def check(self):
        check_execution()
        if self.run_id is not None:
            with Session(self.engine) as session:
                run = session.get(TaskRun, self.run_id)
                if run is None or run.status != 'running' or run.cancellation_requested_at:
                    raise CollectionYield('stopped')
        if self.detail['requests'] >= self.max_requests or time.monotonic() >= self.deadline:
            raise CollectionYield('budget')

    def begin(self, dataset, subject, variant, revision, parameters, *, cache=False):
        # Operational batch settings do not alter the identity of source work.
        scope_params = parameters.model_dump(mode='json')
        for key in ('batch_size', 'max_requests', 'max_seconds'):
            scope_params.pop(key, None)
        self.scope = hashlib.sha256(exact_json([dataset, subject, variant, revision, scope_params]).encode()).hexdigest()
        self.cache = cache
        self.emit(subject=subject, stage='采集数据', unit_saved=0, reports_total=None, reports_saved=None, report_period=None, current_request=None)

    def read(self, interface, parameters, request):
        check_execution()
        key = hashlib.sha256(exact_json([interface, parameters]).encode()).hexdigest()
        # Cancellation is checked even during replay, which can itself be large.
        if self.run_id is not None:
            with Session(self.engine) as session:
                run = session.get(TaskRun, self.run_id)
                if run is None or run.status != 'running' or run.cancellation_requested_at:
                    raise CollectionYield('stopped')
        if self.cache and self.scope:
            with Session(self.engine) as session:
                row = session.get(TonghuashunWorkUnit, (self.scope, key))
                if row:
                    note_execution('reused')
                    self.emit(stage='恢复已保存断点', reused=self.detail['reused']+1,
                              unit_saved=self.detail.get('unit_saved', 0)+1)
                    return json.loads(row.data_json, parse_float=Decimal), 'checkpoint'
        self.check()
        note_execution('logical_requests')
        self.emit(stage='等待接口限速或响应', current_request=parameters,
                  requests=self.detail['requests']+1)
        response = request()
        note_response(response.data, parameters)
        # An in-flight response may outlive a cancellation request. Do not
        # journal or publish it after the independent execution has expired.
        check_execution()
        records = response.data.get('item', response.data.get('abilities', []))
        self.emit(fetched_rows=self.detail['fetched_rows']+(len(records) if isinstance(records, list) else 0))
        if self.cache and self.scope:
            insert = pg_insert if self.engine.dialect.name == 'postgresql' else sqlite_insert
            with Session(self.engine) as session:
                session.execute(insert(TonghuashunWorkUnit).values(scope=self.scope,
                    request_key=key, data_json=exact_json(response.data), created_at=datetime.now(UTC))
                    .on_conflict_do_nothing())
                session.commit()
            self.emit(stage='已保存采集断点', unit_saved=self.detail.get('unit_saved',0)+1,
                      last_advanced_at=datetime.now(UTC).isoformat())
        return response.data, response.request_id

    def published(self, session):
        check_execution()
        # Remove staged responses atomically with the successful source version.
        if self.scope:
            session.execute(delete(TonghuashunWorkUnit).where(TonghuashunWorkUnit.scope == self.scope))

    def discard(self):
        # Invalid data must be refetched rather than replayed forever.
        if self.scope:
            with Session(self.engine) as session:
                self.published(session)
                session.commit()

    def completed(self, summary, *, advanced=True):
        self.emit(stage='已发布采集结果' if advanced else '对象采集失败', processed=summary['succeeded']+summary['failed'],
            coverage_pending=max(0, self.initial_pending-summary['succeeded']) if self.initial_pending is not None else None,
            succeeded=summary['succeeded'], failed=summary['failed'], skipped=summary['skipped'],
            received=summary['received'], changed=summary['changed'],
            last_advanced_at=datetime.now(UTC).isoformat() if advanced else self.detail['last_advanced_at'])


def control():
    return active_control.get()
