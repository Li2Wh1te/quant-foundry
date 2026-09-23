"""Fixed change refs retain row identity and fence current source state."""
from datetime import date
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine
from tests.test_foundation_table_changes_postgresql import bar
from app.data_foundation.__main__ import local_execution
from app.data_foundation.table_updates import freeze_change, is_current_change
from app.data_foundation.update_models import TableChange
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_foundation.source_refs import read_source
from app.data_foundation.models import SourceRef


def test_fixed_change_and_current_guard_handles_correction_and_deletion(pipeline_engine):
    code=uuid4().hex[:12]
    with Session(pipeline_engine) as session,session.begin():
        execution=local_execution(session,'a'*40).id
        session.add(bar(code))
    with Session(pipeline_engine) as session,session.begin():
        first=session.scalar(select(TableChange).where(TableChange.key_json.contains(code)))
        ref=freeze_change(session,change_id=first.id,execution_id=execution)
        assert read_source(session,ref.id)[0]['ts_code']==code
        assert is_current_change(session,ref,first.id)
        first_id,ref_id=first.id,ref.id
    with Session(pipeline_engine) as session,session.begin():
        session.get(EtfDailyBar,('tushare',code,date(2001,1,2))).close=Decimal('12.999999')
    with Session(pipeline_engine) as session,session.begin():
        assert not is_current_change(session,session.get(SourceRef,ref_id),first_id)
        correction=session.scalar(select(TableChange).where(TableChange.id>first_id,TableChange.key_json.contains(code)))
        corrected=freeze_change(session,change_id=correction.id,execution_id=execution)
        assert is_current_change(session,corrected,correction.id)
        correction_id,corrected_id=correction.id,corrected.id
    with Session(pipeline_engine) as session,session.begin():
        session.delete(session.get(EtfDailyBar,('tushare',code,date(2001,1,2))))
    with Session(pipeline_engine) as session,session.begin():
        deleted=session.scalar(select(TableChange).where(TableChange.id>correction_id,TableChange.key_json.contains(code)))
        removal=freeze_change(session,change_id=deleted.id,execution_id=execution)
        assert read_source(session,removal.id)[0]['ts_code']==code
        assert is_current_change(session,removal,deleted.id)
        assert not is_current_change(session,session.get(SourceRef,corrected_id),correction_id)
