"""Cooperative collection budgets, durable request units and operator progress.

The journal contains source responses only, never download descriptors or keys.
Publication remains the sole operation that advances a collection head. A yield
leaves the head unchanged and lets another scheduler run reuse completed reads.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
import hashlib
import json
import math
import re
import threading
import time
from typing import Callable
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.scheduling.models import TaskRun
from app.data_ingestion.models.tonghuashun import TonghuashunWorkUnit
from app.data_ingestion.tonghuashun.contracts import exact_json

active_control = ContextVar('tonghuashun_control', default=None)
execution_deadline = ContextVar('tonghuashun_execution_deadline', default=None)
default_repair = ContextVar('tonghuashun_default_repair', default=None)


@dataclass(frozen=True)
class DefaultRepair:
    """An exact original-default selection, never an arbitrary variant override.

    A plan pins the existing head and a typed target. Financial dates additionally
    require an immutable original containing that exact pair: a caller's dates
    alone cannot manufacture a provider period. This process-local instruction
    uses the existing observations and work units, not a repair ledger.
    """
    dataset: str
    subject: str
    asset_type: str
    revision: int
    observation_id: UUID
    content_sha256: str
    day: date | None = None
    end_date: date | None = None
    report_type: str | None = None
    start_date: date | None = None
    selector_observation_id: UUID | None = None
    selector_content_sha256: str | None = None

    @property
    def request_limit(self):
        return 2 if self.dataset in ('fund_stock_history', 'fund_bond_history') else 1

    def allowed_requests(self):
        """Locally generated parameters bound every transport call in this mode."""
        from app.data_ingestion.tonghuashun.contracts import DATASETS, date_ms
        self.validate()
        interface = DATASETS[self.dataset].interface
        if self.day is not None:
            return [(interface, {'thscode': self.subject, 'interval': '1d', 'start': date_ms(self.day),
                                 'end': date_ms(self.day), 'adjust': 'none'})]
        if self.report_type is not None:
            return [(interface.replace('-history', '-report-dates'), {'thscode': self.subject}),
                    (interface, {'thscode': self.subject, 'end_date': self.end_date.isoformat(),
                                 'report_type': self.report_type})]
        return [(interface, {'thscode': self.subject})]

    def validate(self):
        from datetime import date
        from app.data_ingestion.tonghuashun.contracts import CODE, DATASETS, SHANGHAI
        allowed = ('stock_daily', 'fund_stock_history', 'fund_bond_history',
                   'fund_financial_indicators', 'fund_income', 'fund_balance')
        if (self.dataset not in allowed or type(self.subject) is not str or CODE.fullmatch(self.subject) is None
                or self.asset_type not in DATASETS[self.dataset].assets
                or type(self.revision) is not int or self.revision < 1
                or not isinstance(self.observation_id, UUID)
                or type(self.content_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}', self.content_sha256) is None):
            raise ValueError('invalid_repair_baseline')
        today = datetime.now(SHANGHAI).date()
        def valid_day(value):
            return type(value) is date and date(1900, 1, 1) <= value < today
        if self.dataset == 'stock_daily':
            valid = valid_day(self.day) and all(value is None for value in (
                self.start_date, self.end_date, self.report_type, self.selector_observation_id,
                self.selector_content_sha256))
        elif self.dataset in ('fund_stock_history', 'fund_bond_history'):
            valid = valid_day(self.end_date) and self.report_type in ('quarter', 'semiannual', 'annual') and all(
                value is None for value in (self.day, self.start_date, self.selector_observation_id,
                                           self.selector_content_sha256))
        else:
            valid = (valid_day(self.start_date) and valid_day(self.end_date) and self.start_date <= self.end_date
                     and self.day is None and self.report_type is None
                     and isinstance(self.selector_observation_id, UUID)
                     and type(self.selector_content_sha256) is str
                     and re.fullmatch(r'[0-9a-f]{64}', self.selector_content_sha256) is not None)
        if not valid:
            raise ValueError('invalid_or_unknown_repair_selector')
        return self

    def as_dict(self):
        self.validate()
        if self.day is not None:
            selector = {'day': self.day.isoformat()}
        elif self.report_type is not None:
            selector = {'end_date': self.end_date.isoformat(), 'report_type': self.report_type}
        else:
            selector = {'start_date': self.start_date.isoformat(), 'end_date': self.end_date.isoformat(),
                        'observation_id': str(self.selector_observation_id),
                        'content_sha256': self.selector_content_sha256}
        return {'dataset': self.dataset, 'subject': self.subject, 'asset_type': self.asset_type,
                'baseline': {'revision': self.revision, 'observation_id': str(self.observation_id),
                             'content_sha256': self.content_sha256}, 'selector': selector}

    @classmethod
    def from_dict(cls, value):
        from datetime import date
        if not isinstance(value, dict) or set(value) != {'dataset', 'subject', 'asset_type', 'baseline', 'selector'}:
            raise ValueError('invalid_repair_plan')
        baseline, selector = value['baseline'], value['selector']
        if (not isinstance(baseline, dict) or set(baseline) != {'revision', 'observation_id', 'content_sha256'}
                or not isinstance(selector, dict)):
            raise ValueError('invalid_repair_plan')
        kwargs = {}
        if set(selector) == {'day'}:
            kwargs['day'] = date.fromisoformat(selector['day'])
        elif set(selector) == {'end_date', 'report_type'}:
            kwargs.update(end_date=date.fromisoformat(selector['end_date']), report_type=selector['report_type'])
        elif set(selector) == {'start_date', 'end_date', 'observation_id', 'content_sha256'}:
            kwargs.update(start_date=date.fromisoformat(selector['start_date']),
                          end_date=date.fromisoformat(selector['end_date']),
                          selector_observation_id=UUID(selector['observation_id']),
                          selector_content_sha256=selector['content_sha256'])
        else:
            raise ValueError('invalid_or_unknown_repair_selector')
        return cls(value['dataset'], value['subject'], value['asset_type'], baseline['revision'],
                   UUID(baseline['observation_id']), baseline['content_sha256'], **kwargs).validate()

    def baseline(self, session, *, with_keys=False):
        """Verify pinned native inputs before any HTTP and without locking them.

        Publication still uses the ordinary head CAS. A later competing writer
        cannot be defeated by rebasing this plan or by retrying its source read.
        """
        from app.data_ingestion.models.tonghuashun import TonghuashunCollectionState, TonghuashunObservation
        from app.data_ingestion.tonghuashun.contracts import CollectionConflict, CollectionError, provider_date
        from app.data_ingestion.tonghuashun.repository import materialize
        from app.data_store.adapters.record_adapters import source_date
        self.validate()
        state = session.get(TonghuashunCollectionState, (self.dataset, self.subject, 'default'), populate_existing=True)
        if state is None or state.revision != self.revision or state.observation_id != self.observation_id:
            raise CollectionConflict('修复计划的原默认版本已经变化，本次未重新选择来源。')
        observation = session.get(TonghuashunObservation, self.observation_id)
        def same_source(row, digest):
            return row is not None and (row.dataset, row.subject, row.variant) == (
                self.dataset, self.subject, 'default') and row.content_hash == digest
        if not same_source(observation, self.content_sha256):
            raise CollectionError('修复计划的原默认版本凭据不一致。')
        data = materialize(session, observation)
        native_keys = ()
        if self.day is not None:
            # Query timestamps follow the existing Shanghai request contract.
            # Provider identity comes from the pinned raw body instead. A UTC
            # midnight key could share the business day but remains unsupported
            # by the existing canonical source-date contract. Reject that key
            # and ambiguous duplicates before any source request.
            matches = [row['date_ms'] for row in data.get('item', [])
                       if isinstance(row, dict) and type(row.get('date_ms')) is int
                       and provider_date(row['date_ms']) == self.day]
            if len(matches) != 1:
                raise CollectionError('股票修复缺少唯一可核对的原始日期键。')
            try:
                source_date(matches[0])
            except (ValueError, OverflowError, OSError):
                raise CollectionError('股票修复缺少唯一可核对的原始日期键。') from None
            native_keys = (matches[0],)
        if self.selector_observation_id is not None:
            original = session.get(TonghuashunObservation, self.selector_observation_id)
            if not same_source(original, self.selector_content_sha256):
                raise CollectionError('财报修复缺少可核对的原始期间。')
            original_data = materialize(session, original)
            matches = [(row['start_date_ms'], row['end_date_ms']) for row in original_data.get('item', [])
                       if isinstance(row, dict) and type(row.get('start_date_ms')) is int
                       and type(row.get('end_date_ms')) is int
                       and (provider_date(row['start_date_ms']), provider_date(row['end_date_ms'])) == (
                           self.start_date, self.end_date)]
            if len(matches) != 1 or matches[0][0] > matches[0][1]:
                raise CollectionError('财报修复缺少可核对的原始期间。')
            try:
                for key in matches[0]:
                    source_date(key)
            except (ValueError, OverflowError, OSError):
                raise CollectionError('财报修复缺少可核对的原始期间。') from None
            # This pair is resolved from the specified immutable original, not
            # reconstructed by date_ms or accepted from caller-supplied raw IDs.
            native_keys = matches[0]
        return (data, native_keys) if with_keys else data


def collection_scope_hash(dataset, subject, variant, revision, parameters):
    """Share journal identity between admission and the registered handler."""
    scope_params = parameters.model_dump(mode='json')
    for key in ('batch_size', 'max_requests', 'max_seconds'):
        scope_params.pop(key, None)
    identity = [dataset, subject, variant, revision, scope_params]
    repair = default_repair.get()
    if repair is not None:
        identity.append(repair.as_dict())
    return hashlib.sha256(exact_json(identity).encode()).hexdigest()


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
    max_http_attempts: int | None = None
    counter_lock: object = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self):
        if not math.isfinite(self.deadline) or not callable(self.cancelled):
            raise ValueError('A finite deadline and cancellation callback are required')
        if self.max_http_attempts is not None and (type(self.max_http_attempts) is not int or not 0 <= self.max_http_attempts <= 8):
            raise ValueError('The actual HTTP allowance must be between zero and eight')


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
        with limit.counter_lock:
            if name == 'http_attempts':
                check_execution()
                if limit.max_http_attempts is not None and limit.counts[name] >= limit.max_http_attempts:
                    # This boundary runs immediately before requests.get. The
                    # exhausted attempt is neither counted nor sent, including
                    # transport retries and a mistakenly expanded acquisition.
                    raise CollectionYield('budget')
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
        self.scope = collection_scope_hash(dataset, subject, variant, revision, parameters)
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
        # A repair requires a fresh provider confirmation. Existing work units
        # still journal successful responses below, but cannot silently replay
        # an older catalogue or body as a new confirmation in this mode.
        if self.cache and self.scope and default_repair.get() is None:
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
                  current_interface=interface, request_stage='request',
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
