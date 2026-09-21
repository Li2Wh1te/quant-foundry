"""M3 real-shaped local bars in disposable PostgreSQL, no provider calls."""
from datetime import date, datetime, timezone, timedelta
import json
from uuid import uuid4, UUID
from unittest.mock import patch
import pytest
from sqlalchemy import select
from app.data_foundation.canonical import FoundationError
from app.data_foundation.catalog import register_dependencies
from app.data_foundation.contracts import register_initial_catalog
from app.data_foundation.identity import register_binding
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.work import create_work,claim
from app.data_foundation.bars import normalize_batch
from app.data_foundation.tushare import DOMAIN_KEY,SERIES,domain_hash
from app.data_foundation.governance import create_governance
from app.data_foundation.publication import stage_decisions,publish
from app.data_foundation.query import DataRequirement,query_official,lineage
from app.data_foundation.work_models import CandidateManifest,Work,OfficialBar,Decision
from app.instruments.models import Instrument
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_sources import execution


def setup_daily(session, *, missing=False, duplicate=False, calendar_missing=False, bad_amount=False, middle_gap=False):
    key=uuid4().hex
    iid=uuid4();session.add(Instrument(id=iid,asset_class='etf'));session.flush()
    ex=execution(session)
    contract,_,policy,_=register_initial_catalog(session)
    def baseline(dataset,rows):
        return register_baseline(session,source='tushare',dataset=dataset,scope={},rows=rows,
            observed_at=datetime.now(timezone.utc),decoder_id=ex.id,event_key=key)
    directory=baseline('etf_directory',[{'source':'tushare','ts_code':key,'etf_id':str(iid),'exchange':'SH','list_date':'2020-01-01','list_status':'L'}])
    binding=register_binding(session,source_ref_id=directory.id,subject=key,instrument_id=iid,
        valid_from=date(2026,6,5),valid_to=date(2026,6,10) if middle_gap else date(2026,6,9),binding_version=key,status='resolved',
        evidence={'reviewer':'isolated test','reference':key,'valid_from':'2026-06-05','valid_to':'2026-06-10' if middle_gap else '2026-06-09'})
    calendar=baseline('exchange_calendar',[{'calendar_date':str(date(2026,6,5)+timedelta(days=i)),'exchange':'SSE','is_open':i in [0,3,4]} for i in range(5 if middle_gap else 4 if not calendar_missing else 3)])
    dep=register_dependencies(session,[{'binding_id':binding.id,'purpose':'identity'},{'source_ref_id':directory.id,'purpose':'directory'},{'source_ref_id':calendar.id,'purpose':'calendar'},{'execution_id':ex.id,'purpose':'execution'}])
    rows=[{'source':'tushare','ts_code':key,'trade_date':d,'open':'4.083','high':'4.122','low':'3.947','close':'3.977',
        'vol':'100','amount':'bad' if bad_amount else '5925661.225'} for d in (['2026-06-05','2026-06-09'] if middle_gap else ['2026-06-05'] if missing else ['2026-06-05','2026-06-08'])]
    if duplicate:rows.append(dict(rows[0]))
    ref=baseline('etf_daily',rows)
    params=dict(dataset='market.bar.daily',major=1,profile='default',series=SERIES,start='2026-06-05',end='2026-06-09' if middle_gap else '2026-06-08',domain=DOMAIN_KEY,domain_hash=domain_hash())
    work=create_work(session,kind='A',contract_id=contract.id,execution_id=ex.id,dependency_id=dep.id,parameters=params,source_ref_id=ref.id)
    while work.status!='succeeded':
        row=claim(session,work_id=work.id);normalize_batch(session,work.id,row.lease_epoch)
    return work,ex,policy,iid


def release_daily(session,fixture):
    a,ex,policy,iid=fixture
    b=create_governance(session,normalization_id=a.id,execution_id=ex.id,policy_id=policy.id)
    while True:
        row=claim(session,work_id=b.id)
        release=stage_decisions(session,b.id,row.lease_epoch)
        if release:break
    publish(session,release.id,b.lease_epoch)
    return release


