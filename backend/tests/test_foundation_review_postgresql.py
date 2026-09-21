"""Review regressions use real publication/readers in disposable PostgreSQL."""
import json
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4
from unittest.mock import patch
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_daily_postgresql import setup_daily, release_daily, query
from tests.test_foundation_batches_postgresql import multi
from app.data_foundation.work import create_work, claim
from app.data_foundation.work_models import CandidateManifest, Head
from app.data_foundation.catalog import register_execution
from app.data_foundation.publication import stage_decisions, publish
from app.data_foundation.governance import create_governance, create_multi_governance
from app.data_foundation.inputs import seal_input_set
from app.data_foundation.bars import normalize_batch
from app.data_foundation.quality import record_issue
from app.data_foundation.query import dataset_view


def replay(session, fixture, normalize, changes=None):
    """Create explicit converter evidence; never alter an immutable candidate."""
    old, ex = fixture[:2]
    manifest=json.loads(ex.manifest_json);manifest['transform']['version']=uuid4().hex
    newex=register_execution(session,manifest,b'isolated review converter')
    work=create_work(session,kind='A',contract_id=old.contract_id,execution_id=newex.id,
        dependency_id=old.dependency_id,parameters={**json.loads(old.parameters_json),**(changes or {})},source_ref_id=old.source_ref_id)
    while work.status!='succeeded':
        lease=claim(session,work_id=work.id);normalize(session,work.id,lease.lease_epoch)
    return (work,newex,*fixture[2:])


def publish_after(session, f, parent, epoch=0, governance=create_governance, stage=stage_decisions):
    h=session.get(Head,f[0].scope_key)
    b=governance(session,normalization_id=f[0].id,execution_id=f[1].id,policy_id=f[2].id,
        parent_release_id=parent.id,expected_head_revision=h.revision,expected_issue_epoch=epoch)
    lease=claim(session,work_id=b.id);result=stage(session,b.id,lease.lease_epoch);publish(session,result.id,lease.lease_epoch)
    return result


@pytest.mark.parametrize('unrelated', ['volume','close'])
def test_field_restriction_survives_unrelated_daily_correction(session, unrelated):
    from app.data_foundation.tushare import map_row
    f=setup_daily(session);old=release_daily(session,f)
    oid=UUID(query(session,old,f[3],fields=['turnover'])['items'][0]['official_id'])
    issue=record_issue(session,scope_key=f[0].scope_key,instrument_id=f[3],start=date(2026,6,5),end=date(2026,6,5),
        fields=['turnover'],state='confirmed',reason='隔离测试成交额错误',evidence={'test':True},official_id=oid)
    def changed(raw,iid):
        values,quality=map_row(raw,iid);values[unrelated]=Decimal('1000' if unrelated=='volume' else '4.0');return values,quality
    with patch('app.data_foundation.tushare.map_row',side_effect=changed): later=replay(session,f,normalize_batch)
    new=publish_after(session,later,old,1)
    assert query(session,new,f[3],fields=['turnover'])['items']==[]
    assert query(session,new,f[3],fields=['close'])['state']=='available'
    def corrected(raw,iid):
        values,quality=map_row(raw,iid);values['turnover']+=Decimal('1');return values,quality
    with patch('app.data_foundation.tushare.map_row',side_effect=corrected): fixed=replay(session,f,normalize_batch)
    final=publish_after(session,fixed,new,1)
    assert query(session,final,f[3],fields=['turnover'])['state']=='available'
    record_issue(session,scope_key=f[0].scope_key,instrument_id=f[3],start=issue.start,end=issue.end,
        fields=['turnover'],state='resolved',reason='隔离修复已核对',evidence={'replacement':str(final.id)},official_id=oid,issue_id=issue.issue_id)
    assert query(session,new,f[3],fields=['turnover'])['items']==[]
    assert query(session,old,f[3],fields=['turnover'])['items']==[]


@pytest.mark.parametrize('change', ['rank','other_member','reorder'])
def test_report_restriction_survives_unrelated_member_correction(session,change):
    from tests.test_foundation_holdings_postgresql import setup_reports,release_reports,service,request
    from app.data_foundation.holding_work import normalize_report,normalize_batch as normalize,create_governance as govern,stage_decisions as stage
    from app.data_foundation.holding_issues import record_report_issue
    f=setup_reports(session);old=release_reports(session,f);svc=service(session)
    item=json.loads(svc.query_official(request(f,old)))['items'][0]
    record_report_issue(session,scope_key=f[0].scope_key,target=item['target_key'],fund_share_id=f[3],period_end=date(2025,3,31),
        fields=['hold_ratio'],state='confirmed',reason='隔离测试比例错误',evidence={'test':True},official_id=UUID(item['official_id']))
    def changed(**kwargs):
        result=normalize_report(**kwargs)
        if change=='rank':result['members'][0]['rank']=1
        else:result['members'][1]['hold_ratio']=Decimal('.05')
        if change=='reorder':
            result['members'].reverse()
            for i,m in enumerate(result['members']):m['member_ordinal']=i
        return result
    with patch('app.data_foundation.holding_work.normalize_report',side_effect=changed):later=replay(session,f,normalize)
    new=publish_after(session,later,old,1,govern,stage)
    assert json.loads(svc.check_capability(request(f,new)))['state']=='unavailable'
    assert json.loads(svc.check_capability(request(f,new,fields=['period_change_ratio'])))['state']=='available'
    def corrected(**kwargs):
        result=normalize_report(**kwargs)
        for member in result['members']:member['hold_ratio']=Decimal('.06')
        return result
    with patch('app.data_foundation.holding_work.normalize_report',side_effect=corrected):fixed=replay(session,f,normalize)
    final=publish_after(session,fixed,new,1,govern,stage)
    assert json.loads(svc.check_capability(request(f,final)))['state']=='available'
    assert json.loads(svc.check_capability(request(f,old)))['state']=='unavailable'


