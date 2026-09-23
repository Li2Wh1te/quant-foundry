"""Observed portfolios do not masquerade as reviewed, complete holdings reports."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_portfolio_windows import payload
from app.data_foundation.portfolio_windows import DOMAINS
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject


@pytest.mark.parametrize('native', list(DOMAINS))
def test_publication_keeps_distinct_semantic_series_and_historical_windows(session, native):
    current = native == 'fund_holdings'
    raw = dict(item=[dict(thscode='600001.SH')]) if current else payload('bond' if native == 'fund_bond_history' else 'stock')
    metadata = {k:v for k,v in raw.items() if k != 'item'}
    first = release_records(session, setup(session, raw['item'], native, '160105.SZ', **metadata))
    empty_metadata = {} if current else dict(report_directory=dict(item=[]))
    latest = release_records(session, setup(session, [], native, '160105.SZ', **empty_metadata), first)
    domain = DOMAINS[native]; field = 'members' if current else 'reports'
    req = RecordRequirement(dataset_id=domain, semantic_series_id=f'tonghuashun-{domain}-observed-v1',
        subjects=[session.scalar(select(RecordSubject.id))], fields=[field])
    for release, count in [(first,1),(latest,0)]:
        out = json.loads(service(session).query_official(req.model_copy(update={'release':release.id})))
        assert out['request_satisfied'] and len(out['items'][0][field]) == count
    for change in [dict(fields=['resolved_instrument_identities']),dict(fields=['portfolio_complete']),
                   dict(fields=['transport_complete']),dict(time_mode='strict_public_pit')]:
        out = json.loads(service(session).query_official(req.model_copy(update=change)))
        assert not out['request_satisfied'] and not out['items']
