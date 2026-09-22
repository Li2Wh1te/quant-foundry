"""Set replacement uses real source sealing, release gates and fixed reads."""
import json
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_sources import execution
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.tonghuashun.contracts import content_hash, exact_json
from app.data_foundation.source_refs import register_observation
from app.data_foundation.record_work import create_normalization, validate_release
from app.data_foundation.record_models import RecordSubject
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.work import claim
from app.data_foundation.bars import normalize_batch
from app.data_foundation.work_models import Candidate


def setup_index(session, members, dataset, subject):
    ex = execution(session)
    payload = {'item': members, 'timestamp': 1}
    observation = Observation(id=uuid4(), dataset=dataset, subject=subject, variant='default',
        observed_at=datetime.now(timezone.utc), request_json='{}', data_json=exact_json(payload),
        content_hash=content_hash(payload), row_count=len(members), chain_depth=0)
    session.add(observation)
    session.flush()
    ref = register_observation(session, observation.id, ex.id)
    work, policy = create_normalization(session, source_ref_id=ref.id, execution_id=ex.id)
    lease = claim(session, work_id=work.id)
    normalize_batch(session, work.id, lease.lease_epoch)
    return work, ex, policy


@pytest.mark.parametrize('dataset,subject,domain', [
    ('index_catalog', 'industry', 'index.category_snapshot'),
    ('index_constituents', '000001.SH', 'index.constituent_snapshot')])
def test_member_removal_empty_set_and_historical_release_are_distinct(session, dataset, subject, domain):
    first = release_records(session, setup_index(session, [{'thscode': 'A.SH'}, {'thscode': 'B.SH'}], dataset, subject))
    second = release_records(session, setup_index(session, [{'thscode': 'B.SH'}], dataset, subject), first)
    third = release_records(session, setup_index(session, [], dataset, subject), second)
    validate_release(session, third)
    subject_id = session.scalar(select(RecordSubject.id).where(RecordSubject.source_key == subject))
    req = RecordRequirement(dataset_id=domain, semantic_series_id=f'tonghuashun-{domain}-observed-v1',
        subjects=[subject_id], fields=['collection_key', 'members'])
    for release, expected in [(first, ['A.SH', 'B.SH']), (second, ['B.SH']), (third, [])]:
        result = json.loads(service(session).query_official(req.model_copy(update={'release': release.id})))
        assert result['request_satisfied'] and len(result['items']) == 1
        assert [m['source_code'] for m in result['items'][0]['members']] == expected
    assert not json.loads(service(session).query_official(req.model_copy(update={'time_mode': 'strict_public_pit'})))['items']


def test_invalid_child_never_publishes_valid_siblings_as_new_membership(session):
    fixture = setup_index(session, [{'thscode': 'A.SH'}, {}], 'index_constituents', 'I.SH')
    candidates = session.scalars(select(Candidate).where(Candidate.work_id == fixture[0].id)).all()
    assert len(candidates) == 1 and candidates[0].readiness == 'quarantined'
