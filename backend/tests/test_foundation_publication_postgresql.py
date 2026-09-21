"""M2 typed mechanism acceptance on isolated PostgreSQL; no live provider data."""
import json
import os
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, update, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.instruments.models import Instrument
from app.data_foundation.catalog import register_dependencies, register_definition
from app.data_foundation.canonical import FoundationError, digest
from app.data_foundation.contracts import register_initial_catalog
from app.data_foundation.identity import register_binding
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.work import create_work, claim, fenced, finish_batch
from app.data_foundation.bars import normalize_batch, domain_hash
from app.data_foundation.publication import stage_decisions, publish, target_key
from app.data_foundation.quality import record_issue, read_release
from app.data_foundation.work_models import Work, Candidate, CandidateBar, CandidateManifest, CandidateEntry, OfficialBar, Head, Release, Decision, BlockMember, BlockRef
from tests.test_foundation_sources import execution

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='requires disposable PostgreSQL')


@pytest.fixture
def session(request):
    started=datetime.now(timezone.utc)
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        yield session
        evidence_dir=os.getenv('FOUNDATION_EVIDENCE_DIR')
        if evidence_dir:
            from pathlib import Path
            from app.data_foundation.canonical import encode
            from app.data_foundation.execution import installed_code_hash
            from app.data_foundation.models import Execution
            works=session.scalars(select(Work).where(Work.created_at >= started)).all()
            evidence={
                'test':request.node.name,'evidence_kind':'isolated_fixture',
                'database_version':session.scalar(text('SHOW server_version')),
                'installed_code_hash':installed_code_hash(),
                'works':[{'id':w.id,'input_fingerprint':w.fingerprint,'execution_manifest_hash':session.get(Execution,w.execution_id).manifest_hash,
                    'status':w.status,'cursor':w.cursor} for w in works],
                'releases':[{'id':r.id,'manifest_hash':r.manifest_hash,'status':r.status} for r in session.scalars(select(Release).where(Release.work_id.in_([w.id for w in works])))],
            }
            folder=Path(evidence_dir);folder.mkdir(parents=True,exist_ok=True)
            (folder/(request.node.name+'.json')).write_text(encode(evidence)+'\n')
        session.rollback()
    engine.dispose()


def setup(session, count=2, price='1.2345678901', series=None):
    key = uuid4().hex
    instrument = Instrument(id=uuid4(), asset_class='etf')
    session.add(instrument); session.flush()
    ex = execution(session)
    contract, _, initial_policy, _ = register_initial_catalog(session)
    series = series or 'fixture-' + key
    policy_payload = json.loads(initial_policy.definition_json)
    policy_payload['series'] = series
    policy = register_definition(session, kind='policy', name='fixture-policy-'+key, version='1', definition=policy_payload)
    directory = register_baseline(session, source='tushare', dataset='etf_directory', scope={}, rows=[
        {'source':'tushare', 'ts_code':key, 'etf_id':str(instrument.id)}], observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=key)
    binding = register_binding(session, source_ref_id=directory.id, subject=key, instrument_id=instrument.id,
        valid_from=date(2026,1,1), valid_to=date(2027,1,1), binding_version=key, status='resolved',
        evidence={'reviewer':'fixture', 'reference':'isolated-fixture', 'valid_from':'2026-01-01','valid_to':'2027-01-01'})
    dep = register_dependencies(session, [{'binding_id':binding.id,'purpose':'identity'}, {'source_ref_id':directory.id,'purpose':'directory'}, {'execution_id':ex.id,'purpose':'execution'}])
    rows = [{'schema':'standardized-bar-v1','source_subject':key,'trade_date':str(date(2026,1,1)+timedelta(days=i)),
        'series':series,'open':price,'high':price,'low':price,'close':price,'volume':None,'turnover':'100.12'} for i in range(count)]
    ref = register_baseline(session, source='tushare', dataset='standardized_bar_input', scope={}, rows=rows,
        observed_at=datetime.now(timezone.utc), decoder_id=ex.id, event_key=key)
    params = dict(dataset='market.bar.daily',major=1,profile='default',series=series,start='2026-01-01',end='2026-12-31',domain='bar-core-v1',domain_hash=domain_hash())
    work = create_work(session, kind='A', contract_id=contract.id, execution_id=ex.id, dependency_id=dep.id, parameters=params, source_ref_id=ref.id)
    return dict(work=work, params=params, policy=policy, ex=ex, dep=dep, contract=contract, instrument=instrument, ref=ref)


