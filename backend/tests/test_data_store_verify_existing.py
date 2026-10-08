"""Existing current verification never rewrites output or fakes a receipt."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import text
from app.data_store.errors import DataStoreError
from tests.test_data_store_kernel import database,limits,store
from tests.test_data_store_local_pipeline import ready,Inputs,company
from tests.test_data_store_domain_samples import sample
from app.data_store.adapters.registry import BY_ID
from app.data_store.pipeline import run_entry,read_entry_status,_status
from app.data_store.verify_coverage import verify_existing
from app.data_store.coverage import check_coverage


def files(st,e):
    with st.catalog.transaction() as c:
        return c.execute(text('SELECT path,content_hash FROM data_store_files WHERE dataset=:d ORDER BY path'),{'d':e.spec.name}).all()


def test_actual_native_match_restores_missing_receipt_without_rebuild(ready):
    e=BY_ID['E41'];source=Inputs(company())
    run_entry(ready,e,source)
    status=read_entry_status(ready,e.id);status.pop('full_coverage');_status(ready,e,status)
    before=files(ready,e);generation=ready.catalog.dataset(e.spec.name)['generation']
    result=verify_existing(ready,e,source)
    assert result['complete'] and result['current_objects']==1
    phases=result['phase_seconds']
    assert {'setup','issues','capture_ranges','native_snapshot','current_files',
            'missing_objects','final_fences','receipt'} <= phases.keys()
    assert all(seconds>=0 for seconds in phases.values())
    assert abs(sum(phases.values())-result['seconds'])<.01
    assert files(ready,e)==before and ready.catalog.dataset(e.spec.name)['generation']==generation
    with ready.catalog.transaction() as c:assert check_coverage(c,e,read_entry_status(ready,e.id))['satisfied']


def test_new_source_value_or_missing_key_cannot_gain_coverage(ready):
    e=BY_ID['E41'];run_entry(ready,e,Inputs(company()))
    before=files(ready,e);old=read_entry_status(ready,e.id)['full_coverage']
    result=verify_existing(ready,e,Inputs(company('changed')))
    assert not result['complete'] and result['mismatched_objects']==1
    assert read_entry_status(ready,e.id)['full_coverage']==old
    with ready.catalog.transaction() as c:assert not check_coverage(c,e,read_entry_status(ready,e.id))['satisfied']
    result=verify_existing(ready,e,Inputs(company(),company(subject='C2')))
    assert not result['complete'] and result['missing_objects']==1
    assert files(ready,e)==before


def test_exact_disposed_failure_is_coverage_but_remains_unqualified(ready):
    e=BY_ID['E41'];source=Inputs(company(failure='SOURCE_REFRESH_FAILED'))
    run_entry(ready,e,source)
    result=verify_existing(ready,e,source)
    assert result['complete'] and result['disposed_failures']==1
    with ready.catalog.transaction() as c:assert check_coverage(c,e,read_entry_status(ready,e.id))['reason']=='CURRENT_QUALITY_UNRESOLVED'


@pytest.mark.parametrize('entry_id', ['E04', 'E50'])
def test_scope_failure_without_output_is_disposed_but_remains_restricted(ready, entry_id):
    entry = BY_ID[entry_id]
    raw = replace(sample(entry_id), content={}, failure='SOURCE_REFRESH_FAILED')
    source = Inputs(raw)
    run_entry(ready, entry, source)
    before = ready.catalog.dataset(entry.spec.name)
    issues = ready.catalog.issues(entry.spec.name)

    result = verify_existing(ready, entry, source)

    assert result['complete'] and result['disposed_failures'] == 1
    assert result['current_objects'] == 0 and result['missing_objects'] == 0
    assert ready.catalog.dataset(entry.spec.name) == before
    assert ready.catalog.issues(entry.spec.name) == issues
    with ready.catalog.transaction() as connection:
        assert check_coverage(connection, entry, read_entry_status(ready, entry.id))[
            'reason'] == 'CURRENT_QUALITY_UNRESOLVED'


@pytest.mark.parametrize('mismatch', ['group', 'older_order', 'other_subject', 'no_issue'])
def test_missing_failure_requires_matching_current_scope_evidence(ready, mismatch):
    entry = BY_ID['E50']
    raw = replace(sample(entry.id), content={}, failure='SOURCE_REFRESH_FAILED')
    source = Inputs(raw)
    run_entry(ready, entry, source)
    status = read_entry_status(ready, entry.id)
    status.pop('full_coverage', None)
    _status(ready, entry, status)
    with ready.catalog.engine.begin() as connection:
        if mismatch == 'no_issue':
            # This deletion is confined to the disposable fixture schema.
            connection.execute(text('DELETE FROM data_store_issues WHERE dataset=:d'),
                               {'d': entry.spec.name})
        else:
            target = json.loads(connection.execute(text(
                'SELECT target_json FROM data_store_issues WHERE dataset=:d'),
                {'d': entry.spec.name}).scalar_one())
            if mismatch == 'group':
                target['group'] = '0' * 64
            elif mismatch == 'older_order':
                target['order'] = str(raw.order_ns - 1)
            else:
                target['prefix'][1] = 'OTHER-FICTION.SH'
            connection.execute(text('UPDATE data_store_issues SET target_json=:j WHERE dataset=:d'),
                               {'j': json.dumps(target), 'd': entry.spec.name})

    result = verify_existing(ready, entry, source)

    assert not result['complete'] and result['missing_objects'] == 1
    assert result['disposed_failures'] == 0
    after = read_entry_status(ready, entry.id)
    assert 'full_coverage' not in after
    assert after['coverage_pending']


@pytest.mark.parametrize('same_size',[False,True])
def test_corrupt_preserved_file_cannot_gain_coverage(ready,same_size):
    e=BY_ID['E41'];source=Inputs(company());run_entry(ready,e,source)
    status=read_entry_status(ready,e.id);status.pop('full_coverage');_status(ready,e,status)
    with ready.catalog.transaction() as c:
        relative=c.execute(text('SELECT path FROM data_store_files WHERE dataset=:d'),{'d':e.spec.name}).scalar_one()
    path=ready.files.root/relative
    body=path.read_bytes()
    path.write_bytes(bytes([body[0]^1])+body[1:] if same_size else body+b'x')
    with pytest.raises(DataStoreError) as error:
        verify_existing(ready,e,source)
    assert error.value.code=='FILE_INVALID'
    after=read_entry_status(ready,e.id)
    assert 'full_coverage' not in after and after['coverage_pending']


def test_whole_report_across_arrow_batches_and_files_keeps_exact_hash(ready):
    from decimal import Decimal
    from tests.test_data_store_domain_samples import ths, NEXT_DAY_MS
    from app.data_store.adapters.normalize import normalize

    # One complete report spans both the 1024-row Arrow batch and a physical
    # file boundary. Members cannot become separately confirmed objects.
    entry=BY_ID['E07']
    source=ths(entry.id,'FUND.SH',{'item':[
        {'thscode':f'{i:06d}.SZ','stock_name':'持仓','end_date_ms':NEXT_DAY_MS,
         'hold_ratio':Decimal('1.2'),'investment_rank':i+1} for i in range(2050)]})
    larger=replace(ready.limits,file_rows=1600)
    ready.limits=larger;ready.budget.limits=larger
    built=run_entry(ready,entry,Inputs(source))
    assert built['complete'] and built['qualified']
    assert len(files(ready,entry))>=2
    with ready.catalog.transaction() as c:
        assert c.execute(text('SELECT max(row_count) FROM data_store_files WHERE dataset=:d'),
                         {'d':entry.spec.name}).scalar_one()>1024
    generation=ready.catalog.dataset(entry.spec.name)['generation']
    before=files(ready,entry)
    result=verify_existing(ready,entry,Inputs(source))
    assert result['complete'] and result['current_objects']==1
    assert files(ready,entry)==before
    assert ready.catalog.dataset(entry.spec.name)['generation']==generation
    assert not ready.budget.pending_keys()
    assert len(next(normalize(entry,source)).rows)>1024

    changed=replace(source,content={'item':[{**row,'hold_ratio':Decimal('1.3')}
                                         for row in source.content['item']]})
    mismatch=verify_existing(ready,entry,Inputs(changed))
    assert not mismatch['complete'] and mismatch['mismatched_objects']==1
    assert files(ready,entry)==before
