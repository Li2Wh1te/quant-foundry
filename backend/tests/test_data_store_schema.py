from datetime import date,datetime,timezone
from decimal import Decimal
import ast
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import inspect

from app.data_store.catalog import DDL
from app.data_store.errors import DataStoreError
from app.data_store.schema import DatasetSpec, prefix_end


@pytest.mark.parametrize('dtype,values',[
    (pa.int64(),[-2**63,-100,-1,0,1,100,2**63-1]),
    (pa.uint64(),[0,1,2**64-1]),
    (pa.string(),['','\x00','\x00x','a','a\x00','aa','b','中']),
    (pa.date32(),[date(1960,1,1),date(1970,1,1),date(2026,9,25)]),
    (pa.bool_(),[False,True]),
    (pa.timestamp('us','UTC'),[datetime(1960,1,1,tzinfo=timezone.utc),datetime(2026,1,1,tzinfo=timezone.utc)]),
    (pa.decimal128(38,8),[Decimal('-1e25'),Decimal('-0.00000001'),Decimal(0),Decimal('0.00000001'),Decimal('1e25')]),
])
def test_ordered_keys_match_business_order(dtype,values):
    s=DatasetSpec('test',pa.schema([pa.field('key',dtype,False)]),('key',),'r1')
    keys=[s.key_bytes((v,)) for v in values]
    assert keys==sorted(keys) and len(set(keys))==len(keys)


def test_prefix_is_an_exact_report_interval():
    s=DatasetSpec('reports',pa.schema([pa.field('report',pa.string(),False),pa.field('member',pa.int64(),False)]),
                  ('report','member'),'r1',report_prefix=1)
    prefix=s.key_bytes(('a',));end=prefix_end(prefix)
    for member in [-2**63,0,2**63-1]:
        assert prefix <= s.key_bytes(('a',member)) < end
    assert s.key_bytes(('aa',0))>=end


def test_migration_is_self_contained_and_matches_current_ddl():
    path=Path(__file__).parents[1]/'app/db/migrations/versions/20261005_01_current_data_store.py'
    tree=ast.parse(path.read_text())
    ddl=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
             and any(isinstance(t,ast.Name) and t.id=='DDL' for t in n.targets))
    assert ddl==DDL
    assert 'from app.' not in path.read_text()
    from app.data_store.tables import metadata
    # D01's frozen DDL still has six tables. D02 adds one independent current
    # operational table in its own migration, not a rewrite of D01's history.
    assert len(metadata.tables)==7
    assert 'data_store_entry_status' in metadata.tables
    assert all(not foreign.column.table.name.startswith('foundation_')
               for table in metadata.tables.values() for foreign in table.foreign_keys)


def test_semantics_partition_rule_and_required_fields_never_implicitly_compatible():
    from dataclasses import replace
    s=DatasetSpec('bars',pa.schema([pa.field('id',pa.int64(),False)]),('id',),'r1',{'unit':'CNY'})
    assert not replace(s,semantics={'unit':'USD'}).accepts(s)
    assert not replace(s,rule='r2').accepts(s)
    assert not replace(s,partitioning='new').accepts(s)
    assert not replace(s,schema=s.schema.append(pa.field('required',pa.string(),False))).accepts(s)
    assert replace(s,schema=s.schema.append(pa.field('optional',pa.string(),True))).accepts(s)


def test_variable_width_materialization_has_a_declared_byte_bound():
    text = pa.field('text',pa.string(),False)
    spec = DatasetSpec('wide',pa.schema([pa.field('id',pa.int64(),False),text]),('id',),'r1')
    assert spec.bounded_rows(65536,65536) == 1
    tight = DatasetSpec('tight',pa.schema([pa.field('id',pa.int64(),False),
        pa.field('text',pa.string(),False,{b'max_utf8_bytes':b'8'})]),('id',),'r1')
    assert 1 < tight.bounded_rows(65536,65536) < 65536
    with pytest.raises(DataStoreError):
        tight.validate_batch(pa.RecordBatch.from_pydict({'id':[1],'text':['too many characters']},schema=tight.schema),
                             max_rows=10,max_bytes=65536)
    with pytest.raises(DataStoreError):
        DatasetSpec('invalid',pa.schema([pa.field('id',pa.string(),False,{b'max_utf8_bytes':b'0'})]),('id',),'r1')
