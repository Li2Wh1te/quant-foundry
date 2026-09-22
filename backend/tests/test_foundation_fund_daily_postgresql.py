"""Daily observations exercise the same immutable release and query mechanism."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_records_postgresql import setup_records,release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_fund_daily import bar
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_work import validate_release
from app.data_foundation.work_models import Candidate,BlockRef


def test_daily_published_units_fixed_corrections_and_missing_fields(session):
    first=release_records(session,setup_records(session,[bar()], 'etf_daily'))
    second=release_records(session,setup_records(session,[bar(close='1.7',vol=None)],'etf_daily'),first)
    subject=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='TEST.SH'))
    req=RecordRequirement(dataset_id='market.fund_daily',semantic_series_id='tushare-market.fund_daily-observed-v1',
        subjects=[subject],fields=['close','volume_lots'],business_range={'from':'2026-01-02','to':'2026-01-02'},release=first.id)
    old=json.loads(service(session).query_official(req))
    assert old['items'][0]['close']=='1.5' and old['items'][0]['volume_lots']=='123.4567'
    new=json.loads(service(session).query_official(req.model_copy(update={'release':second.id})))
    assert not new['request_satisfied'] and new['excluded'][0]['reason']=='FIELD_UNAVAILABLE'
    current=json.loads(service(session).query_official(req.model_copy(update={'release':second.id,'fields':['close']})))
    assert current['items'][0]['close']=='1.7'
    assert all(k.startswith('subject:') for k in session.scalars(select(BlockRef.partition_key).where(BlockRef.release_id==second.id)))
    validate_release(session,second)


def test_daily_quarantine_preserves_bad_price_evidence_without_publishing_it(session):
    fixture=setup_records(session,[bar(ts_code='GOOD.SH'),bar(ts_code='BAD.SH',high='0')],'etf_daily')
    release_records(session,fixture)
    states=list(session.scalars(select(Candidate.readiness).where(Candidate.work_id==fixture[0].id)))
    assert sorted(states)==['quarantined','ready']
