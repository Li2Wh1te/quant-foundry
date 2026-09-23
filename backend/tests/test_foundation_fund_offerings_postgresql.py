"""Observed offering membership replaces atomically while old windows persist."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_index_snapshots_postgresql import setup_index
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_work import validate_release
from app.data_foundation.work_models import Candidate


def test_whole_offering_set_removal_and_intraday_history_are_readable(session):
    setup = lambda rows: setup_index(session, rows, 'fund_offerings', 'active')
    first = release_records(session, setup([{'thscode': 'A.OF', 'subscription_end_ms': 1794812400000}, {'thscode': 'B.OF'}]))
    second = release_records(session, setup([{'thscode': 'B.OF'}]), first)
    third = release_records(session, setup([]), second)
    validate_release(session, third)
    subject = session.scalar(select(RecordSubject.id).where(RecordSubject.kind == 'fund_offering_filter'))
    request = RecordRequirement(dataset_id='fund.offering_snapshot',
        semantic_series_id='tonghuashun-fund.offering_snapshot-observed-v1', subjects=[subject], fields=['collection_key', 'members'])
    for release, expected in [(first, ['A.OF', 'B.OF']), (second, ['B.OF']), (third, [])]:
        result = json.loads(service(session).query_official(request.model_copy(update={'release': release.id})))
        assert result['request_satisfied'] and len(result['items']) == 1
        assert [m['source_code'] for m in result['items'][0]['members']] == expected
        if release == first:
            assert result['items'][0]['members'][0]['reported_subscription_end'] == '2026-11-16T07:00:00Z'
    for changes in [{'fields': ['current_subscription_eligibility']}, {'time_mode': 'strict_public_pit'}]:
        result = json.loads(service(session).query_official(request.model_copy(update=changes)))
        assert not result['request_satisfied'] and not result['items']


def test_conflicting_window_quarantines_collection_not_just_bad_member(session):
    work, _, _ = setup_index(session, [{'thscode': 'GOOD.OF'}, {'thscode': 'BAD.OF',
        'subscription_start_ms': 1794812400000, 'subscription_end_ms': 1786896000000}], 'fund_offerings', 'active')
    candidates = list(session.scalars(select(Candidate).where(Candidate.work_id == work.id)))
    assert len(candidates) == 1 and candidates[0].readiness == 'quarantined'