def query(session,release,iid,**changes):
    request=DataRequirement(**(dict(dataset_id='market.bar.daily',contract_version='1.0',profile_id='default',semantic_series_id=SERIES,
        subjects=[iid],business_range={'from':'2026-06-05','to':'2026-06-08'},release=release.id)|changes))
    return json.loads(query_official(session,request,lambda:'test'))


def test_real_shaped_input_to_typed_release_and_field_invariance(session):
    f=setup_daily(session);release=release_daily(session,f)
    with patch('app.data_foundation.source_refs.read_source',side_effect=AssertionError('ordinary query must not decode source')):
        result=query(session,release,f[3],fields=['close','turnover'])
        assert result['state']=='available' and len(result['items'])==2
        assert result['items'][0]['turnover']=='5925661225'
        price=result['items'][0]['close']
        assert price=='3.977'
        rejected=query(session,release,f[3],fields=['close','volume'])
        assert rejected['state']=='unavailable' and rejected['items']==[]
        assert query(session,release,f[3],fields=['close'])['items'][0]['close']==price
    evidence=lineage(session,UUID(result['items'][0]['official_id']))
    assert evidence['policy']['comparison']=='not_applicable'
    assert evidence['policy']['field_quality']['volume']=='UNIT_UNVERIFIED'
    assert evidence['policy']['excluded_candidates']==[]


def test_missing_session_is_not_min_max_coverage(session):
    f=setup_daily(session,missing=True);release=release_daily(session,f)
    result=query(session,release,f[3],fields=['close'])
    assert result['state']=='partial' and result['items']==[]
    partial=query(session,release,f[3],fields=['close'],allow_partial=True)
    assert len(partial['items'])==1 and partial['request_satisfied'] is False
    assert partial['scope_summary']['expected_business_keys']==2


def test_unknown_calendar_stops_automatic_publication(session):
    f=setup_daily(session,calendar_missing=True)
    with pytest.raises(FoundationError,match='日历'):release_daily(session,f)


def test_duplicate_source_keys_block_without_selecting_last_row(session):
    f=setup_daily(session,duplicate=True);release=release_daily(session,f)
    result=query(session,release,f[3],fields=['close'],allow_partial=True)
    assert result['state']=='partial' and len(result['items'])==1
    assert result['excluded'][0]['reason']=='blocked'


def test_optional_bad_amount_preserves_ohlc(session):
    f=setup_daily(session,bad_amount=True);release=release_daily(session,f)
    assert query(session,release,f[3])['state']=='available'
    assert query(session,release,f[3],fields=['turnover'])['state']=='unavailable'


def test_scope_outside_known_calendar_is_unknown_and_never_truncated(session):
    f=setup_daily(session);release=release_daily(session,f)
    result=query(session,release,f[3],business_range={'from':'2026-06-04','to':'2026-06-08'},fields=['close'])
    assert result['state']=='unknown' and result['items']==[]
    assert result['scope_summary']['expected_business_keys'] is None


def test_query_authentication_rechecked_and_read_only(session):
    from sqlalchemy import event
    f=setup_daily(session);release=release_daily(session,f)
    def guard(conn,cursor,statement,*args):
        assert statement.lstrip().split()[0].upper() not in {'INSERT','UPDATE','DELETE'}
    event.listen(session.bind,'before_cursor_execute',guard)
    try:assert query(session,release,f[3])['state']=='available'
    finally:event.remove(session.bind,'before_cursor_execute',guard)