def normalize(session, fixture):
    work = fixture['work']
    while work.status != 'succeeded':
        claimed = claim(session, work_id=work.id)
        normalize_batch(session, work.id, claimed.lease_epoch)
    return session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == work.id))


def governance(session, fixture, manifest, parent=None, revision=0, issue_epoch=0, actions=None):
    if actions is None:
        bars = session.scalars(select(CandidateBar).join(CandidateEntry, CandidateEntry.candidate_id == CandidateBar.candidate_id)
            .where(CandidateEntry.manifest_id == manifest.id).order_by(CandidateBar.trade_date)).all()
        actions = [dict(target_key=target_key(b.instrument_id,b.trade_date), instrument_id=str(b.instrument_id), trade_date=str(b.trade_date),
            action='select', candidate_id=str(b.candidate_id),parent_official_id=None,reason='SINGLE_SOURCE') for b in bars]
    work = create_work(session,kind='B',contract_id=fixture['contract'].id,execution_id=fixture['ex'].id,dependency_id=fixture['dep'].id,
        parameters={**fixture['params'],'actions':actions},candidate_manifest_id=manifest.id,policy_id=fixture['policy'].id,
        parent_release_id=parent.id if parent else None,expected_head_revision=revision,expected_issue_epoch=issue_epoch)
    claimed = claim(session, preferred_kind='B', work_id=work.id)
    release = stage_decisions(session, work.id, claimed.lease_epoch)
    return work, release


def read(session, release, epoch=0):
    return json.loads(read_release(session,release_id=release.id,expected_issue_epoch=epoch,authenticate=lambda:'test-owner'))


def test_g02_typed_official_values_and_d06_retry(session):
    f = setup(session)
    assert create_work(session,kind='A',contract_id=f['contract'].id,execution_id=f['ex'].id,dependency_id=f['dep'].id,
        parameters=f['params'],source_ref_id=f['ref'].id).id == f['work'].id
    manifest = normalize(session,f)
    work, release = governance(session,f,manifest)
    assert release.status == 'sealed' and session.get(Head,work.scope_key) is None
    publish(session,release.id,work.lease_epoch)
    result = read(session,release)
    assert result['status'] == 'available' and len(result['items']) == 2
    assert result['items'][0]['close'] == '1.2345678901'
    # Ordinary reads depend only on typed official records and issue metadata.
    from unittest.mock import patch
    with patch('app.data_foundation.source_refs.read_source', side_effect=AssertionError('source decoder invoked')):
        assert read(session,release)['items'] == result['items']
    assert publish(session,release.id,work.lease_epoch).id == release.id
    assert len(session.scalars(select(OfficialBar).join(Decision,Decision.id==OfficialBar.decision_id).where(Decision.work_id==work.id)).all())==2


def test_d08_partial_batch_does_not_seal_and_d06_recovery(session):
    f = setup(session,count=201)
    work=claim(session,work_id=f['work'].id)
    normalize_batch(session,work.id,work.lease_epoch)
    assert work.cursor==200 and work.status=='queued'
    assert session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==work.id)) is None
    stale_epoch=work.lease_epoch
    work=claim(session,work_id=work.id)
    with pytest.raises(FoundationError): fenced(session,work.id,stale_epoch)
    normalize_batch(session,work.id,work.lease_epoch)
    assert work.cursor==201 and work.status=='succeeded'
    assert len(session.scalars(select(Candidate).where(Candidate.work_id==work.id)).all())==201


