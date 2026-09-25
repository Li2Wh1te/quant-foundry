"""Pure D02 contract checks, small fixtures only."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
import ast
import json
import pytest

from app.data_store.adapters.contracts import LocalInput,digest,native_json
from app.data_store.adapters.registry import ENTRIES,BY_ID,BY_NATIVE,SYNTHETIC
from app.data_store.adapters.normalize import normalize
from app.data_store.local_sources import materialize_observation,EffectiveBasis,SourceLimits,RescueSources
from app.data_store.adapters.canonical import NativeInputError
from app.data_ingestion.tonghuashun.confirmation import returned_keys

NOW=datetime(2026,1,1,tzinfo=timezone.utc)


def test_all_71_baseline_entries_have_real_disposition_and_static_arrow_schema():
    assert len(ENTRIES)==71 and sum(e.business for e in ENTRIES)==60
    for e in ENTRIES:
        c=e.describe_capability()
        assert c['supplier_network_required'] is False
        if e.business:
            assert e.layout.model and e.spec.schema_id and len(e.spec.schema)<=128
            assert not e.spec.name.startswith('operations.')
        elif e.target:assert BY_ID[e.target].business
    root=Path(__file__).parents[1]/'app/data_store'
    for f in [*(root/'adapters').glob('*.py'),root/'local_sources.py',root/'pipeline.py']:
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node,ast.ImportFrom):assert not (node.module or '').startswith('app.data_foundation')


def daily(rows,n=0):
    data={'item':rows,'coverage':'observed_rows_only','adjust':'none','thscode':'TEST.SH'}
    return LocalInput('tonghuashun','stock_daily','TEST.SH','default',NOW+timedelta(seconds=n),data,digest(data))


def price(day=1743350400000):
    return dict(date_ms=day,open_price=Decimal('1'),high_price=Decimal('2'),low_price=Decimal('0.5'),
                close_price=Decimal('1.5'),volume=100,turnover=100)


def test_daily_window_is_split_by_business_date_not_one_large_points_payload():
    e=BY_ID['E50'];units=list(normalize(e,daily([price(),price(1743436800000)])))
    assert len(units)==2 and all(u.failure is None and len(u.rows)==1 for u in units)
    assert {u.object_key for u in units}=={'2025-03-31','2025-04-01'}
    assert not any('points' in f.name for f in e.spec.schema)


def test_C10_duplicate_point_and_malformed_report_never_publish_valid_siblings():
    e=BY_ID['E50'];units=list(normalize(e,daily([price(),price()])))
    assert len(units)==1 and units[0].failure
    e=BY_ID['E57'];data={'item':[{'thscode':'A.SH'}, {'thscode':None}]}
    raw=LocalInput('tonghuashun','index_constituents','IDX.SH','default',NOW,data,digest(data))
    result=list(normalize(e,raw));assert result and all(r.failure for r in result)
    empty=LocalInput('tonghuashun','index_constituents','IDX.SH','default',NOW,{'item':[]},digest([]))
    good=list(normalize(e,empty));assert len(good)==1 and good[0].failure is None


def observation(identity,data,n=0,base=None,requests=None):
    return dict(id=identity,dataset='stock_daily',subject='TEST.SH',variant='default',observed_at=NOW+timedelta(seconds=n),
                content_hash=digest(data),data_json=native_json(data),request_json=native_json(requests or []),base_observation_id=base)


def test_native_delta_hash_dependency_and_actual_equal_value_confirmation():
    data=daily([price()]).content
    first=observation('a',data)
    second=observation('b',data,3,'a',[{'interface':'test','parameters':{},**returned_keys({'item':[price()]})}])
    second['data_json']=native_json({'key_field':'date_ms','removed':[],'upserts':[],
                                   'metadata':{k:v for k,v in data.items() if k!='item'}})
    restored,basis,field=materialize_observation(second,{'a':first}.get)
    assert restored==data and field=='date_ms'
    assert basis[str(price()['date_ms'])][0]>0
    second['content_hash']='0'*64
    with pytest.raises(NativeInputError):materialize_observation(second,{'a':first}.get)


def test_sparse_reanchor_retains_excluded_key_and_restricts_unknown_reconfirmation():
    data=daily([price()]).content;t=EffectiveBasis();first=observation('a',data)
    _,b,k=materialize_observation(first,lambda _:None)
    t.apply(first,data,b,k,[])
    second=observation('b',data,3)
    _,b,k=materialize_observation(second,lambda _:None)
    _,uncertain=t.apply(second,data,b,k,[])
    assert uncertain==(str(price()['date_ms']),)
    third=observation('c',data,4,requests=[{'parameters':{'start':1743436800000,'end':1743523200000}}])
    _,b,k=materialize_observation(third,lambda _:None)
    _,uncertain=t.apply(third,data,b,k,json.loads(third['request_json']))
    assert uncertain==()


def test_rescue_original_anchor_read_without_old_kernel(tmp_path):
    row=observation('a',daily([price()]).content)
    path=tmp_path/'source.jsonl'
    path.write_text(native_json({'format':'qf-local-rescue-v1','kind':'ths_observation','entry_id':'E50','record':row})+'\n')
    source=RescueSources([path]);records=list(source.iter_entry(BY_ID['E50']))
    assert len(records)==1 and source.summary['complete']
    assert all(not u.failure for u in normalize(BY_ID['E50'],records[0]))


def test_sparse_actual_empty_receipt_preserves_unreturned_confirmation():
    data=daily([price()]).content; tracker=EffectiveBasis();first=observation('a',data)
    _,basis,field=materialize_observation(first,lambda _:None)
    basis,_=tracker.apply(first,data,basis,field,[]);old=basis.copy()
    requests=[{'parameters':{},**returned_keys({'item':[]})}]
    second=observation('b',data,3,requests=requests)
    _,basis,field=materialize_observation(second,lambda _:None)
    basis,uncertain=tracker.apply(second,data,basis,field,requests)
    assert not uncertain and basis==old


def test_import_receipt_keeps_inherited_groups_and_original_acquisition_order():
    firstdata={'item':[{'ex_date_ms':1743350400000,'event':'a'},
                        {'ex_date_ms':1743350400000,'event':'b'}]}
    def obs(identity,data,seconds,requests):
        row=observation(identity,data,seconds,requests=requests);row['dataset']='stock_actions';return row
    tracker=EffectiveBasis();first=obs('a',firstdata,1,[])
    _,basis,field=materialize_observation(first,lambda _:None)
    basis,_=tracker.apply(first,firstdata,basis,field,[]);old=basis.copy()
    seconddata={'item':firstdata['item']+[{'ex_date_ms':1743436800000,'event':'c'}]}
    request={'parameters':{},'artifact_sha256':'a'*64,
             'source_observed_at':(NOW+timedelta(seconds=2)).isoformat(),
             **returned_keys({'item':[{'ex_date_ms':1743436800000}]})}
    second=obs('b',seconddata,5,[request]);_,basis,field=materialize_observation(second,lambda _:None)
    basis,uncertain=tracker.apply(second,seconddata,basis,field,[request])
    assert not uncertain and basis['1743350400000']==old['1743350400000']
    from app.data_store.adapters.contracts import instant_ns
    assert basis['1743436800000'][0]==instant_ns(NOW+timedelta(seconds=2))
