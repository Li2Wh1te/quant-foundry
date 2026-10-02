from datetime import date,datetime,timezone
from decimal import Decimal
import ast
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import inspect

from app.data_store.catalog import DDL
from app.data_store.errors import DataStoreError
from app.data_store.schema import DatasetSpec, prefix_end, _ordered


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


@pytest.mark.parametrize('member_type,member',[
    (pa.string(), 'm\x00中'), (pa.int64(), -1), (pa.uint64(), 2**64-1),
    (pa.date32(), date(1960,1,1)), (pa.bool_(), True),
    (pa.timestamp('us','UTC'), datetime(1960,1,1,tzinfo=timezone.utc)),
    (pa.decimal128(38,8), Decimal('-0.00000001')),
])
def test_validated_full_key_retains_exact_object_prefix_and_contract(member_type,member):
    spec=DatasetSpec('objects',pa.schema([pa.field('source',pa.string(),False),
        pa.field('subject',pa.string(),False),pa.field('object',pa.string(),False),
        pa.field('member',member_type,False)]),('source','subject','object','member'),'r1')
    descriptor=spec.descriptor()
    prefix=('s\x00', '中', 'o\x00tail')
    # Retain the pre-cache expression as a byte oracle. Both a full key and
    # its partial prefix must keep the persisted order-preserving encoding.
    old=b''.join(_ordered(value,spec.schema.field(name).type)
                 for name,value in zip(spec.key,(*prefix,member)))
    encoded=spec.key_bytes((*prefix,member))
    assert encoded==old
    assert encoded[:-len(_ordered(member,member_type))]==spec.key_bytes(prefix)
    assert spec.descriptor()==descriptor
    restored=DatasetSpec.from_descriptor(descriptor)
    assert restored.key_bytes((*prefix,member))==encoded
    with pytest.raises(DataStoreError):
        restored.key_bytes((*prefix,None))


def test_validated_string_member_prefix_handles_empty_nul_and_unicode_components():
    from itertools import product
    from app.data_store.verify_coverage import _string_member_prefix

    fields=('source','subject','object','member')
    spec=DatasetSpec('strings',pa.schema([pa.field(k,pa.string(),False) for k in fields]),fields,'r1')
    # Exercise terminator adjacency, escaped NUL pairs and multibyte UTF-8;
    # find the three-field prefix only from the validated four-field bytes.
    values=('', '\x00', '\x00\x00', '中', 'a\x00中')
    for components in product(values,repeat=4):
        encoded=spec.key_bytes(components)
        assert _string_member_prefix(encoded)==spec.key_bytes(components[:3])


def test_migration_is_self_contained_and_matches_current_ddl():
    path=Path(__file__).parents[1]/'app/db/migrations/versions/20261005_01_current_data_store.py'
    tree=ast.parse(path.read_text())
    ddl=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
             and any(isinstance(t,ast.Name) and t.id=='DDL' for t in n.targets))
    assert ddl==DDL
    assert 'from app.' not in path.read_text()
    from app.data_store.tables import metadata
    # D01's frozen DDL still has six tables. Later additive revisions add
    # entry status and derived physical issue totals without rewriting D01.
    assert len(metadata.tables)==8
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
