"""Explicit empty narrative windows publish without inventing market absence."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.narrative_windows import DOMAINS
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject


@pytest.mark.parametrize('native,field', [('fund_news','articles'),('rank_trend','points'),('anomaly_stock','narratives')])
def test_empty_publication_and_refused_market_completeness(session, native, field):
    meta = {} if native == 'anomaly_stock' else dict(requested_start='2026-09-01',requested_end='2026-09-20')
    release = release_records(session, setup(session, [], native, '510300.SH', **meta))
    domain = DOMAINS[native]
    req = RecordRequirement(dataset_id=domain,semantic_series_id=f'tonghuashun-{domain}-observed-v1',
        subjects=[session.scalar(select(RecordSubject.id))],fields=[field],release=release.id)
    out = json.loads(service(session).query_official(req))
    assert out['request_satisfied'] and out['items'][0][field] == []
    for change in [dict(fields=['complete_history']),dict(fields=['verified_market_events']),dict(time_mode='strict_public_pit')]:
        out = json.loads(service(session).query_official(req.model_copy(update=change)))
        assert not out['request_satisfied'] and not out['items']