def test_d09_late_parent_cannot_replace_head(session):
    f=setup(session); manifest=normalize(session,f)
    work,release=governance(session,f,manifest)
    # Changing an explicit reason creates another fixed governance event.
    actions=json.loads(work.parameters_json)['actions']
    actions[0]['reason']='SECOND_ASSESSMENT'
    loser,late=governance(session,f,manifest,actions=actions)
    publish(session,release.id,work.lease_epoch)
    assert publish(session,late.id,loser.lease_epoch) is None
    assert loser.status=='superseded' and session.get(Head,work.scope_key).release_id==release.id


def test_g11_g13_withdrawal_and_current_issue_guard(session):
    f=setup(session); manifest=normalize(session,f)
    work,release=governance(session,f,manifest);publish(session,release.id,work.lease_epoch)
    old=read(session,release)
    issue=record_issue(session,scope_key=work.scope_key,instrument_id=f['instrument'].id,start=date(2026,1,1),end=date(2026,1,1),
        fields=['close'],state='confirmed',reason='fixture incorrect value',evidence={'fixture':True},official_id=__import__('uuid').UUID(old['items'][0]['official_id']))
    with pytest.raises(FoundationError,match='已改变'): read(session,release)
    assert read(session,release,1)['items']==[]
    actions=[dict(target_key=target_key(f['instrument'].id,date(2026,1,1)),instrument_id=str(f['instrument'].id),trade_date='2026-01-01',action='withdraw',candidate_id=None,parent_official_id=None,reason='SOURCE_WITHDRAWN')]
    nextwork,nextrelease=governance(session,f,manifest,parent=release,revision=1,issue_epoch=1,actions=actions)
    publish(session,nextrelease.id,nextwork.lease_epoch)
    result=read(session,nextrelease,1)
    assert result['items']==[] and result['gaps'][0]['reason']=='withdrawn'
    members=session.scalars(select(BlockMember).where(BlockMember.block_id.in_(select(BlockRef.block_id).where(BlockRef.release_id==nextrelease.id)))).all()
    assert {m.state for m in members}=={'value','withdrawn'}
    record_issue(session,scope_key=work.scope_key,instrument_id=f['instrument'].id,start=issue.start,end=issue.end,
        fields=['close'],state='resolved',reason='replacement restricted',evidence={'replacement':str(nextrelease.id)},issue_id=issue.issue_id,official_id=issue.official_id)
    assert read(session,release,2)['items']==[]  # exact bad old revision remains restricted


def test_g15_equal_values_new_decision_and_immutable_release(session):
    f=setup(session);manifest=normalize(session,f)
    work,release=governance(session,f,manifest);publish(session,release.id,work.lease_epoch)
    nextwork,nextrelease=governance(session,f,manifest,parent=release,revision=1)
    publish(session,nextrelease.id,nextwork.lease_epoch)
    a,b=read(session,release)['items'],read(session,nextrelease)['items']
    assert a[0]['close']==b[0]['close'] and a[0]['decision_id']!=b[0]['decision_id']
    with pytest.raises(DBAPIError):
        with session.begin_nested(): session.execute(update(Release).where(Release.id==release.id).values(manifest_hash='0'*64))


def test_c08_c14_missing_dependencies_stop_work_without_touching_sources(session):
    from app.data_foundation.worker import run_once
    from app.data_foundation.read_models import processing_view
    f=setup(session)
    wid=f['work'].id
    session.commit()
    assert run_once(session.bind,work_id=wid,runtime_digest='')=='A'
    session.expire_all()
    work=session.get(Work,wid)
    assert work.status=='dependency_missing' and work.cursor==0
    summary=processing_view(session,work)
    assert summary['current_release'] is None and summary['output_releases']==[]
    assert summary['counters']['total'] is None
    assert next(s for s in summary['steps'] if s['step']=='publication')['status']=='evidence_missing'


