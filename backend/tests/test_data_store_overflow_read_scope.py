"""Real API/CurrentStore reads with producer-written exception collections."""
from dataclasses import replace
import hashlib
import json
import os

import pytest
from sqlalchemy import text

from app.data_store.adapters.normalize import normalize
from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.merge import MergeSpool
from app.data_store.pipeline import run_entry, read_entry_status, _status
from app.data_store.problem_samples import write_overflow
from app.data_store.readers import Query
from app.data_store.router import _frequency
from app.data_store.storage import CurrentStore
from tests.test_data_store_api import api, query_body, TOKEN
from tests.test_data_store_domain_samples import ready, sample
from tests.test_data_store_kernel import database, limits, store
from tests.test_data_store_local_pipeline import Inputs

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1',
                                reason='isolated PostgreSQL required')
HEADERS={'Authorization':'Bearer '+TOKEN}
PATH='/api/admin/data-store/query'


def install_overflow(st, entry, unit):
    # Exercise the actual producer and key codec, rather than replacing the
    # public reader's scope decision or inventing a parallel exception format.
    with st.budget.reserve('write') as space:
        spool=MergeSpool(space,entry.spec)
        try:
            spool.offer(replace(unit,rows=(),failure='SOURCE_REFRESH_FAILED'))
            partition=entry.spec.partitioner((*unit.key,'root'))
            proof=write_overflow(st,entry,partition,spool)
        finally:
            spool.db.close()
    status=read_entry_status(st,entry.id)
    status.update(overflow_restriction=proof,qualified=False)
    _status(st,entry,status)
    return proof


@pytest.fixture(params=['E24','E25'])
def overflow_api(api,request):
    client,st=api;entry=BY_ID[request.param];raw=sample(entry.id)
    run_entry(st,entry,Inputs(raw))
    unit=next(normalize(entry,raw));partition=entry.spec.partitioner((*unit.key,'root'))
    bad_subject=next('blocked-'+str(i) for i in range(256)
        if entry.spec.partitioner((unit.representation,'blocked-'+str(i),unit.object_key,'root'))==partition)
    bad=replace(unit,subject=bad_subject)
    proof=install_overflow(st,entry,bad)
    body=query_body(entry,dict(representation=unit.representation,subject=unit.subject,
        object_key=unit.object_key),columns=[],frequency=_frequency(entry))
    return client,st,entry,unit,bad,body,proof


def test_complete_verified_collection_preserves_good_object_and_real_bad_control(overflow_api):
    client,st,entry,unit,bad,body,proof=overflow_api
    before=(st.catalog.dataset(entry.spec.name),read_entry_status(st,entry.id),
            st.catalog.issues(entry.spec.name,limit=100))
    response=client.post(PATH,headers=HEADERS,json=body)
    assert response.status_code==200,response.text
    first=response.json();assert first['rows'] and first['rows'][0]['basis_state']=='valid'
    assert (first['rows'][0]['representation'],first['rows'][0]['subject'],
            first['rows'][0]['object_key'])==unit.key
    if first['next_cursor']:
        next_page=client.post(PATH,headers=HEADERS,json={**body,'cursor':first['next_cursor']})
        assert next_page.status_code==200 and next_page.json()['rows']
        assert next_page.json()['rows'][0]['member_key']!=first['rows'][0]['member_key']
    control=client.post(PATH,headers=HEADERS,json={**body,'subject':bad.subject})
    assert control.status_code==409 and control.json()['detail']['code']=='DATA_RESTRICTED'
    assert before==(st.catalog.dataset(entry.spec.name),read_entry_status(st,entry.id),
                   st.catalog.issues(entry.spec.name,limit=100))


@pytest.mark.parametrize('damage',['incomplete','missing','hash','wrong_entry','wrong_partition',
                                   'unknown_format','truncated','count','record','wrong_file'])
def test_unproved_collection_keeps_conservative_rejection(overflow_api,damage):
    client,st,entry,unit,bad,body,proof=overflow_api
    path=st.files.root/'.problem_samples'/proof['file']
    lines=[json.loads(line) for line in path.read_text().splitlines()]
    if damage=='incomplete':proof['complete_key_list']=False
    elif damage=='missing':path.unlink()
    elif damage=='hash':proof['sha256']='0'*64
    elif damage=='wrong_file':proof['file']='e99.jsonl'
    else:
        if damage=='wrong_entry':lines[0]['entry']='E99'
        elif damage=='wrong_partition':lines[0]['partition']='undated.b99'
        elif damage=='unknown_format':lines[0]['format']='unknown'
        elif damage=='truncated':lines=lines[:1]
        elif damage=='count':lines[0]['blocking_objects']+=1
        elif damage=='record':lines[1]['key']='not-a-key'
        content=('\n'.join(json.dumps(line) for line in lines)+'\n').encode()
        path.write_bytes(content);proof['sha256']=hashlib.sha256(content).hexdigest()
    status=read_entry_status(st,entry.id);status['overflow_restriction']=proof;_status(st,entry,status)
    response=client.post(PATH,headers=HEADERS,json=body)
    assert response.status_code==409 and response.json()['detail']['code']=='DATA_RESTRICTED'


