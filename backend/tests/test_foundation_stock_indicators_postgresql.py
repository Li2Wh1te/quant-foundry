"""Nested indicators pass official publication and pinned historical reads."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_stock_indicators import payload
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject


def test_indicator_window_replacement_and_formula_rejection(session):
    first = release_records(session, setup(session, payload()['item'], 'stock_indicators', '000001.SZ'))
    current = release_records(session, setup(session, [], 'stock_indicators', '000001.SZ'), first)
    req = RecordRequirement(dataset_id='market.financial_indicator_window',
        semantic_series_id='tonghuashun-market.financial_indicator_window-observed-v1',
        subjects=[session.scalar(select(RecordSubject.id))], fields=['reports'])
    for release, count in [(first, 1), (current, 0)]:
        out = json.loads(service(session).query_official(req.model_copy(update={'release': release.id})))
        assert out['request_satisfied'] and len(out['items'][0]['reports']) == count
    for change in [dict(fields=['calculation_formula']), dict(fields=['comparable_units']),
                   dict(time_mode='strict_public_pit')]:
        out = json.loads(service(session).query_official(req.model_copy(update=change)))
        assert not out['request_satisfied'] and not out['items']
