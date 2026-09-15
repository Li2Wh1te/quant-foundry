"""Cooperative collection budgets, durable request units and operator progress.

The journal contains source responses only, never download descriptors or keys.
Publication remains the sole operation that advances a collection head. A yield
leaves the head unchanged and lets another scheduler run reuse completed reads.
"""
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import time

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.scheduling.models import TaskRun
from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit
from app.data_ingestion.tonghuashun.contracts import exact_json

active_control = ContextVar('tonghuashun_control', default=None)


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
                    self.emit(stage='恢复已保存断点', reused=self.detail['reused']+1,
                              unit_saved=self.detail.get('unit_saved', 0)+1)
                    return json.loads(row.data_json, parse_float=Decimal), 'checkpoint'
        self.check()
        self.emit(stage='等待接口限速或响应', current_request=parameters,
                  requests=self.detail['requests']+1)
        response = request()
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