def test_two_claimants_and_expired_epoch_are_fenced(session):
    f=setup(session);wid=f['work'].id;session.commit()
    engine=session.bind
    with Session(engine) as first, Session(engine) as second:
        a=claim(first,work_id=wid)
        assert a is not None
        assert claim(second,work_id=wid) is None
        first.commit();second.rollback()
        first.execute(update(Work).where(Work.id==wid).values(lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))
        first.commit()
        b=claim(second,work_id=wid);second.commit()
        assert b.lease_epoch==2
        with pytest.raises(FoundationError):fenced(first,wid,1)
    # Finish the isolated committed work explicitly so it is not claimable by
    # later tests. No evidence rows are deleted to fake cleanup success.
    with Session(engine) as finish,finish.begin():
        row=fenced(finish,wid,2);finish_batch(finish,row,status='cancelled')


def test_g11_read_guard_linearizes_against_issue_writer(session):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    f=setup(session);manifest=normalize(session,f)
    work,release=governance(session,f,manifest);publish(session,release.id,work.lease_epoch)
    rid,scope,iid=release.id,work.scope_key,f['instrument'].id
    session.commit();engine=session.bind
    with Session(engine) as reader,ThreadPoolExecutor(max_workers=1) as pool:
        payload=read_release(reader,release_id=rid,expected_issue_epoch=0,authenticate=lambda:'owner')
        def restrict():
            with Session(engine) as writer,writer.begin():
                record_issue(writer,scope_key=scope,instrument_id=iid,start=date(2026,1,1),end=date(2026,1,2),
                    fields=['close'],state='confirmed',reason='fixture restriction',evidence={'fixture':True})
        future=pool.submit(restrict)
        try:
            with pytest.raises(TimeoutError):future.result(timeout=.2)
            assert json.loads(payload)['status']=='available'
            reader.commit()
        finally:reader.rollback()
        future.result(timeout=5)
    with pytest.raises(FoundationError):read_release(session,release_id=rid,expected_issue_epoch=0,authenticate=lambda:'owner')


def test_concurrent_publications_have_exactly_one_head_winner(session):
    from concurrent.futures import ThreadPoolExecutor
    f=setup(session);manifest=normalize(session,f)
    work,a=governance(session,f,manifest)
    actions=json.loads(work.parameters_json)['actions'];actions[0]['reason']='concurrent-other-assessment'
    other,b=governance(session,f,manifest,actions=actions)
    jobs=[(a.id,work.lease_epoch),(b.id,other.lease_epoch)]
    scope=work.scope_key;engine=session.bind;session.commit()
    def publish_one(job):
        with Session(engine) as writer,writer.begin():
            result=publish(writer,*job)
            return result.id if result else None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(publish_one,jobs))
    assert sum(value is not None for value in results)==1
    session.expire_all()
    assert session.get(Head,scope).release_id in results


def test_issue_commits_before_failed_publication_still_restricts_old_head(session):
    f=setup(session);manifest=normalize(session,f)
    work,old=governance(session,f,manifest);publish(session,old.id,work.lease_epoch)
    newer,draft=governance(session,f,manifest,parent=old,revision=1)
    record_issue(session,scope_key=work.scope_key,instrument_id=f['instrument'].id,start=date(2026,1,1),end=date(2026,1,2),
        fields=['close'],state='confirmed',reason='invalid range',evidence={'fixture':True})
    assert publish(session,draft.id,newer.lease_epoch) is None
    assert session.get(Head,work.scope_key).release_id==old.id
    assert read(session,old,1)['status']=='unavailable'


def test_d10_source_a_b_a_keeps_three_official_revisions(session):
    f=setup(session,count=1);previous=None
    for revision,price in enumerate(['1.2345678901','2','1.2345678901']):
        if revision:
            original=json.loads(session.get(__import__('app.data_foundation.models',fromlist=['BaselineBlock']).BaselineBlock,(f['ref'].baseline_id,0)).payload_json)
            for key in ('open','high','low','close'):original[0][key]=price
            f['ref']=register_baseline(session,source='tushare',dataset='standardized_bar_input',scope={},rows=original,
                observed_at=datetime.now(timezone.utc),decoder_id=f['ex'].id,event_key=uuid4().hex)
            f['work']=create_work(session,kind='A',contract_id=f['contract'].id,execution_id=f['ex'].id,dependency_id=f['dep'].id,parameters=f['params'],source_ref_id=f['ref'].id)
        manifest=normalize(session,f)
        work,release=governance(session,f,manifest,parent=previous,revision=revision)
        publish(session,release.id,work.lease_epoch)
        assert read(session,release)['items'][0]['close']==price
        previous=release
    assert len(session.scalars(select(OfficialBar).where(OfficialBar.instrument_id==f['instrument'].id)).all())==3