def test_unlocated_subject_scope_is_not_an_exact_object_bypass(overflow_api):
    client,st,entry,unit,bad,body,_=overflow_api
    install_overflow(st,entry,replace(unit,object_key='unlocated'))
    response=client.post(PATH,headers=HEADERS,json=body)
    assert response.status_code==409 and response.json()['detail']['code']=='DATA_RESTRICTED'


@pytest.mark.parametrize('scope',['object','subject','global'])
def test_ordinary_issues_still_restrict_a_disjoint_overflow(overflow_api,scope):
    client,st,entry,unit,bad,body,_=overflow_api
    prefix=list(unit.key) if scope=='object' else list(unit.key[:2]) if scope=='subject' else []
    target={'scope_version':'object-key-v1','partition':None if scope!='object' else
            entry.spec.partitioner((*unit.key,'root')),'prefix':prefix,'blocking':True}
    with st.catalog.engine.begin() as c:
        c.execute(text("INSERT INTO data_store_issues(dataset,issue_key,scope_key,reason,evidence_token,target_json,resolution_json) VALUES (:d,'ordinary',:s,'SOURCE_REFRESH_FAILED','test',:t,'{}')"),
                  {'d':entry.spec.name,'s':'local.partition.'+entry.spec.partitioner((*unit.key,'root')),
                   't':json.dumps(target)})
    assert client.post(PATH,headers=HEADERS,json=body).status_code==409


def test_identity_and_member_bounds_do_not_confuse_neighbouring_keys(overflow_api):
    client,st,entry,unit,bad,body,_=overflow_api
    partition=entry.spec.partitioner((*unit.key,'root'))
    other_representation=next(unit.representation+'-'+str(i) for i in range(256)
        if entry.spec.partitioner((unit.representation+'-'+str(i),unit.subject,unit.object_key,'root'))==partition)
    for other in [replace(unit,representation=other_representation),
                  bad,
                  replace(unit,object_key=unit.object_key+'-near')]:
        install_overflow(st,entry,other)
        assert client.post(PATH,headers=HEADERS,json=body).status_code==200
    install_overflow(st,entry,replace(unit,object_key='later'))
    wide=Query(partitions=(partition,),lower=unit.key[:2]+('a',),upper=unit.key[:2]+('z',))
    with pytest.raises(DataStoreError) as error:st.read(entry.spec,wide)
    assert error.value.code=='DATA_RESTRICTED'
    members=Query(partitions=(partition,),lower=unit.key+('a',),upper=unit.key+('z',))
    st.read(entry.spec,members)


def test_next_page_rechecks_changed_quality_without_a_data_generation_change(overflow_api):
    client,st,entry,unit,bad,body,_=overflow_api
    first=client.post(PATH,headers=HEADERS,json=body).json();assert first['next_cursor']
    generation=st.catalog.dataset(entry.spec.name)['generation']
    install_overflow(st,entry,unit)
    assert st.catalog.dataset(entry.spec.name)['generation']==generation
    response=client.post(PATH,headers=HEADERS,json={**body,'cursor':first['next_cursor']})
    assert response.status_code==409 and response.json()['detail']['code']=='DATA_RESTRICTED'


def test_public_final_fence_rechecks_changed_exception_collection(overflow_api,monkeypatch):
    client,st,entry,unit,bad,body,_=overflow_api
    original=CurrentStore.read_many
    def change_after_read(instance,*args,**kwargs):
        result=original(instance,*args,**kwargs)
        install_overflow(st,entry,unit)
        return result
    monkeypatch.setattr(CurrentStore,'read_many',change_after_read)
    response=client.post(PATH,headers=HEADERS,json=body)
    assert response.status_code==409 and response.json()['detail']['code']=='DATA_RESTRICTED'


@pytest.mark.parametrize('guard',['unauthenticated','maintenance','coverage'])
def test_existing_public_admission_guards_remain_effective(overflow_api,guard):
    client,st,entry,unit,bad,body,_=overflow_api
    headers=HEADERS
    if guard=='unauthenticated':headers={}
    else:
        with st.catalog.engine.begin() as c:
            c.execute(text('INSERT INTO data_store_legacy_maintenance VALUES (1,:p)'),
                      {'p':'rebuilding' if guard=='maintenance' else 'reset_done'})
            if guard=='coverage':
                state=read_entry_status(st,entry.id);state.pop('full_coverage');_status(st,entry,state)
    response=client.post(PATH,headers=headers,json=body)
    assert response.status_code==(401 if guard=='unauthenticated' else 503)
