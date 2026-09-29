"""Closeout regressions use synthetic local sources and isolated PostgreSQL."""
from dataclasses import replace
import sqlite3
import pytest
from sqlalchemy import text
from tests.test_data_store_kernel import database,limits,store,spec,KEY
from tests.test_data_store_local_pipeline import ready,Inputs,company
from app.data_store.pipeline import run_entry,read_entry_status
from app.data_store.adapters.registry import BY_ID
from app.data_store.errors import DataStoreError
from app.data_store.catalog import Issue
from app.data_store.merge import MergeSpool


@pytest.mark.parametrize('code',['QUERY_TIMEOUT','OPERATION_CANCELLED','MEMORY_PRESSURE'])
def test_sqlite_interrupt_preserves_guard_reason(ready,monkeypatch,code):
    def interrupt(self,unit,**kwargs):
        self._interrupt=DataStoreError(code)
        raise sqlite3.OperationalError('interrupted')
    monkeypatch.setattr(MergeSpool,'offer',interrupt)
    with pytest.raises(DataStoreError) as error:run_entry(ready,BY_ID['E41'],Inputs(company()))
    assert error.value.code==code
    assert read_entry_status(ready,'E41')['reason']==code
    assert ready.budget.pending_keys()==set()


def test_large_issue_delta_is_atomic_and_resolutions_match_tokens(store):
    from app.data_store.catalog import SourceUpdate
    from app.data_store.schema import fingerprint
    st=spec();store.register(st)
    issues=tuple(Issue('problem.'+str(i),'scope','CORE_VALUE_INVALID','a'*64,{}, {}) for i in range(1100))
    update=SourceUpdate('scope','a'*64,'b'*64,0,qualified=False)
    # No rows are needed to establish exact current restrictions. More than one
    # SQL batch must still be committed with a single source checkpoint.
    result=store.replace_partition(st,'default',[],update,complete=True,source_check=lambda s:s is None,issues=issues)
    assert result.generation==1
    with store.catalog.transaction() as c:assert c.execute(text('SELECT count(*) FROM data_store_issues')).scalar_one()==1100
    before=store.catalog.dataset(st.name)['generation']
    # Force the GLOBAL cap in a later batch; earlier mutations/checkpoints must
    # not leak from a failed atomic commit.
    from app.data_store.catalog import Catalog
    original=store.catalog.limits
    store.catalog.limits=replace(original,issue_count=1100)
    more=tuple(Issue('new.'+str(i),'scope','CORE_VALUE_INVALID','b'*64,{}, {}) for i in range(300))
    with pytest.raises(DataStoreError,match='当前问题'):
        store.replace_partition(st,'default',[],replace(update,expected_revision=1,input_token='c'*64),
                                complete=True,source_check=lambda s:s['revision']==1,issues=more)
    assert store.catalog.dataset(st.name)['generation']==before
    with store.catalog.transaction() as c:assert c.execute(text('SELECT count(*) FROM data_store_issues')).scalar_one()==1100
    store.catalog.limits=original
    store.replace_partition(st,'default',[],replace(update,expected_revision=1,input_token='d'*64,qualified=True),
        complete=True,source_check=lambda s:s['revision']==1,resolved={i.key:'b'*64 for i in issues})
    with store.catalog.transaction() as c:assert c.execute(text('SELECT count(*) FROM data_store_issues')).scalar_one()==1100


def test_partition_over_one_thousand_problems_commits_without_truncation(ready):
    from app.data_store.pipeline import _partition_issues
    from app.data_store.adapters.contracts import Unit
    e=BY_ID['E41'];ready.register(e.spec)
    # Build one real merge partition with many independently addressable
    # problems; this used to fail in the pipeline before reaching the kernel.
    with ready.budget.reserve('read',pending='pipeline.test') as space:
        spool=MergeSpool(space,e.spec,partition_count=None)
        target=None;n=0
        for i in range(50000):
            u=Unit('C'+str(i),'synthetic-test','current','g',1,'a'*64,(),failure='SOURCE_REFRESH_FAILED')
            part=e.spec.partitioner((*u.key,'root'))
            target=target or part
            if part!=target:continue
            spool.offer(u);n+=1
            if n==1100:break
        spool.db.commit()
        additions,resolved=_partition_issues(ready,e,target,spool)
        assert len(additions)==1100 and not resolved
        spool.close();space.discard=True
