"""Transactional outbox captures backdated corrections without full copies."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import select, text, update, delete
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session
from app.data_foundation.update_models import TableChange
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.models.trading_calendar import TradingCalendarDay


def bar(code, source='tushare'):
    return EtfDailyBar(source=source,ts_code=code,trade_date=date(2001,1,2),
        open=Decimal('12.123456'),high=13,low=12,close=13,vol=Decimal('900.1234'),amount=100)


def test_exact_old_new_backdated_values_and_touch_suppression(session):
    code=uuid4().hex[:12]
    row=bar(code);session.add(row);session.flush()
    changes=list(session.scalars(select(TableChange).where(TableChange.dataset=='etf_daily')))
    assert len(changes)==1 and changes[0].before_json is None
    original=json.loads(changes[0].after_json,parse_float=Decimal)
    assert original['open']==Decimal('12.123456')
    assert original['vol']==Decimal('900.1234')
    row.updated_at=datetime.now(timezone.utc);session.flush()
    assert len(list(session.scalars(select(TableChange))))==1
    row.close=Decimal('12.999999');session.flush()
    changed=list(session.scalars(select(TableChange).order_by(TableChange.id)))[1]
    assert json.loads(changed.before_json,parse_float=Decimal)['close']==Decimal('13')
    assert json.loads(changed.after_json,parse_float=Decimal)['close']==Decimal('12.999999')
    assert json.loads(changed.key_json)['trade_date']=='2001-01-02'
    session.delete(row);session.flush()
    removed=list(session.scalars(select(TableChange).order_by(TableChange.id)))[2]
    assert removed.after_json is None and removed.before_json is not None


def test_provider_filter_and_transaction_rollback(session):
    session.add(bar(uuid4().hex[:12],source='other'));session.flush()
    assert not list(session.scalars(select(TableChange)))
    with session.begin_nested() as transaction:
        session.add(bar(uuid4().hex[:12]));session.flush()
        assert len(list(session.scalars(select(TableChange))))==1
        transaction.rollback()
    assert not list(session.scalars(select(TableChange)))


def test_journal_is_immutable_and_calendar_and_factor_channels_are_captured(session):
    session.add(TradingCalendarDay(exchange='TEST',calendar_date=date(2001,1,2),is_open=True))
    session.add(EtfAdjustmentFactor(source='tushare',ts_code='TEST.SH',trade_date=date(2001,1,2),adj_factor=Decimal('1.123456789012')))
    session.flush()
    rows=list(session.scalars(select(TableChange)))
    assert {row.dataset for row in rows}=={'exchange_calendar','etf_adjustment_factors'}
    for statement in [update(TableChange).values(key_json='{}'),delete(TableChange)]:
        with pytest.raises(DBAPIError),session.begin_nested():session.execute(statement)



def test_every_source_table_has_capture_and_truncate_protection(session):
    from app.data_foundation.table_intake import TABLE_SOURCES
    tables = {spec.model.__table__.name for spec in TABLE_SOURCES}
    triggers = session.execute(text("SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE t.tgname IN ('foundation_capture_change','foundation_source_no_truncate')")).all()
    assert {table for table, name in triggers if name == 'foundation_capture_change'} == tables
    assert {table for table, name in triggers if name == 'foundation_source_no_truncate'} == tables
    for table in ('etf_daily_bars', 'foundation_table_changes'):
        with pytest.raises(DBAPIError),session.begin_nested():
            session.execute(text(f'TRUNCATE {table} CASCADE'))


def test_provider_transition_is_a_removal_or_arrival_not_a_foreign_payload(session):
    code = uuid4().hex[:12]
    row = bar(code, source='other'); session.add(row); session.flush()
    row.source = 'tushare'; session.flush()
    first = session.scalar(select(TableChange))
    assert first.before_json is None and json.loads(first.after_json)['source'] == 'tushare'
    row.source = 'other'; session.flush()
    second = list(session.scalars(select(TableChange).order_by(TableChange.id)))[1]
    assert second.after_json is None and json.loads(second.before_json)['source'] == 'tushare'


def test_primary_key_move_captures_old_removal_and_new_arrival(session):
    row = bar(uuid4().hex[:12])
    session.add(row)
    session.flush()
    row.trade_date = date(2001, 1, 3)
    session.flush()
    changes = list(session.scalars(select(TableChange).order_by(TableChange.id)))
    assert len(changes) == 3
    assert json.loads(changes[1].key_json)['trade_date'] == '2001-01-02'
    assert changes[1].before_json is not None and changes[1].after_json is None
    assert json.loads(changes[2].key_json)['trade_date'] == '2001-01-03'
    assert changes[2].before_json is None and changes[2].after_json is not None

def test_late_commit_with_smaller_id_remains_in_journal(pipeline_engine):
    # A committed-ID watermark would lose the first writer. Consumers must
    # discover by receipt absence, never by id > highest previously seen id.
    with Session(pipeline_engine) as first,Session(pipeline_engine) as second:
        first.add(bar('LATECOMMIT'));first.flush()
        low=first.scalar(select(TableChange.id))
        second.add(bar('EARLYCOMMIT'));second.commit()
        with Session(pipeline_engine) as reader:
            visible=set(reader.scalars(select(TableChange.id)))
            assert low not in visible and visible and min(visible)>low
        first.commit()
        with Session(pipeline_engine) as reader:
            assert low in set(reader.scalars(select(TableChange.id)))
