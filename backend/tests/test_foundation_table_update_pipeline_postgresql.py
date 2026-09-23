"""Local table deltas publish current values and withdraw deleted keys."""
import json
from datetime import date
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, fixture, isolated_series
from tests.test_foundation_table_changes_postgresql import bar
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.table_updates import freeze_change
from app.data_foundation.update_models import TableChange
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation import record_work
from app.data_ingestion.models.etf_daily import EtfDailyBar


def run_change(engine, change_id, execution, image, archive):
    with Session(engine) as session,session.begin():
        source=freeze_change(session,change_id=change_id,execution_id=execution)
        batch=register_job(session,source_ref_id=source.id,execution_id=execution,
            runtime_digest=image,archive_root=archive,table_change_id=change_id)
        batch_id=batch.id
    return advance_job(engine,batch_id,runtime_digest=image,archive_root=archive,steps=100,publish=True)


def test_insert_correction_and_deletion_have_independent_formal_releases(pipeline_engine,tmp_path):
    with Session(pipeline_engine) as session:
        (_, _, execution), image=fixture(session,tmp_path)
    code=uuid4().hex[:12]
    with Session(pipeline_engine) as session,session.begin():session.add(bar(code))
    with Session(pipeline_engine) as session:
        inserted=session.scalar(select(TableChange.id).where(TableChange.key_json.contains(code)).order_by(TableChange.id.desc()))
    first=run_change(pipeline_engine,inserted,execution,image,tmp_path)
    assert first['status']=='published' and first['head_activated']
    with Session(pipeline_engine) as session,session.begin():
        session.get(EtfDailyBar,('tushare',code,date(2001,1,2))).close=Decimal('12.999999')
    with Session(pipeline_engine) as session:
        correction=session.scalar(select(TableChange.id).where(TableChange.key_json.contains(code),TableChange.id>inserted).order_by(TableChange.id.desc()))
    second=run_change(pipeline_engine,correction,execution,image,tmp_path)
    assert second['status']=='published' and second['release_id']!=first['release_id']
    with Session(pipeline_engine) as session:
        subject=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key==code))
        request=RecordRequirement(dataset_id='market.fund_daily',
            semantic_series_id=record_work.series_for('market.fund_daily','tushare'),
            subjects=[subject],fields=['close'],business_range={'from':'2001-01-02','to':'2001-01-02'})
        latest=json.loads(service(session).query_official(request))
        assert latest['request_satisfied'] and latest['items'][0]['close']=='12.999999'
    with Session(pipeline_engine) as session,session.begin():
        session.delete(session.get(EtfDailyBar,('tushare',code,date(2001,1,2))))
    with Session(pipeline_engine) as session:
        deletion=session.scalar(select(TableChange.id).where(TableChange.key_json.contains(code),TableChange.id>correction).order_by(TableChange.id.desc()))
    third=run_change(pipeline_engine,deletion,execution,image,tmp_path)
    assert third['status']=='published'
    with Session(pipeline_engine) as session:
        latest=json.loads(service(session).query_official(request))
        assert not latest['request_satisfied'] and not latest['items']
        old=json.loads(service(session).query_official(request.model_copy(update={'release':first['release_id']})))
        assert old['request_satisfied'] and old['items'][0]['close']=='13'
