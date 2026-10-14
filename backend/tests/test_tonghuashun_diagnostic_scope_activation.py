"""Capture arming before a public submission assigns its new run UUID."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier
from unittest.mock import Mock

import pytest

from app.scheduling.registry import TaskContext
from app.data_ingestion.scheduler_tasks import tonghuashun as task
from app.data_ingestion.tonghuashun import diagnostics as d
from app.data_ingestion.tonghuashun.contracts import CollectionParameters

PLAN = '11111111-2222-4333-8444-555555555555'
RUN_A = '22222222-3333-4444-8555-666666666666'
RUN_B = '33333333-4444-4555-8666-777777777777'
PARAMS = CollectionParameters(subjects=['SYNTHETIC.OF'], batch_size=1,
                              max_requests=1, max_seconds=10).model_dump(mode='json')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ('QF_THS_DIAGNOSTIC_RUN_IDS', 'QF_THS_DIAGNOSTIC_SCOPE_KEYS',
                 'QF_THS_DIAGNOSTIC_UNTIL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(d, '_scope_claims', {})


def arm(monkeypatch, *keys, minutes=5):
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_SCOPE_KEYS', ','.join(keys))
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_UNTIL',
                       (datetime.now(UTC)+timedelta(minutes=minutes)).isoformat())


def captured(run_id=RUN_A, plan=PLAN, params=PARAMS, dataset='fund_income'):
    token = d.begin_run(dataset, run_id, plan, params)
    try:
        return d.wants_body(d.begin_request())
    finally:
        d.end_run(token)


def test_public_handler_new_uuid_captures_the_first_response(monkeypatch):
    arm(monkeypatch, d.scope_key('fund_income', PLAN, PARAMS))
    events = []
    monkeypatch.setattr(d, '_emit', lambda event, fields, warning=False: events.append(fields))
    monkeypatch.setattr(task, 'get_engine', lambda: None)
    monkeypatch.setattr(task.TonghuashunClient, 'from_settings', lambda settings: None)
    monkeypatch.setattr(task, 'get_settings', lambda: None)
    from app.data_ingestion.tonghuashun import control
    monkeypatch.setattr(control, 'CollectionControl', lambda *a, **kw: Mock())
    raw = b'{ "code":0, "data":{"item":[],"symbol":"SYNTHETIC.OF"} }'

    def collect(*args):
        attempt = d.begin_request()
        assert d.wants_body(attempt)
        d.finish_request(attempt, {'thscode':'SYNTHETIC.OF'}, 'fixture-api-key',
                         200, raw, True, json.loads(raw), True, None)
        return {'succeeded':0}

    monkeypatch.setattr(task, 'collect', collect)
    task.execute('fund_income', TaskContext(task_id=PLAN, run_id=RUN_A,
                 task_type='data.ths.fund_income'), CollectionParameters(**PARAMS))
    assert len(events) == 1 and events[0]['run_id'] == RUN_A
    assert events[0]['raw_response'] == raw.decode()
    assert not captured(RUN_B)


@pytest.mark.parametrize('change', ['subject', 'budget', 'date', 'mode', 'plan', 'dataset'])
def test_changed_queued_or_other_scope_cannot_capture(monkeypatch, change):
    arm(monkeypatch, d.scope_key('fund_income', PLAN, PARAMS))
    params, plan, dataset = dict(PARAMS), PLAN, 'fund_income'
    if change == 'subject': params['subjects'] = ['OTHER.OF']
    if change == 'budget': params['max_requests'] = 40
    if change == 'date': params['end_date'] = '2020-01-01'
    if change == 'mode': params['mode'] = 'reconcile'
    if change == 'plan': plan = RUN_B
    if change == 'dataset': dataset = 'fund_performance_history'
    assert not captured(plan=plan, params=params, dataset=dataset)
    assert d._scope_claims == {}


def test_two_scopes_allow_one_run_each_and_no_second_run(monkeypatch):
    second = d.scope_key('fund_performance_history', RUN_B, PARAMS)
    arm(monkeypatch, d.scope_key('fund_income', PLAN, PARAMS), second)
    assert captured()
    assert captured(run_id=RUN_B, plan=RUN_B, dataset='fund_performance_history')
    assert not captured(run_id=RUN_B)
    assert len(d._scope_claims) == 2


def test_concurrent_runs_cannot_both_claim_one_scope(monkeypatch):
    arm(monkeypatch, d.scope_key('fund_income', PLAN, PARAMS))
    barrier = Barrier(2)

    def begin(identifier):
        barrier.wait()
        return captured(identifier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(begin, [RUN_A, RUN_B])) == [False, True]


@pytest.mark.parametrize('bad', ['expired', 'too_long', 'malformed', 'three', 'mixed'])
def test_activation_fails_closed(monkeypatch, bad):
    key = d.scope_key('fund_income', PLAN, PARAMS)
    arm(monkeypatch, key, minutes=-1 if bad == 'expired' else 31 if bad == 'too_long' else 5)
    if bad == 'malformed': monkeypatch.setenv('QF_THS_DIAGNOSTIC_SCOPE_KEYS', 'invalid')
    if bad == 'three': monkeypatch.setenv('QF_THS_DIAGNOSTIC_SCOPE_KEYS', ','.join([key]*3))
    if bad == 'mixed': monkeypatch.setenv('QF_THS_DIAGNOSTIC_RUN_IDS', RUN_A)
    assert not captured()


def test_uuid_activation_still_accepts_handler_scope_arguments(monkeypatch):
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_RUN_IDS', RUN_A)
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_UNTIL',
                       (datetime.now(UTC)+timedelta(minutes=5)).isoformat())
    assert captured()
    assert d._scope_claims == {}
