"""Local discovery, late arrivals and failure fairness on real PostgreSQL."""
import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4, UUID
import pytest
from sqlalchemy import select
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import session, pipeline_engine, fixture, isolated_series
from app.data_foundation import record_updates as updates
from app.data_foundation.local_intake import register_campaign, capture_dataset
from app.data_foundation.intake import scan_page
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.source_refs import register_observation
from app.data_foundation.update_models import UpdateVisit
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State
from app.data_ingestion.tonghuashun.contracts import exact_json, content_hash, CollectionError


def observation(session, *, current=False, days=0, small_id=False):
    subject = 'company-'+uuid4().hex
    body = {'item': [{'company_id': subject, 'company_name': 'Update fixture'}]}
    row = Observation(id=UUID(int=uuid4().int >> 64) if small_id else uuid4(),
        dataset='fund_company', subject=subject, variant='default',
        observed_at=datetime.now(timezone.utc)+timedelta(days=days), request_json='{}',
        data_json=exact_json(body), content_hash=content_hash(body), row_count=1, chain_depth=0)
    session.add(row); session.flush()
    if current:
        session.add(State(dataset=row.dataset, subject=subject, variant='default',
            observation_id=row.id, revision=1, status='ready', attempted_at=datetime.now(timezone.utc)))
        session.flush()
    return row


def campaign(session, execution):
    row = register_campaign(session, event_key=uuid4().hex, decoder_id=execution)
    scan = capture_dataset(session, row.id, 'fund_company')
    scan_page(session, scan.id)
    return row.id


def test_fixed_capture_is_not_mistaken_for_formal_publication(session, tmp_path):
    (_, _, execution), _ = fixture(session, tmp_path)
    observation(session, current=True)
    campaign_id = campaign(session, execution)
    result = updates.discover(session, native_dataset='fund_company',
        backfill_campaign_id=campaign_id, execution_id=execution)
    assert result['status'] == 'waiting_backfill'
    assert result['pending_backfill'] >= 1 and result['items'] == []


def test_late_history_failure_rotation_and_published_receipts_survive_new_decoder(session, tmp_path, monkeypatch):
    (_, _, execution), image = fixture(session, tmp_path)
    initial = observation(session, current=True)
    campaign_id = campaign(session, execution)
    # Complete the actual frozen denominator through the normal verified
    # pipeline. This also permits rerunning this committed fixture database.
    captured = list(session.scalars(select(Observation).where(Observation.dataset == 'fund_company')))
    jobs = []
    for row in captured:
        state = session.get(State, (row.dataset, row.subject, row.variant))
        current = state is not None and state.observation_id == row.id
        source = register_observation(session, row.id, execution)
        jobs.append(register_job(session, source_ref_id=source.id, execution_id=execution,
            runtime_digest=image, archive_root=tmp_path, preserve_head=not current,
            source_revision=state.revision if current else None).id)
    session.commit()
    for job in jobs:
        assert advance_job(session.bind, job, runtime_digest=image, archive_root=tmp_path,
                           steps=10, publish=True)['status'] == 'published'
    assert updates.discover(session, native_dataset='fund_company', backfill_campaign_id=campaign_id,
                            execution_id=execution)['items'] == []
    # Both historical arrivals have older dates and smaller IDs than the
    # captured version. A permanent time/UUID high-water mark would miss them.
    broken = observation(session, days=-100, small_id=True)
    history = observation(session, days=-90, small_id=True)
    current = observation(session, current=True)
    broken_id, history_id, current_id = broken.id, history.id, current.id
    session.commit()
    original = updates.register_observation
    def isolated_failure(session, observation_id, execution_id):
        if observation_id == broken_id:
            raise CollectionError('隔离测试来源无法解码。')
        return original(session, observation_id, execution_id)
    monkeypatch.setattr(updates, 'register_observation', isolated_failure)
    options = dict(native_dataset='fund_company', backfill_campaign_id=campaign_id,
        execution_id=execution, runtime_digest=image, source_limit=2, steps_per_source=10, archive_root=tmp_path)
    first = updates.advance_updates(session.bind, **options)
    by_id = {item['observation_id']: item for item in first['items']}
    assert by_id[current_id]['status'] == 'published'
    assert by_id[broken_id]['status'] == 'failed'
    session.expire_all()
    assert json.loads(session.get(UpdateVisit, (broken_id, execution, 0)).result_json)['error_code'] == 'SOURCE_INVALID'
    session.rollback()
    second = updates.advance_updates(session.bind, **options)
    historical = next(item for item in second['items'] if item['observation_id'] == history_id)
    assert historical['status'] == 'published' and historical['head_activated'] is False
    # Discovery uses publication provenance, not the decoder identity, so a
    # new execution does not reprocess all already published local versions.
    (_, _, newer_execution), _ = fixture(session, tmp_path)
    remaining = updates.discover(session, native_dataset='fund_company', backfill_campaign_id=campaign_id,
                                 execution_id=newer_execution)
    assert [item['observation_id'] for item in remaining['items']] == [broken_id]


def test_quarantined_release_is_not_a_business_receipt_or_a_domain_barrier(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    bad = observation(session, current=True)
    bad_body = {'item': [{'company_name': 'Missing source identity'}]}
    bad.data_json, bad.content_hash = exact_json(bad_body), content_hash(bad_body)
    good = observation(session, current=True)
    empty = observation(session, current=True)
    empty_body = {'item': []}
    empty.data_json, empty.content_hash, empty.row_count = exact_json(empty_body), content_hash(empty_body), 0
    campaign_id = campaign(session, execution)
    bad_id, good_id, empty_id = bad.id, good.id, empty.id
    session.commit()

    from app.data_foundation.full_formalization import run_campaign
    result = run_campaign(session.bind, campaign_id=campaign_id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, datasets=['fund_company'], publish=True, steps=100)
    assert result['status'] == 'completed_with_issues'
    session.expire_all()
    receipts = updates.publication_receipts('fund_company')
    accepted = set(session.scalars(select(receipts.c.observation_id)))
    assert bad_id not in accepted and {good_id, empty_id} <= accepted
    found = updates.discover(session, native_dataset='fund_company',
        backfill_campaign_id=campaign_id, execution_id=execution)
    assert found['status'] == 'ready' and found['pending_backfill'] == 0
    assert found['backfill_issues'] == 1
    assert bad_id in {item['observation_id'] for item in found['items']}
    session.rollback()

    advanced = updates.advance_updates(session.bind, native_dataset='fund_company',
        backfill_campaign_id=campaign_id, execution_id=execution, runtime_digest=image,
        archive_root=tmp_path, source_limit=20, steps_per_source=10)
    by_id = {item['observation_id']: item for item in advanced['items']}
    assert by_id[bad_id]['status'] == 'quarantined'
    assert by_id[bad_id]['candidate_counts']['quarantined'] >= 1
    assert bad_id in {item['observation_id'] for item in updates.discover(session,
        native_dataset='fund_company', backfill_campaign_id=campaign_id,
        execution_id=execution)['items']}