def test_c20_archive_hash_missing_files_and_fixed_execution_closure(session,tmp_path):
    import hashlib
    import platform
    from pathlib import Path
    from app.data_foundation.__main__ import local_execution
    from app.data_foundation.execution import register_archive, replay_status, verify_execution
    # Artificial archive bytes exercise registry and loss handling only. The
    # deployment verifier separately performs a real Docker save/load/offline run.
    image='sha256:'+'1'*64
    ex=local_execution(session,'a'*40,image)
    path=tmp_path/('1'*64+'.tar');path.write_bytes(b'isolated archive-registry fixture')
    lock_hash=hashlib.sha256((Path(__file__).resolve().parents[1]/'uv.lock').read_bytes()).hexdigest()
    evidence=dict(image_digest=image,archive_key=path.name,archive_hash=hashlib.sha256(path.read_bytes()).hexdigest(),byte_count=path.stat().st_size,
        verification=dict(restored_image_digest=image,network='none',python=platform.python_version(),lock_hash=lock_hash))
    register_archive(session,ex.id,evidence,tmp_path)
    assert replay_status(session,ex.id,tmp_path)=='ready'
    f=setup(session);f['work'].execution_id  # original work inputs must not mutate
    new=create_work(session,kind='A',contract_id=f['contract'].id,execution_id=ex.id,dependency_id=f['dep'].id,
        parameters=f['params'],source_ref_id=f['ref'].id)
    verify_execution(session,new,image,tmp_path)
    with pytest.raises(FoundationError):verify_execution(session,new,'sha256:'+'2'*64,tmp_path)
    from unittest.mock import patch
    original_open=Path.open
    def unreadable_archive(file,*args,**kwargs):
        if file.suffix=='.tar':raise PermissionError('isolated permission failure')
        return original_open(file,*args,**kwargs)
    with patch.object(Path,'open',unreadable_archive):
        assert replay_status(session,ex.id,tmp_path)=='dependency_missing'
        with pytest.raises(FoundationError):verify_execution(session,new,image,tmp_path)
        with pytest.raises(FoundationError):register_archive(session,ex.id,evidence,tmp_path)
    path.unlink()
    assert replay_status(session,ex.id,tmp_path)=='dependency_missing'
    with pytest.raises(FoundationError):verify_execution(session,new,image,tmp_path)


def test_worker_commits_a_batch_only_with_verified_fixed_dependencies(session):
    from unittest.mock import patch
    from app.data_foundation.worker import run_once
    f=setup(session);wid=f['work'].id;session.commit()
    # This mechanism test isolates process/session behavior; archive verification
    # itself is covered by the separate C20 fixture and production restore proof.
    with patch('app.data_foundation.worker.verify_execution'):
        assert run_once(session.bind,work_id=wid)=='A'
    session.expire_all()
    assert session.get(Work,wid).status=='succeeded'
    assert session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==wid)) is not None


def test_read_guard_rechecks_owner_and_optional_nulls(session):
    f=setup(session);manifest=normalize(session,f)
    work,release=governance(session,f,manifest);publish(session,release.id,work.lease_epoch)
    owners=iter(['first','changed'])
    with pytest.raises(FoundationError):read_release(session,release_id=release.id,expected_issue_epoch=0,authenticate=lambda:next(owners))
    result=json.loads(read_release(session,release_id=release.id,expected_issue_epoch=0,authenticate=lambda:'same',fields=('volume',)))
    assert result['status']=='unavailable' and result['items']==[]


