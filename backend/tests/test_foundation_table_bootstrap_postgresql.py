"""Fixed-to-current comparison writes only differences and survives retries."""
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, fixture
from tests.test_foundation_table_changes_postgresql import bar
from app.data_foundation.__main__ import local_execution
from app.data_foundation.table_intake import capture_tables, TABLE_SOURCES
from app.data_foundation.table_bootstrap import bootstrap_table, bootstrap_all
from app.data_foundation.update_models import TableChange
from app.data_ingestion.models.etf_daily import EtfDailyBar


def test_fixed_snapshot_diff_keeps_only_insert_correction_and_removal(pipeline_engine,tmp_path):
    spec=next(item for item in TABLE_SOURCES if item.dataset=='etf_daily')
    original=['BASEA','BASEB','BASEC']
    with Session(pipeline_engine) as session,session.begin():
        for code in original:session.add(bar(code))
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection,connection.begin():
        with Session(bind=connection) as session:
            execution=local_execution(session,'a'*40)
            capture,manifest=capture_tables(session,event_key=uuid4().hex,decoder_id=execution.id,sources=(spec,),chunk_size=2)
            capture_id,execution_id=capture.id,execution.id
    with Session(pipeline_engine) as session,session.begin():
        session.get(EtfDailyBar,('tushare','BASEB',date(2001,1,2))).close=Decimal('12.123456')
        session.delete(session.get(EtfDailyBar,('tushare','BASEC',date(2001,1,2))))
        session.add(bar('NEW01'))
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection,connection.begin():
        with Session(bind=connection) as session:
            receipt,result=bootstrap_table(session,capture_ref_id=capture_id,native_dataset='etf_daily',
                execution_id=execution_id,archive_root=tmp_path,minimum_free_bytes=0)
            assert (result['fixed_original_rows'],result['snapshot_current_rows'])==(3,3)
            assert (result['unchanged'],result['inserted'],result['changed'],result['deleted'])==(1,1,1,1)
            seed=list(session.scalars(select(TableChange).where(TableChange.event_key.is_not(None))))
            assert len(seed)==3
            assert sum(row.before_json is None for row in seed)==1
            assert sum(row.after_json is None for row in seed)==1
            assert any('12.123456' in row.after_json for row in seed if row.after_json)
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection,connection.begin():
        with Session(bind=connection) as session:
            again,confirmed=bootstrap_table(session,capture_ref_id=capture_id,native_dataset='etf_daily',
                execution_id=execution_id,archive_root=tmp_path,minimum_free_bytes=0)
            assert again.id==receipt.id and confirmed==result
            assert session.scalar(select(TableChange).where(TableChange.event_key.is_not(None)).limit(1)) is not None


def test_provider_move_out_is_a_deletion_in_bootstrap(pipeline_engine,tmp_path):
    spec=next(item for item in TABLE_SOURCES if item.dataset=='etf_daily')
    with Session(pipeline_engine) as session,session.begin():session.add(bar('MOVED'))
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection,connection.begin():
        with Session(bind=connection) as session:
            execution=local_execution(session,'a'*40)
            capture,_=capture_tables(session,event_key=uuid4().hex,decoder_id=execution.id,sources=(spec,))
            capture_id,execution_id=capture.id,execution.id
    with Session(pipeline_engine) as session,session.begin():
        session.get(EtfDailyBar,('tushare','MOVED',date(2001,1,2))).source='other'
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection,connection.begin():
        with Session(bind=connection) as session:
            _,result=bootstrap_table(session,capture_ref_id=capture_id,native_dataset='etf_daily',
                execution_id=execution_id,archive_root=tmp_path,minimum_free_bytes=0)
            assert result['deleted']==1
            assert result['snapshot_current_rows']==result['fixed_original_rows']-1


def test_single_submission_handles_every_selected_table_and_reuses_receipts(pipeline_engine,tmp_path):
    with Session(pipeline_engine) as session:
        (_, _, execution), image = fixture(session, tmp_path)
    selected = tuple(item for item in TABLE_SOURCES if item.dataset in ('etf_daily','etf_adjustment_factors'))
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection, connection.begin():
        with Session(bind=connection) as session:
            capture, _ = capture_tables(session,event_key=uuid4().hex,decoder_id=execution,sources=selected)
            capture_id = capture.id
    opts = dict(capture_ref_id=capture_id,execution_id=execution,runtime_digest=image,
                archive_root=tmp_path,datasets=[item.dataset for item in selected],minimum_free_bytes=0)
    first=bootstrap_all(pipeline_engine,**opts)
    assert first['status']=='completed',first
    assert len(first['tables'])==2
    second=bootstrap_all(pipeline_engine,**opts)
    assert second==first
