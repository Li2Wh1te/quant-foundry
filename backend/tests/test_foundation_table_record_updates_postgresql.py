"""Local journal discovery waits for fixed publication and advances late edits."""
from datetime import date
from decimal import Decimal
from uuid import uuid4
from sqlalchemy.orm import Session
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, fixture, isolated_series
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_table_changes_postgresql import bar
from app.data_foundation.table_intake import capture_tables, TABLE_SOURCES
from app.data_foundation.table_bootstrap import bootstrap_all
from app.data_foundation.table_record_updates import discover, advance_changes
from app.data_foundation.full_formalization import advance_source
from app.data_ingestion.models.etf_daily import EtfDailyBar


def test_updates_wait_for_fixed_business_receipt_then_publish_late_correction(pipeline_engine,tmp_path):
    with Session(pipeline_engine) as session:
        (_, _, execution), image=fixture(session,tmp_path)
    code=uuid4().hex[:12]
    with Session(pipeline_engine) as session,session.begin():session.add(bar(code))
    spec=next(item for item in TABLE_SOURCES if item.dataset=='etf_daily')
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection,connection.begin():
        with Session(bind=connection) as session:
            capture,manifest=capture_tables(session,event_key=uuid4().hex,decoder_id=execution,sources=(spec,))
            capture_id=capture.id
            source_id=manifest['tables'][0]['chunks'][0]['source_ref_id']
    with Session(pipeline_engine) as session:
        assert discover(session,native_dataset='etf_daily',capture_ref_id=capture_id,
                        execution_id=execution)['status']=='waiting_bootstrap'
    bootstrap=bootstrap_all(pipeline_engine,capture_ref_id=capture_id,execution_id=execution,
        runtime_digest=image,archive_root=tmp_path,datasets=['etf_daily'],minimum_free_bytes=0)
    assert bootstrap['status']=='completed',bootstrap
    with Session(pipeline_engine) as session:
        result=discover(session,native_dataset='etf_daily',capture_ref_id=capture_id,execution_id=execution)
        assert result['status']=='waiting_backfill' and result['pending_backfill']==1
    fixed=advance_source(pipeline_engine,source_id,execution_id=execution,runtime_digest=image,
        archive_root=tmp_path,steps=100,publish=True)
    assert fixed['status']=='published'
    with Session(pipeline_engine) as session,session.begin():
        session.get(EtfDailyBar,('tushare',code,date(2001,1,2))).close=Decimal('12.999999')
    with Session(pipeline_engine) as session:
        assert discover(session,native_dataset='etf_daily',capture_ref_id=capture_id,
                        execution_id=execution)['status']=='ready'
    params=dict(native_dataset='etf_daily',capture_ref_id=capture_id,execution_id=execution,
        runtime_digest=image,archive_root=tmp_path,source_limit=4,steps_per_source=10)
    first=advance_changes(pipeline_engine,**params)
    assert first['status']=='advanced' and first['items']
    assert all(item['status']=='published' for item in first['items']),first
    second=advance_changes(pipeline_engine,**params)
    assert second['items']==[]


def test_previously_empty_source_fact_settles_then_publishes_new_local_row(pipeline_engine,tmp_path):
    from datetime import datetime, timezone
    from app.data_foundation.scope_settlement import freeze_empty_tables
    from app.data_ingestion.models.trading_calendar import TradingStatusSourceFact
    from app.data_foundation.record_query import RecordRequirement
    from app.data_foundation.record_models import RecordSubject
    from app.data_foundation import record_work
    from sqlalchemy import select
    import json
    with Session(pipeline_engine) as session:
        (_, _, execution), image=fixture(session,tmp_path)
    native='trading_status_source_facts'
    spec=next(item for item in TABLE_SOURCES if item.dataset==native)
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection,connection.begin():
        with Session(bind=connection) as session:
            capture,_=capture_tables(session,event_key=uuid4().hex,decoder_id=execution,sources=(spec,))
            capture_id=capture.id
    assert bootstrap_all(pipeline_engine,capture_ref_id=capture_id,execution_id=execution,
        runtime_digest=image,archive_root=tmp_path,datasets=[native],minimum_free_bytes=0)['status']=='completed'
    with Session(pipeline_engine) as session,session.begin():
        assert discover(session,native_dataset=native,capture_ref_id=capture_id,
                        execution_id=execution)['status']=='waiting_empty_settlement'
        _,receipt=freeze_empty_tables(session,capture_ref_id=capture_id,execution_id=execution)[0]
        receipt_id=receipt.id
    fixed=advance_source(pipeline_engine,receipt_id,execution_id=execution,runtime_digest=image,
        archive_root=tmp_path,steps=100,publish=True)
    assert fixed['status']=='published'
    row_id=uuid4()
    with Session(pipeline_engine) as session,session.begin():
        session.add(TradingStatusSourceFact(id=row_id,source='tushare',endpoint='suspend_d',
            query_kind='ts_code',query_value='A.SH',payload={'rows':[{'ts_code':'A.SH','status':'S'}]},
            source_hash='a'*64,source_revision='a'*64,observed_at=datetime.now(timezone.utc)))
    params=dict(native_dataset=native,capture_ref_id=capture_id,execution_id=execution,
        runtime_digest=image,archive_root=tmp_path,source_limit=4,steps_per_source=10)
    actual=advance_changes(pipeline_engine,**params)
    assert actual['status']=='advanced' and len(actual['items'])==1,actual
    assert actual['items'][0]['status']=='published',actual
    with Session(pipeline_engine) as session:
        subject=session.scalar(select(RecordSubject.id).where(RecordSubject.kind=='source_row:'+native))
        req=RecordRequirement(dataset_id='operations.trading_status_source_fact',
            semantic_series_id=record_work.series_for('operations.trading_status_source_fact','tushare'),
            subjects=[subject],fields=['reported_text','reported_nested_hashes'])
        result=json.loads(service(session).query_official(req))
        assert result['request_satisfied'],result
        assert result['items'][0]['reported_text']['source']=='tushare'
        assert len(result['items'][0]['reported_nested_hashes']['payload'])==64
