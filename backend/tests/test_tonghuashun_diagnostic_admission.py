"""Selected diagnostic scopes bound execution as well as body retention."""
from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.scheduling.registry import TaskContext
from app.data_ingestion.clients import tonghuashun as transport
from app.data_ingestion.scheduler_tasks import tonghuashun as task
from app.data_ingestion.tonghuashun import control, diagnostics as d
from app.data_ingestion.tonghuashun.contracts import CollectionParameters

PLAN = '11111111-2222-4333-8444-555555555555'
A = '22222222-3333-4444-8555-666666666666'
B = '33333333-4444-4555-8666-777777777777'
PARAMS = CollectionParameters(subjects=['SYNTHETIC.OF'], batch_size=1,
                              max_requests=1, max_seconds=10).model_dump(mode='json')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ('QF_THS_DIAGNOSTIC_RUN_IDS','QF_THS_DIAGNOSTIC_SCOPE_KEYS','QF_THS_DIAGNOSTIC_UNTIL'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(d, '_scope_claims', {})
    monkeypatch.setattr(transport, '_gate', transport._RequestGate())


def arm(monkeypatch, minutes=5):
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_SCOPE_KEYS', d.scope_key('fund_income', PLAN, PARAMS))
    monkeypatch.setenv('QF_THS_DIAGNOSTIC_UNTIL',
                       (datetime.now(UTC)+timedelta(minutes=minutes)).isoformat())


class Response:
    status_code = 200
    headers = {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def iter_content(self, size): yield b'{"code":0,"data":{"item":[]}}'


def client(monkeypatch):
    get=Mock(return_value=Response())
    monkeypatch.setattr(transport.requests, 'get', get)
    return transport.TonghuashunClient('https://example.test','fixture-key',
                                      interval_ms=0,max_attempts=1),get


def send(c):
    return c.request('fund.financials.income-statements', {'thscode':'SYNTHETIC.OF'})


def test_expired_selected_run_sends_zero_http(monkeypatch):
    arm(monkeypatch, -1);c,get=client(monkeypatch)
    token=d.begin_run('fund_income',A,PLAN,PARAMS)
    try:
        with pytest.raises(control.CollectionYield, match='stopped'):send(c)
    finally:d.end_run(token)
    get.assert_not_called()


def test_second_run_for_consumed_scope_sends_zero_extra_http(monkeypatch):
    arm(monkeypatch);c,get=client(monkeypatch)
    token=d.begin_run('fund_income',A,PLAN,PARAMS)
    try:send(c)
    finally:d.end_run(token)
    token=d.begin_run('fund_income',B,PLAN,PARAMS)
    try:
        with pytest.raises(control.CollectionYield, match='stopped'):send(c)
    finally:d.end_run(token)
    assert get.call_count==1


def test_expiry_between_requests_stops_next_http(monkeypatch):
    arm(monkeypatch);c,get=client(monkeypatch)
    token=d.begin_run('fund_income',A,PLAN,PARAMS)
    try:
        send(c)
        monkeypatch.setenv('QF_THS_DIAGNOSTIC_UNTIL',
                           (datetime.now(UTC)-timedelta(seconds=1)).isoformat())
        with pytest.raises(control.CollectionYield, match='stopped'):send(c)
    finally:d.end_run(token)
    assert get.call_count==1


def test_unselected_ordinary_work_keeps_its_existing_request(monkeypatch):
    arm(monkeypatch, -1);c,get=client(monkeypatch)
    token=d.begin_run('fund_income',A,B,PARAMS)
    try:send(c)
    finally:d.end_run(token)
    assert get.call_count==1


def test_expired_handler_yields_before_credentials_or_collection(monkeypatch):
    arm(monkeypatch, -1)
    monitor=Mock(detail={'succeeded':0,'failed':0,'fetched_rows':0,'changed':0})
    monkeypatch.setattr(control,'CollectionControl',lambda *a,**kw:monitor)
    monkeypatch.setattr(task,'get_engine',lambda:None)
    credentials=Mock();collect=Mock()
    monkeypatch.setattr(task.TonghuashunClient,'from_settings',credentials)
    monkeypatch.setattr(task,'collect',collect)
    result=task.execute('fund_income',TaskContext(task_id=PLAN,run_id=A,
                        task_type='data.ths.fund_income'),CollectionParameters(**PARAMS))
    assert result['yield_reason']=='stopped'
    credentials.assert_not_called();collect.assert_not_called()
    assert control.active_control.get() is None


@pytest.fixture
def overlay():
    path=Path(__file__).resolve().parents[2]/'scripts/r01_diagnostic_overlay.py'
    spec=importlib.util.spec_from_file_location('diagnostic_overlay',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('existing', [True,False])
def test_local_base_is_verified_and_never_uses_digest_as_repository(monkeypatch,overlay,existing):
    image='sha256:'+'1'*64;tag='qf-r01-diagnostic-base:'+'1'*64
    monkeypatch.setattr(overlay.subprocess,'check_output',lambda *a,**kw:image+'\n')
    response=SimpleNamespace(returncode=0 if existing else 1,
                             stdout=image+'\n' if existing else '',stderr='' if existing else 'No such image')
    call=Mock(return_value=response)
    monkeypatch.setattr(overlay.subprocess,'run',call)
    assert overlay.ensure_local_base(image)==tag
    assert call.call_count==(1 if existing else 2)
    if not existing:assert call.call_args.args[0]==['docker','image','tag',image,tag]


def test_unrelated_local_base_tag_is_never_overwritten(monkeypatch,overlay):
    image='sha256:'+'1'*64
    monkeypatch.setattr(overlay.subprocess,'check_output',lambda *a,**kw:image+'\n')
    call=Mock(return_value=SimpleNamespace(returncode=0,stdout='sha256:'+'2'*64,stderr=''))
    monkeypatch.setattr(overlay.subprocess,'run',call)
    with pytest.raises(ValueError,match='unrelated'):overlay.ensure_local_base(image)
    assert call.call_count==1


def test_unavailable_base_tag_inspection_does_not_tag_blindly(monkeypatch,overlay):
    image='sha256:'+'1'*64
    monkeypatch.setattr(overlay.subprocess,'check_output',lambda *a,**kw:image+'\n')
    call=Mock(return_value=SimpleNamespace(returncode=1,stdout='',stderr='daemon unavailable'))
    monkeypatch.setattr(overlay.subprocess,'run',call)
    with pytest.raises(ValueError,match='verify'):overlay.ensure_local_base(image)
    assert call.call_count==1
