"""Actual publication replaces an entire NAV window and preserves old reads."""
import json
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, func
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_sources import execution
from tests.test_foundation_records_postgresql import release_records
from tests.test_foundation_holdings_postgresql import service
from tests.test_foundation_fund_nav import payload
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.tonghuashun.contracts import content_hash, exact_json
from app.data_foundation.source_refs import register_observation
from app.data_foundation.record_work import create_normalization, validate_release
from app.data_foundation.record_models import RecordSubject, OfficialRecord
from app.data_foundation.record_query import RecordRequirement
from app.data_foundation.work import claim
from app.data_foundation.bars import normalize_batch
from app.data_foundation.work_models import Candidate


def setup_nav(session, rows):
    ex = execution(session)
    content = payload(rows)
    observation = Observation(id=uuid4(), dataset='fund_nav', subject='NAV.OF', variant='default',
        observed_at=datetime.now(timezone.utc), request_json='{}', data_json=exact_json(content),
        content_hash=content_hash(content), row_count=len(rows), chain_depth=0)
    session.add(observation)
    session.flush()
    ref = register_observation(session, observation.id, ex.id)
    work, policy = create_normalization(session, source_ref_id=ref.id, execution_id=ex.id)
    lease = claim(session, work_id=work.id)
    normalize_batch(session, work.id, lease.lease_epoch)
    return work, ex, policy


def test_new_basis_replaces_window_without_old_dates_and_old_release_stays_readable(session, monkeypatch):
    first_rows = [{'nav_date': '2021-09-15', 'unit_nav': 1, 'adj_nav': 7},
                  {'nav_date': '2026-09-17', 'unit_nav': 2, 'adj_nav': 8}]
    new_rows = [{'nav_date': '2026-09-17', 'unit_nav': 2, 'adj_nav': 9},
                {'nav_date': '2026-09-18', 'unit_nav': 3}]
    first = release_records(session, setup_nav(session, first_rows))
    second = release_records(session, setup_nav(session, new_rows), first)
    third = release_records(session, setup_nav(session, new_rows), second)
    validate_release(session, third)
    assert session.scalar(select(func.count()).select_from(OfficialRecord)) == 2
    subject = session.scalar(select(RecordSubject.id).where(RecordSubject.source_key == 'NAV.OF'))
    request = RecordRequirement(dataset_id='fund.nav_snapshot',
        semantic_series_id='tonghuashun-fund.nav_snapshot-observed-v1', subjects=[subject],
        fields=['source_code', 'sequence_start', 'sequence_end', 'points'])
    import app.data_foundation.source_refs as sources
    monkeypatch.setattr(sources, 'read_source', lambda *a: (_ for _ in ()).throw(AssertionError('Source decoding during formal read')))
    for release, start, adjusted in ((first, '2021-09-15', '7'), (third, '2026-09-17', '9')):
        result = json.loads(service(session).query_official(request.model_copy(update={'release': release.id})))
        assert result['request_satisfied'] and len(result['items']) == 1
        item = result['items'][0]
        assert item['sequence_start'] == start and len(item['points']) == 2
        assert item['points'][0]['reported_adjusted_nav'] == adjusted
    for changes in ({'fields': ['currency']}, {'fields': ['cumulative_nav']}, {'time_mode': 'strict_public_pit'}):
        result = json.loads(service(session).query_official(request.model_copy(update=changes)))
        assert not result['request_satisfied'] and not result['items']


def test_one_invalid_point_does_not_create_a_partial_window(session):
    work, _, _ = setup_nav(session, [{'nav_date': '2026-09-17', 'unit_nav': 1},
        {'nav_date': '2026-09-18', 'unit_nav': -1}])
    candidates = list(session.scalars(select(Candidate).where(Candidate.work_id == work.id)))
    assert len(candidates) == 1 and candidates[0].readiness == 'quarantined'
