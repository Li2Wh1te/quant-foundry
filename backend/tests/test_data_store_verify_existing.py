"""Existing current verification never rewrites output or fakes a receipt."""
import pytest
from sqlalchemy import text
from app.data_store.errors import DataStoreError
from tests.test_data_store_kernel import database,limits,store
from tests.test_data_store_local_pipeline import ready,Inputs,company
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