def test_conversion_repair_preserves_prices_and_restricts_bad_history(session):
    """Inject an old converter fault only in this isolated test process."""
    from decimal import Decimal
    from app.data_foundation.catalog import register_execution
    from app.data_foundation.models import Execution
    from app.data_foundation.quality import record_issue
    with patch('app.data_foundation.tushare.optional_amount',return_value=(Decimal('5925661.225'),None)):
        fixture=setup_daily(session)
    old=release_daily(session,fixture)
    before=query(session,old,fixture[3],fields=['close','turnover'])
    oldwork,ex,policy,iid=fixture
    bad_id=UUID(before['items'][0]['official_id'])
    issue=record_issue(session,scope_key=oldwork.scope_key,instrument_id=iid,start=date(2026,6,5),end=date(2026,6,5),
        fields=['turnover'],state='confirmed',reason='隔离测试：旧转换遗漏千元换算',evidence={'isolated_fault':True},official_id=bad_id)
    assert query(session,old,iid,fields=['turnover'])['items']==[]
    assert query(session,old,iid,fields=['close'])['state']=='available'
    manifest=json.loads(ex.manifest_json);manifest['transform']['version']='2'
    newexecution=register_execution(session,manifest,b'isolated corrected converter evidence')
    work=create_work(session,kind='A',contract_id=oldwork.contract_id,execution_id=newexecution.id,
        dependency_id=oldwork.dependency_id,parameters=json.loads(oldwork.parameters_json),source_ref_id=oldwork.source_ref_id)
    claimed=claim(session,work_id=work.id);normalize_batch(session,work.id,claimed.lease_epoch)
    b=create_governance(session,normalization_id=work.id,execution_id=newexecution.id,policy_id=policy.id,
        parent_release_id=old.id,expected_head_revision=1,expected_issue_epoch=1)
    claimed=claim(session,work_id=b.id);new=stage_decisions(session,b.id,claimed.lease_epoch);publish(session,new.id,claimed.lease_epoch)
    after=query(session,new,iid,fields=['close','turnover'])
    assert after['state']=='available'
    assert [r['close'] for r in after['items']]==[r['close'] for r in before['items']]
    assert after['items'][0]['turnover']=='5925661225'
    record_issue(session,scope_key=oldwork.scope_key,instrument_id=iid,start=issue.start,end=issue.end,
        fields=['turnover'],state='resolved',reason='隔离测试：新转换已验证',evidence={'replacement':str(new.id)},
        issue_id=issue.issue_id,official_id=bad_id)
    assert query(session,old,iid,fields=['turnover'])['state']=='partial'
    assert query(session,old,iid,fields=['turnover'])['items']==[]
    assert session.get(OfficialBar,bad_id).turnover==Decimal('5925661.225')
    # M4 historical process views must retain the transform used by each work,
    # while comparison suppresses the subsequently restricted old amount.
    from app.data_foundation.views import process_detail,release_changes
    assert process_detail(session,oldwork)['rules']['transform']['version'] != process_detail(session,work)['rules']['transform']['version']
    req=DataRequirement(dataset_id='market.bar.daily',contract_version='1.0',profile_id='default',semantic_series_id=SERIES,
        subjects=[iid],business_range={'from':'2026-06-05','to':'2026-06-08'},fields=['close','turnover'],release=new.id)
    diff=release_changes(session,req,old.id,new.id,lambda:'test')
    assert 'turnover' not in diff['items'][0]['before']
    assert diff['items'][0]['after']['turnover']=='5925661225'
    assert diff['items'][0]['kind']=='restricted_comparison'
    # Rolling back the policy/input creates a new revision, but must not make
    # a previously confirmed erroneous candidate readable under a fresh ID.
    rollback = create_governance(session, normalization_id=oldwork.id, execution_id=ex.id,
        policy_id=policy.id, parent_release_id=new.id, expected_head_revision=2, expected_issue_epoch=2)
    lease = claim(session, work_id=rollback.id)
    rolled = stage_decisions(session, rollback.id, lease.lease_epoch)
    publish(session, rolled.id, lease.lease_epoch)
    assert query(session, rolled, iid, fields=['turnover'])['items'] == []
    assert query(session, rolled, iid, fields=['close'])['state'] == 'available'
    replay_manifest = json.loads(ex.manifest_json)
    replay_manifest['transform']['version'] = 'rollback-replay'
    replay_execution = register_execution(session, replay_manifest, b'isolated old converter replay')
    replay = create_work(session, kind='A', contract_id=oldwork.contract_id, execution_id=replay_execution.id,
        dependency_id=oldwork.dependency_id, parameters=json.loads(oldwork.parameters_json), source_ref_id=oldwork.source_ref_id)
    with patch('app.data_foundation.tushare.optional_amount', return_value=(Decimal('5925661.225'), None)):
        lease = claim(session, work_id=replay.id)
        normalize_batch(session, replay.id, lease.lease_epoch)
    rerun = create_governance(session, normalization_id=replay.id, execution_id=replay_execution.id,
        policy_id=policy.id, parent_release_id=rolled.id, expected_head_revision=3, expected_issue_epoch=2)
    lease = claim(session, work_id=rerun.id)
    replayed = stage_decisions(session, rerun.id, lease.lease_epoch)
    publish(session, replayed.id, lease.lease_epoch)
    assert query(session, replayed, iid, fields=['turnover'])['items'] == []
    assert query(session, replayed, iid, fields=['close'])['state'] == 'available'


