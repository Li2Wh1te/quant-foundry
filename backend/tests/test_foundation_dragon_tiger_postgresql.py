"""Actual releases replace complete leaderboards and retain historical periods."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session,pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_dragon_tiger import document
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Candidate


def create(session,raw):
    return setup(session,raw['item'],'dragon_tiger','2026-09-15.all',
                 collection_scope=raw['collection_scope'],provider_metadata=raw['provider_metadata'])


def test_nested_leaderboard_empty_replacement_and_historical_read(session):
    raw=document();first=release_records(session,create(session,raw))
    raw['item'][0].update(stock_items=[],hot_money_items=[])
    second=release_records(session,create(session,raw),first)
    ident=session.scalar(select(RecordSubject.id).where(RecordSubject.source_key=='dragon_tiger:2026-09-15.all'))
    req=RecordRequirement(dataset_id='market.dragon_tiger_snapshot',semantic_series_id='tonghuashun-market.dragon_tiger_snapshot-observed-v1',
        subjects=[ident],fields=['stocks','hot_money'],business_range={'from':'2026-09-15','to':'2026-09-15'})
    for release,count in [(first,3),(second,0)]:
        result=json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert result['request_satisfied'] and len(result['items'][0]['stocks'])==count
        if count:assert result['items'][0]['hot_money'][0]['reported_buying']=='-4.5'
    for changes in [dict(fields=['currency']),dict(fields=['participant_identity']),dict(fields=['period_start']),dict(time_mode='strict_public_pit')]:
        result=json.loads(service(session).query_official(req.model_copy(update=changes)))
        assert not result['request_satisfied'] and not result['items']


def test_malformed_nested_member_is_one_quarantined_complete_observation(session):
    raw=document();raw['item'][0]['hot_money_items'][0]['rows'][0]['net_value']='NaN'
    work,_,_=create(session,raw)
    candidates=list(session.scalars(select(Candidate).where(Candidate.work_id==work.id)))
    assert len(candidates)==1 and candidates[0].readiness=='quarantined'