def test_multi_input_governance_summary_tracks_all_consumed_inputs(session):
    fixtures=[setup_daily(session),setup_daily(session)];b,_=multi(session,fixtures)
    lease=claim(session,work_id=b.id);release=stage_decisions(session,b.id,lease.lease_epoch);publish(session,release.id,lease.lease_epoch)
    assert dataset_view(session)['pending_governance'] is False
    from tests.test_foundation_holdings_postgresql import service
    assert json.loads(service(session).describe_dataset('market.bar.daily'))['pending_governance'] is False
    newer=setup_daily(session)
    assert dataset_view(session)['pending_governance'] is True
    # Reviewed historical exclusions count as settled, even when not selected.
    manifest=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==newer[0].id))
    retained=session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==fixtures[0][0].id))
    inputs=seal_input_set(session,[dict(manifest_id=manifest.id,role='historical',reason='已审查但未采用'),dict(manifest_id=retained.id,role='eligible',reason='继续采用')])
    next_b=create_multi_governance(session,input_set_id=inputs.id,execution_id=fixtures[0][1].id,policy_id=fixtures[0][2].id,parent_release_id=release.id,expected_head_revision=1)
    lease=claim(session,work_id=next_b.id);next_r=stage_decisions(session,next_b.id,lease.lease_epoch);publish(session,next_r.id,lease.lease_epoch)
    assert dataset_view(session)['pending_governance'] is False
    publish_after(session,fixtures[1],next_r)
    assert dataset_view(session)['pending_governance'] is False


@pytest.mark.parametrize('reverse', [False, True])
def test_inherited_discontinuous_coverage_accepts_history_gap_patch(session,reverse):
    """Exercise nested coverage through governance, releases, capability and assets."""
    from datetime import datetime, timezone
    from app.data_foundation.source_refs import read_source,register_baseline
    from tests.test_foundation_holdings_postgresql import service
    from app.data_foundation.query import DataRequirement
    from app.data_foundation.tushare import SERIES
    f=setup_daily(session,middle_gap=True)
    rows=read_source(session,f[0].source_ref_id)
    rows.append({**rows[0],'trade_date':'2026-06-08'})
    pieces=[]
    for day in ('2026-06-05','2026-06-09','2026-06-08'):
        ref=register_baseline(session,source='tushare',dataset='etf_daily',scope={},
            rows=[r for r in rows if r['trade_date']==day],observed_at=datetime.now(timezone.utc),
            decoder_id=f[1].id,event_key=uuid4().hex)
        work=create_work(session,kind='A',contract_id=f[0].contract_id,execution_id=f[1].id,
            dependency_id=f[0].dependency_id,parameters={**json.loads(f[0].parameters_json),'start':day,'end':day},source_ref_id=ref.id)
        lease=claim(session,work_id=work.id);normalize_batch(session,work.id,lease.lease_epoch)
        pieces.append((work,*f[1:]))
    b,_=multi(session,pieces[:2][::-1] if reverse else pieces[:2])
    lease=claim(session,work_id=b.id);old=stage_decisions(session,b.id,lease.lease_epoch);publish(session,old.id,lease.lease_epoch)
    patched=publish_after(session,pieces[2],old)
    # Random work UUIDs must not decide whether identical evidence is usable.
    result=query(session,patched,f[3],business_range={'from':'2026-06-08','to':'2026-06-09'},fields=['close'])
    assert result['state']=='available' and len(result['items'])==2
    req=DataRequirement(dataset_id='market.bar.daily',contract_version='1.0',profile_id='default',semantic_series_id=SERIES,
        subjects=[f[3]],business_range={'from':'2026-06-08','to':'2026-06-09'},release=patched.id,fields=['close'])
    svc=service(session)
    assert json.loads(svc.check_capability(req))['state']=='available'
    summary=json.loads(svc.describe_dataset('market.bar.daily'))
    assert summary['expected_business_keys']==3 and summary['official_keys']==3
    # The unobserved weekend is still unknown rather than silently filled.
    assert query(session,patched,f[3],business_range={'from':'2026-06-05','to':'2026-06-09'})['state']=='unknown'