def test_cancelled_work_can_resume_only_remaining_units(session):
    from app.data_foundation.work import cancel,retry
    f=setup(session,count=201)
    work=claim(session,work_id=f['work'].id)
    normalize_batch(session,work.id,work.lease_epoch)
    cancel(session,work.id)
    assert claim(session,work_id=work.id) is None
    assert work.cursor==200
    retry(session,work.id)
    claimed=claim(session,work_id=work.id)
    normalize_batch(session,work.id,claimed.lease_epoch)
    assert work.cursor==201 and work.status=='succeeded'


def test_sealed_release_survives_worker_loss_and_does_not_duplicate_values(session):
    f=setup(session);manifest=normalize(session,f)
    work,release=governance(session,f,manifest)
    rid,wid=release.id,work.id
    session.execute(update(Work).where(Work.id==wid).values(lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))
    session.flush()
    newer=claim(session,preferred_kind='B',work_id=wid)
    assert newer.lease_epoch==2
    publish(session,rid,newer.lease_epoch)
    assert len(read(session,release)['items'])==2
    assert len(session.scalars(select(OfficialBar).join(Decision,Decision.id==OfficialBar.decision_id).where(Decision.work_id==wid)).all())==2


def test_g13_unchanged_month_blocks_are_reused(session):
    f=setup(session,count=35);manifest=normalize(session,f)
    work,release=governance(session,f,manifest);publish(session,release.id,work.lease_epoch)
    old={r.partition_key:r.block_id for r in session.scalars(select(BlockRef).where(BlockRef.release_id==release.id))}
    action=dict(target_key=target_key(f['instrument'].id,date(2026,1,1)),instrument_id=str(f['instrument'].id),trade_date='2026-01-01',action='block',candidate_id=None,parent_official_id=None,reason='QUALITY_BLOCKED')
    newwork,newrelease=governance(session,f,manifest,parent=release,revision=1,actions=[action]);publish(session,newrelease.id,newwork.lease_epoch)
    new={r.partition_key:r.block_id for r in session.scalars(select(BlockRef).where(BlockRef.release_id==newrelease.id))}
    assert new[str(f['instrument'].id)+'/2026-02']==old[str(f['instrument'].id)+'/2026-02']
    assert new[str(f['instrument'].id)+'/2026-01']!=old[str(f['instrument'].id)+'/2026-01']
    result=json.loads(read_release(session,release_id=newrelease.id,expected_issue_epoch=0,authenticate=lambda:'owner',allow_partial=True))
    assert len(result['items'])==34 and result['gaps'][0]['reason']=='blocked'


def test_process_summary_separates_a_work_from_current_b_publication(session):
    from app.data_foundation.read_models import processing_view
    f=setup(session);manifest=normalize(session,f)
    work,release=governance(session,f,manifest);publish(session,release.id,work.lease_epoch)
    a=processing_view(session,f['work'])
    assert a['current_release']==str(release.id) and a['output_releases']==[]
    assert next(s for s in a['steps'] if s['step']=='publication')['status']=='evidence_missing'
    b=processing_view(session,work)
    assert b['output_releases']==[str(release.id)]


def test_d05_database_binding_protection_and_existing_identity(session):
    from app.data_foundation.models import Binding, DependencyEntry
    from app.data_foundation.identity import resolve_binding
    f=setup(session)
    binding_id=session.scalar(select(DependencyEntry.binding_id).where(DependencyEntry.manifest_id==f['dep'].id,DependencyEntry.binding_id.is_not(None)))
    binding=session.get(Binding,binding_id)
    assert resolve_binding(session,[binding.id],binding.source,binding.subject,date(2026,1,1)).instrument_id==f['instrument'].id
    with pytest.raises(FoundationError):resolve_binding(session,[binding.id],binding.source,binding.subject,date(2027,1,1))
    with pytest.raises(DBAPIError):
        with session.begin_nested():
            duplicate=Binding(**{c.name:getattr(binding,c.name) for c in Binding.__table__.columns if c.name!='id'})
            duplicate.valid_from=date(2026,2,1)
            session.add(duplicate);session.flush()
    with pytest.raises(DBAPIError):
        with session.begin_nested():session.execute(update(Binding).where(Binding.id==binding.id).values(valid_to=date(2030,1,1)))
