"""Full local table capture keeps exact numbers and one committed snapshot."""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4, UUID
import os
import pytest
from sqlalchemy import create_engine, Table, Column, MetaData, String, Numeric, select, func, text, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.data_foundation.__main__ import local_execution
from app.data_foundation.table_intake import TableSource, capture_tables, verify_capture
from app.data_foundation.models import Baseline
from app.data_foundation.source_refs import read_source
from app.data_foundation.canonical import FoundationError

pytestmark = pytest.mark.skipif(os.getenv('POSTGRES_TEST_ENABLED') != '1', reason='requires disposable PostgreSQL')


@pytest.fixture
def table_fixture():
    engine = create_engine(get_settings().database_url)
    name = 'capture_fixture_' + uuid4().hex
    table = Table(name, MetaData(), Column('source', String, primary_key=True),
        Column('code', String, primary_key=True), Column('price', Numeric(32,18)), Column('payload', JSONB))
    table.create(engine)
    with engine.begin() as c:
        for i in range(5):
            c.execute(text(f'INSERT INTO "{name}" VALUES (:source,:code,:price,CAST(:payload AS jsonb))'),
                dict(source='tushare',code=str(i),price=Decimal('12.123456789012345678'),payload='{"amount":0.1234567890123456789}'))
        c.execute(table.insert().values(source='other',code='excluded',price=1,payload={}))
    yield engine, table, TableSource('fixture', name, SimpleNamespace(__table__=table))
    table.drop(engine)
    engine.dispose()


def test_atomic_capture_exact_values_chunks_and_idempotent_original_input(table_fixture):
    engine, table, spec = table_fixture
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c, c.begin():
        with Session(bind=c) as s:
            ex = local_execution(s, 'a' * 40)
            ref, doc = capture_tables(s,event_key=uuid4().hex,decoder_id=ex.id,chunk_size=2,sources=(spec,))
            assert [x['rows'] for x in doc['tables'][0]['chunks']] == [2,2,1]
            assert doc['tables'][0]['rows'] == 5
            first = read_source(s, UUID(doc['tables'][0]['chunks'][0]['source_ref_id']))
            assert first[0]['price'] == '12.123456789012345678'
            assert first[0]['payload']['amount'] == '0.1234567890123456789'
            s.execute(table.update().values(price=99))
            same, original = capture_tables(s,event_key=doc['event_key'],decoder_id=ex.id,chunk_size=2,sources=(spec,))
            assert same.id == ref.id and original['tables'][0]['rows'] == 5
            assert verify_capture(s,same)['snapshot'] == doc['snapshot']
        c.rollback()


def test_empty_source_is_explicit_and_capture_failure_leaves_no_partial_baselines(table_fixture):
    engine, table, spec = table_fixture
    key=uuid4().hex
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        with pytest.raises(KeyError), c.begin():
            with Session(bind=c) as s:
                ex=local_execution(s,'a'*40)
                capture_tables(s,event_key=key,decoder_id=ex.id,chunk_size=2,
                    sources=(spec,TableSource('fixture','missing',spec.model,'no_such_column')))
        with c.begin():
            assert c.scalar(select(func.count()).select_from(Baseline).where(Baseline.dataset==spec.dataset)) == 0
        with c.begin(), Session(bind=c) as s:
            ex=local_execution(s,'a'*40)
            s.execute(table.delete().where(table.c.source=='tushare'))
            ref,doc=capture_tables(s,event_key=key,decoder_id=ex.id,sources=(spec,))
            assert doc['tables'][0]['rows']==0 and len(doc['tables'][0]['chunks'])==1
            chunk=doc['tables'][0]['chunks'][0]
            assert read_source(s,UUID(chunk['source_ref_id']))==[]
            assert verify_capture(s,ref)['tables'][0]['rows']==0
            c.rollback()


def test_concurrent_source_change_cannot_leak_between_streamed_chunks(table_fixture):
    engine, table, spec = table_fixture
    changed=[]
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c, c.begin():
        def on_sql(conn,cursor,statement,parameters,context,executemany):
            if not changed and statement.startswith('INSERT INTO foundation_baselines'):
                changed.append(True)
                with engine.begin() as writer:
                    writer.execute(table.update().where(table.c.source=='tushare').values(price=77))
                    writer.execute(table.insert().values(source='tushare',code='late',price=88,payload={}))
        event.listen(c,'before_cursor_execute',on_sql)
        try:
            with Session(bind=c) as s:
                ex=local_execution(s,'a'*40)
                ref,doc=capture_tables(s,event_key=uuid4().hex,decoder_id=ex.id,chunk_size=2,sources=(spec,))
                assert changed and doc['tables'][0]['rows']==5
                rows=[row for chunk in doc['tables'][0]['chunks'] for row in read_source(s,UUID(chunk['source_ref_id']))]
                assert {row['price'] for row in rows}=={'12.123456789012345678'}
        finally:
            event.remove(c,'before_cursor_execute',on_sql)
        c.rollback()


def test_capture_rejects_non_snapshot_transactions(table_fixture):
    engine,table,spec=table_fixture
    with Session(engine) as s, s.begin():
        with pytest.raises(FoundationError,match='可重复读'):
            capture_tables(s,event_key=uuid4().hex,decoder_id=uuid4(),sources=(spec,))


def test_empty_table_receipt_retains_original_capture_after_mutable_table_changes(table_fixture):
    from app.data_foundation.scope_settlement import freeze_empty_tables
    from app.data_foundation.record_adapters import convert
    engine, table, spec = table_fixture
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c, c.begin(), Session(bind=c) as s:
        ex = local_execution(s, 'a'*40)
        s.execute(table.delete().where(table.c.source == 'tushare'))
        ref, doc = capture_tables(s, event_key=uuid4().hex, decoder_id=ex.id, sources=(spec,))
        receipts = freeze_empty_tables(s, capture_ref_id=ref.id, execution_id=ex.id)
        assert len(receipts) == 1
        fixed = receipts[0][1]
        body = convert(fixed, read_source(s, fixed.id)[0])['body']
        assert body['fixed_rows'] == 0 and body['market_absence'] is None
        s.execute(table.insert().values(source='tushare', code='arrived-later', price=99, payload={}))
        assert freeze_empty_tables(s, capture_ref_id=ref.id, execution_id=ex.id)[0][1].id == fixed.id
        next_ref, _ = capture_tables(s, event_key=uuid4().hex, decoder_id=ex.id, sources=(spec,))
        assert freeze_empty_tables(s, capture_ref_id=next_ref.id, execution_id=ex.id) == []
        c.rollback()
