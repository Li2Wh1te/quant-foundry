"""Complete event windows publish atomically and retain historical reads."""
import json
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_popularity_postgresql import setup
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.work_models import Candidate


@pytest.mark.parametrize('native,domain,row', [
    ('fund_dividends', 'fund.dividend_window', {'per_ten_cash_before_tax': '0.55', 'progress': '2'}),
    ('stock_actions', 'market.corporate_action_window', {'dividend_per_share': '0', 'per_share_bonus': '-0.2'}),
])
def test_whole_window_replacement_and_unsupported_semantics(session, native, domain, row):
    first = release_records(session, setup(session, [row, row], native, '160105.SZ'))
    current = release_records(session, setup(session, [], native, '160105.SZ'), first)
    subject = session.scalar(select(RecordSubject.id))
    req = RecordRequirement(dataset_id=domain, semantic_series_id=f'tonghuashun-{domain}-observed-v1',
                            subjects=[subject], fields=['events', 'source_code'])
    for release, count in [(first, 2), (current, 0)]:
        out = json.loads(service(session).query_official(req.model_copy(update={'release': release.id})))
        assert out['request_satisfied'] and len(out['items'][0]['events']) == count
    for changes in [dict(fields=['normalized_cashflow']), dict(fields=['payment_execution']),
                    dict(fields=['event_identity']), dict(time_mode='strict_public_pit')]:
        out = json.loads(service(session).query_official(req.model_copy(update=changes)))
        assert not out['request_satisfied'] and not out['items']


def test_corrupt_event_quarantines_entire_fund_dividend_window(session):
    work, _, _ = setup(session, [{'per_ten_cash_before_tax': '0.55'},
                                {'payment_date_ms': True}], 'fund_dividends', '160105.SZ')
    candidates = list(session.scalars(select(Candidate).where(Candidate.work_id == work.id)))
    assert len(candidates) == 1 and candidates[0].readiness == 'quarantined'
