"""Kill real child processes at explicit transaction barriers in disposable PG."""
import json
import select as select_io
import subprocess
import sys
from pathlib import Path
from uuid import UUID
import pytest
from sqlalchemy import select, func
from app.data_foundation.work_models import Head, Release, Decision
from app.data_foundation.publication import publish
from tests.test_foundation_publication_postgresql import session, pytestmark, setup, normalize, governance

CHILD = '''
import json,sys,time
from uuid import UUID
from sqlalchemy.orm import Session
from app.db.session import get_engine
from app.data_foundation.publication import publish
payload=json.loads(sys.argv[1])
with Session(get_engine()) as session:
    if payload.get('operation') == 'normalize':
        from app.data_foundation.work import claim
        from app.data_foundation.bars import normalize_batch
        work=claim(session,work_id=UUID(payload['work']))
        normalize_batch(session,work.id,work.lease_epoch)
    elif payload.get('operation') == 'scan':
        from app.data_foundation.intake import scan_page
        scan_page(session,UUID(payload['scan']),limit=1)
    else:
        publish(session,UUID(payload['release']),payload['epoch'])
    if payload['commit']:session.commit()
    print('barrier',flush=True)
    time.sleep(60)
'''


def at_barrier(payload):
    process=subprocess.Popen([sys.executable,'-c',CHILD,json.dumps(payload)],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    if not select_io.select([process.stdout],[],[],15)[0]:
        process.kill();process.wait();pytest.fail('Child did not reach transaction barrier')
    if process.stdout.readline().strip()!='barrier':
        error=process.stderr.read();process.wait();pytest.fail(error)
    return process


@pytest.mark.parametrize('committed',[False,True])
@pytest.mark.parametrize('attempt',range(3))
def test_process_death_before_or_after_publication_commit(session,committed,attempt):
    fixture=setup(session)
    work,release=governance(session,fixture,normalize(session,fixture))
    rid,scope,epoch,wid=release.id,work.scope_key,work.lease_epoch,work.id
    session.commit()
    child=at_barrier(dict(release=str(rid),epoch=epoch,commit=committed))
    child.kill();child.wait(timeout=5)
    session.expire_all()
    head=session.get(Head,scope)
    assert (head is not None and head.release_id==rid) is committed
    # Response loss after commit must be idempotent; pre-commit loss retries the
    # same complete sealed result without reconstructing or duplicating values.
    result=publish(session,rid,epoch)
    assert result.id==rid
    session.commit()
    assert session.scalar(select(func.count()).select_from(Release).where(Release.work_id==wid))==1
    assert session.scalar(select(func.count()).select_from(Decision).where(Decision.work_id==wid))==2


@pytest.mark.parametrize('attempt',range(20))
def test_two_real_processes_cannot_both_advance_same_head(session,attempt):
    fixture=setup(session);manifest=normalize(session,fixture)
    first,a=governance(session,fixture,manifest)
    actions=json.loads(first.parameters_json)['actions'];actions[0]['reason']='second publication candidate'
    second,b=governance(session,fixture,manifest,actions=actions)
    payloads=[dict(release=str(r.id),epoch=w.lease_epoch,commit=True) for w,r in [(first,a),(second,b)]]
    scope=first.scope_key;session.commit()
    # Both start from the same parent-less precondition. Separate interpreters
    # exercise actual PostgreSQL locks; loser must be superseded, never replace.
    code=CHILD.replace("    print('barrier',flush=True)\n    time.sleep(60)","    print('done',flush=True)")
    children=[subprocess.Popen([sys.executable,'-c',code,json.dumps(p)],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for p in payloads]
    try:
        for child in children:
            out,err=child.communicate(timeout=15)
            assert child.returncode==0,err
        session.expire_all()
        states=[session.get(Release,UUID(p['release'])).status for p in payloads]
        assert states.count('published')==1
        assert str(session.get(Head,scope).release_id) in [p['release'] for p in payloads]
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.wait()


@pytest.mark.parametrize('committed',[False,True])
@pytest.mark.parametrize('attempt',range(3))
def test_process_death_keeps_normalization_checkpoint_atomic(session,committed,attempt):
    from app.data_foundation.work_models import Work, Candidate, CandidateManifest
    fixture=setup(session,count=201)
    wid=fixture['work'].id;session.commit()
    child=at_barrier(dict(operation='normalize',work=str(wid),commit=committed))
    child.kill();child.wait(timeout=5);session.expire_all()
    work=session.get(Work,wid)
    expected=200 if committed else 0
    assert work.cursor==expected
    assert session.scalar(select(func.count()).select_from(Candidate).where(Candidate.work_id==wid))==expected
    assert session.scalar(select(CandidateManifest).where(CandidateManifest.work_id==wid)) is None
    fixture['work']=work
    manifest=normalize(session,fixture)
    assert manifest.row_count==201 and work.cursor==201
    assert session.scalar(select(func.count()).select_from(Candidate).where(Candidate.work_id==wid))==201


@pytest.mark.parametrize('committed',[False,True])
@pytest.mark.parametrize('attempt',range(3))
def test_process_death_keeps_scan_registration_and_cursor_atomic(session,committed,attempt):
    from uuid import uuid4
    from app.data_foundation.intake import register_scope,set_paused,start_scan,scan_page
    from app.data_foundation.intake_models import IntakeItem
    from app.data_foundation.catalog import now
    from tests.test_foundation_sources import execution
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
    from app.data_ingestion.tonghuashun.contracts import exact_json,content_hash
    subject='crash-'+uuid4().hex
    ex=execution(session)
    scope=register_scope(session,dataset='etf_daily',subject=subject,variant='default',
        selector={'start':'2026-06-24','end':'2026-09-15'},decoder_id=ex.id)
    set_paused(session,scope.id,False)
    data={'item':[{'date_ms':1,'close':'1.2'}]}
    for _ in range(3):
        session.add(Observation(id=uuid4(),dataset='etf_daily',subject=subject,variant='default',observed_at=now(),
            request_json='{}',data_json=exact_json(data),content_hash=content_hash(data),row_count=1,chain_depth=0))
    session.flush();scan=start_scan(session,scope.id,uuid4().hex)
    sid,scope_id=scan.id,scope.id;session.commit()
    child=at_barrier(dict(operation='scan',scan=str(sid),commit=committed))
    child.kill();child.wait(timeout=5);session.expire_all()
    count=lambda:session.scalar(select(func.count()).select_from(IntakeItem).where(IntakeItem.scope_id==scope_id))
    assert count()==(1 if committed else 0)
    for _ in range(5):
        result=scan_page(session,sid,limit=1)
        session.commit()
        if result['status']=='completed':break
    assert result['status']=='completed' and count()==3