def test_real_governance_rejects_manually_changed_decision_plan(session):
    fixture=setup_daily(session)
    a,ex,policy,iid=fixture
    from app.data_foundation.governance import plan_actions
    manifest=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==a.id))
    actions=plan_actions(session,manifest.id,policy.id);actions[0]['reason']='arbitrary operator selection'
    b=create_work(session,kind='B',contract_id=a.contract_id,execution_id=ex.id,dependency_id=a.dependency_id,
        parameters={**json.loads(a.parameters_json),'actions':actions},candidate_manifest_id=manifest.id,policy_id=policy.id)
    claimed=claim(session,work_id=b.id)
    with pytest.raises(FoundationError,match='治理计划'):
        stage_decisions(session,b.id,claimed.lease_epoch)


def test_repeated_read_does_not_copy_official_values_or_change_head(session):
    from sqlalchemy import func
    fixture=setup_daily(session);release=release_daily(session,fixture)
    count=session.scalar(select(func.count()).select_from(OfficialBar))
    ids=None
    for _ in range(100):
        result=query(session,release,fixture[3],fields=['close'])
        current=[r['official_id'] for r in result['items']]
        assert ids is None or current==ids
        ids=current
    assert session.scalar(select(func.count()).select_from(OfficialBar))==count


def test_query_rechecks_auth_and_wrong_contract_release(session):
    f=setup_daily(session);release=release_daily(session,f)
    req=DataRequirement(dataset_id='market.bar.daily',contract_version='1.0',profile_id='default',semantic_series_id=SERIES,
        subjects=[f[3]],business_range={'from':'2026-06-05','to':'2026-06-08'},release=release.id)
    calls=iter(['owner','owner','changed'])
    with pytest.raises(FoundationError,match='认证'):
        query_official(session,req,lambda:next(calls))
    with pytest.raises(FoundationError,match='语义'):
        query(session,release,f[3],semantic_series_id='provider-forward')


def test_no_release_pit_unknown_and_zero_applicable_days(session):
    f=setup_daily(session);release=release_daily(session,f)
    assert query(session,release,f[3],time_mode='strict_public_pit')['state']=='unknown'
    empty=query(session,release,f[3],business_range={'from':'2026-06-06','to':'2026-06-07'})
    assert empty['state']=='available' and empty['items']==[]
    assert empty['scope_summary']['expected_business_keys']==0


def test_middle_gap_with_both_endpoints_present(session):
    f=setup_daily(session,middle_gap=True);release=release_daily(session,f)
    result=query(session,release,f[3],business_range={'from':'2026-06-05','to':'2026-06-09'},allow_partial=True)
    assert result['state']=='partial' and len(result['items'])==2
    assert result['scope_summary']['expected_business_keys']==3
    assert result['excluded'][0]['trade_date']=='2026-06-08'


def test_real_manifest_cannot_bypass_planner_by_claiming_fixture_domain(session):
    from app.data_foundation.governance import plan_actions
    from app.data_foundation.bars import domain_hash as fixture_hash
    a,ex,policy,iid=setup_daily(session)
    manifest=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==a.id))
    actions=plan_actions(session,manifest.id,policy.id)
    params={**json.loads(a.parameters_json),'domain':'bar-core-v1','domain_hash':fixture_hash(),'actions':actions}
    b=create_work(session,kind='B',contract_id=a.contract_id,execution_id=ex.id,dependency_id=a.dependency_id,
        parameters=params,candidate_manifest_id=manifest.id,policy_id=policy.id)
    claimed=claim(session,work_id=b.id)
    with pytest.raises(FoundationError,match='治理计划'):stage_decisions(session,b.id,claimed.lease_epoch)
