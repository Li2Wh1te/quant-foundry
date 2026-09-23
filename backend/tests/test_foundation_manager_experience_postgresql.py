"""Exercise real career replacement and the unavailable-field read gate."""
import json
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_index_snapshots_postgresql import setup_index
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.record_work import validate_release


def test_career_set_replacement_preserves_history_and_unavailable_semantics(session):
    setup = lambda history: setup_index(session, [{'investment_history': history}], 'fund_manager_experience', 'M-career')
    first = release_records(session, setup({'F': {'code': 'F', 'end': '至今', 'hs_rate': '4.2'}}))
    second = release_records(session, setup({}), first)
    validate_release(session, second)
    subject = session.scalar(select(RecordSubject.id).where(RecordSubject.source_key == 'M-career'))
    request = RecordRequirement(dataset_id='fund.manager_experience',
        semantic_series_id='tonghuashun-fund.manager_experience-observed-v1',
        subjects=[subject], fields=['manager_id', 'assignments'], release=first.id)
    first_result = json.loads(service(session).query_official(request))
    assert first_result['request_satisfied']
    assert first_result['items'][0]['assignments'][0]['end_text'] == '至今'
    current = json.loads(service(session).query_official(request.model_copy(update={'release': second.id})))
    assert current['request_satisfied'] and current['items'][0]['assignments'] == []
    unavailable = json.loads(service(session).query_official(request.model_copy(update={'fields': ['performance']})))
    assert not unavailable['request_satisfied'] and not unavailable['items']
    pit = json.loads(service(session).query_official(request.model_copy(update={'time_mode': 'strict_public_pit'})))
    assert not pit['items']
