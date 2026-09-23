"""New observations replace prior snapshots while old releases remain readable."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_quote_snapshots import quote
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Candidate


@pytest.mark.parametrize('native', ['stock_quote','etf_quote','index_quote','stock_valuation'])
def test_snapshot_history_replacement_and_unverified_semantics(session,native):
    valuation=native=='stock_valuation';domain=f'market.{native}_snapshot'
    field='reported_pe_ttm' if valuation else 'reported_last_price'
    row=lambda value:dict(thscode='A.SH',pe_ttm=value) if valuation else quote(last_price=value)
    create=lambda value,stamp:setup(session,[row(value)],native,'A.SH',timestamp=stamp)
    first=release_records(session,create('10',1790005915123))
    second=release_records(session,create('12',1790092315123),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='A.SH'))
    req=RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',
        subjects=[ident],fields=['reported_at',field])
    for release,value in [(first,'10'),(second,'12')]:
        result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert result['request_satisfied'] and len(result['items'])==1
        assert result['items'][0][field]==value
    unavailable=['financial_period','valuation_formula'] if valuation else ['currency','volume_unit','executable_quote','trading_status']
    for changes in [dict(fields=[field]) for field in unavailable+['timestamp_semantics','effective_business_date']]+[dict(time_mode='strict_public_pit')]:
        denied=json.loads(service(session).query_official(req.model_copy(update=changes)))
        assert not denied['request_satisfied'] and not denied['items']


def test_subject_conflict_quarantines_without_a_publishable_value(session):
    work,_,_=setup(session,[quote(thscode='OTHER.SH')],'stock_quote','A.SH',timestamp=1790005915123)
    candidates=session.scalars(select(Candidate).where(Candidate.work_id==work.id)).all()
    assert len(candidates)==1 and candidates[0].readiness=='quarantined'


def test_explicit_null_observation_removes_stale_quote_without_numeric_capability(session):
    first=release_records(session,setup(session,[quote()],'stock_quote','A.SH',timestamp=1790005915123))
    fields=['open_price','high_price','low_price','last_price','prev_price','price_change',
            'price_change_ratio_pct','volume','turnover']
    second=release_records(session,setup(session,[dict(thscode='A.SH',**{f:None for f in fields})],
        'stock_quote','A.SH',timestamp=1790092315123),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='A.SH'))
    req=RecordRequirement(dataset_id='market.stock_quote_snapshot',
        semantic_series_id='tonghuashun-market.stock_quote_snapshot-observed-v1',subjects=[ident],
        release=second.id,fields=['reported_value_status'])
    result=json.loads(service(session).query_official(req))
    assert result['request_satisfied'] and result['items'][0]['reported_value_status']=='explicit_null_report'
    missing=json.loads(service(session).query_official(req.model_copy(update={'fields':['reported_last_price']})))
    assert not missing['request_satisfied'] and not missing['items']
    old=json.loads(service(session).query_official(req.model_copy(update={'release':first.id,'fields':['reported_last_price']})))
    assert old['request_satisfied'] and old['items'][0]['reported_last_price']=='10.5'
