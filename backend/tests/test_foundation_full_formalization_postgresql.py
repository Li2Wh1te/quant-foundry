"""Full publication resumes exact work and never treats capture as publication."""
import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from sqlalchemy import select, func
from sqlalchemy.orm import aliased
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, fixture, isolated_series
from tests.test_foundation_record_updates_postgresql import observation, campaign
from app.data_foundation.full_formalization import plan_campaign, run_campaign, advance_source
from app.data_foundation.record_pipeline import register_job
from app.data_foundation.canonical import FoundationError
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.record_models import OfficialRecord, RecordBlockMember
from app.data_foundation.work_models import Head, BlockRef
from app.data_foundation.work_models import Work, Release, CandidateManifest
from app.data_foundation.models import SourceRef
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation
from app.data_ingestion.tonghuashun.contracts import exact_json, content_hash
from app.data_foundation.record_adapters import SOURCE_DATASETS
from app.data_foundation.work import scope_key
from app.data_foundation import record_work
import pytest


def test_full_batch_has_separate_purpose_and_is_restartable(session, tmp_path):
    (update, source, execution), image = fixture(session, tmp_path)
    batch = register_job(session, source_ref_id=source, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, purpose='backfill')
    assert batch.id != update and batch.event_key.startswith('record-backfill:')
    assert json.loads(batch.manifest_json)['purpose'] == 'backfill'
    batch_id = batch.id
    session.commit()
    options = dict(execution_id=execution, runtime_digest=image, archive_root=tmp_path)
    first = advance_source(session.bind, source, steps=1, **options)
    assert first['status'] == 'step_ready'
    final = advance_source(session.bind, source, steps=100, publish=True, **options)
    assert final['status'] == 'published' and final['candidate_counts']['ready'] == 1
    again = advance_source(session.bind, source, steps=100, publish=True, **options)
    assert again['release_id'] == final['release_id'] and again['batch_id'] == batch_id


def test_campaign_reads_full_denominator_and_restart_uses_real_release_edges(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    observation(session, current=True)
    observation(session, current=True)
    campaign_id = campaign(session, execution)
    groups = plan_campaign(session, campaign_id, ['fund_company'])
    assert groups[0]['fixed_versions'] >= 2 and groups[0]['already_published'] == 0
    session.commit()
    options = dict(campaign_id=campaign_id, execution_id=execution, runtime_digest=image,
                   archive_root=tmp_path, datasets=['fund_company'], publish=True, steps=100)
    result = run_campaign(session.bind, **options)
    assert result['status'] == 'completed'
    again = run_campaign(session.bind, **options)
    assert again['domains'][0]['already_published'] == groups[0]['fixed_versions']
    assert again['domains'][0]['results'] == {}


def test_quarantined_source_is_reported_and_does_not_stop_healthy_source(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    bad = observation(session)
    from app.data_ingestion.tonghuashun.contracts import exact_json, content_hash
    body = {'item':[{'company_name':'Missing source identity'}]}
    bad.data_json, bad.content_hash = exact_json(body), content_hash(body)
    session.flush()
    observation(session, current=True)
    campaign_id = campaign(session, execution)
    session.commit()
    result = run_campaign(session.bind, campaign_id=campaign_id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, datasets=['fund_company'], publish=True, steps=100)
    assert result['status'] == 'completed_with_issues' and result['unresolved_sources'] >= 1
    assert result['domains'][0]['results']['quarantined'] >= 1
    assert result['domains'][0]['results']['published'] >= 1
    assert plan_campaign(session, campaign_id, ['fund_company'])[0]['sources']


def test_absent_domain_or_unapproved_execution_does_not_submit_work(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    campaign_id = campaign(session, execution)
    with pytest.raises(FoundationError):plan_campaign(session, campaign_id, ['not-installed'])
    with pytest.raises(ValueError):run_campaign(session.bind, campaign_id=campaign_id,
        execution_id=execution, runtime_digest=image, archive_root=tmp_path, publish=False)


def test_older_source_after_newer_head_keeps_new_values_and_gets_historical_release(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    newer_at = datetime.now(timezone.utc)
    older = register_baseline(session, source='tushare', dataset='etf_directory', scope={},
        rows=[dict(ts_code='ORDER.SH', csname='Old name'),
              dict(ts_code='ONLYOLD.SH', csname='Old unique')],
        observed_at=newer_at-timedelta(days=1), decoder_id=execution, event_key=uuid4().hex)
    newer = register_baseline(session, source='tushare', dataset='etf_directory', scope={},
        rows=[dict(ts_code='ORDER.SH', csname='New name')],
        observed_at=newer_at, decoder_id=execution, event_key=uuid4().hex)
    old_id, new_id = older.id, newer.id
    options = dict(execution_id=execution, runtime_digest=image, archive_root=tmp_path)
    session.commit()

    # Leave the old governance planned against the empty head, then let the
    # newer observation publish. The resumed old work must replan safely.
    first = advance_source(session.bind, old_id, steps=1, **options)
    assert first['status'] == 'step_ready'
    fresh = advance_source(session.bind, new_id, steps=100, publish=True, **options)
    assert fresh['status'] == 'published'
    stale = advance_source(session.bind, old_id, steps=100, publish=True, **options)
    assert stale['status'] == 'published' and stale['requires_history']
    session.expire_all()
    head = session.scalar(select(Head).where(Head.release_id == stale['release_id']))
    assert head is not None

    def names(release_id):
        rows = session.scalars(select(OfficialRecord).join(RecordBlockMember,
            RecordBlockMember.official_id == OfficialRecord.id)
            .join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
            .where(BlockRef.release_id == release_id)).all()
        return {json.loads(row.body_json)['source_code']: json.loads(row.body_json)['name'] for row in rows}

    assert names(head.release_id) == {'ORDER.SH': 'New name', 'ONLYOLD.SH': 'Old unique'}
    history = advance_source(session.bind, old_id, steps=100, publish=True,
        preserve_head=True, **options)
    assert history['status'] == 'published' and not history['head_activated']
    session.expire_all()
    assert names(history['release_id']) == {'ORDER.SH': 'Old name', 'ONLYOLD.SH': 'Old unique'}
    assert session.scalar(select(Head).where(Head.scope_key == head.scope_key)).release_id == stale['release_id']


def test_campaign_requeue_finishes_older_history_without_reverting_current(session, tmp_path, monkeypatch):
    from app.data_foundation import full_formalization as full

    (_, _, execution), image = fixture(session, tmp_path)
    subject = 'requeue-' + uuid4().hex
    base_time = datetime.now(timezone.utc)
    rows = []
    for name, observed_at in (('Old name', base_time-timedelta(days=2)),
                              ('New name', base_time-timedelta(days=1))):
        body = {'item':[{'company_id':subject, 'company_name':name}]}
        row = Observation(id=uuid4(), dataset='fund_company', subject=subject, variant='default',
            observed_at=observed_at, request_json='{}', data_json=exact_json(body),
            content_hash=content_hash(body), row_count=1, chain_depth=0)
        session.add(row)
        rows.append(row)
    session.flush()
    old_id, new_id = (row.id for row in rows)
    campaign_id = campaign(session, execution)
    session.commit()

    original = full.advance_source
    delayed = False
    def yield_old(engine, source_id, **options):
        nonlocal delayed
        with session.no_autoflush:
            source = session.get(SourceRef, source_id)
        if source and source.observation_id == old_id and not delayed:
            delayed = True
            return original(engine, source_id, **(options | {'steps':1}))
        return original(engine, source_id, **options)

    monkeypatch.setattr(full, 'advance_source', yield_old)
    result = full.run_campaign(session.bind, campaign_id=campaign_id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, datasets=['fund_company'], publish=True, steps=100)
    assert result['status'] in ('completed', 'completed_with_issues') and delayed
    session.expire_all()
    old_source = session.scalar(select(SourceRef.id).where(SourceRef.observation_id == old_id))
    new_source = session.scalar(select(SourceRef.id).where(SourceRef.observation_id == new_id))
    domain = SOURCE_DATASETS[('tonghuashun', 'fund_company')]
    scope = scope_key(domain, 1, 'default', record_work.series_for(domain, 'tonghuashun'))
    head = session.get(Head, scope)
    current = session.scalars(select(OfficialRecord).join(RecordBlockMember,
        RecordBlockMember.official_id == OfficialRecord.id)
        .join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
        .where(BlockRef.release_id == head.release_id)).all()
    assert any(json.loads(row.body_json).get('company_id') == subject
               and json.loads(row.body_json).get('name') == 'New name' for row in current)
    origin = aliased(Work)
    old_releases = session.scalar(select(func.count()).select_from(Release)
        .join(Work, Work.id == Release.work_id)
        .join(CandidateManifest, CandidateManifest.id == Work.candidate_manifest_id)
        .join(origin, origin.id == CandidateManifest.work_id)
        .where(origin.source_ref_id == old_source, Release.status == 'published'))
    assert old_releases == 2
    assert {old_source, new_source} <= full.published_sources(session,
        [old_source, new_source], 'tonghuashun', domain)


def test_older_source_cannot_reopen_newer_blocked_key(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    observed_at = datetime.now(timezone.utc)
    old = register_baseline(session, source='tushare', dataset='etf_directory', scope={},
        rows=[dict(ts_code='BLOCKED.SH', csname='Old value')],
        observed_at=observed_at-timedelta(days=1), decoder_id=execution, event_key=uuid4().hex)
    newer = register_baseline(session, source='tushare', dataset='etf_directory', scope={},
        rows=[dict(ts_code='BLOCKED.SH', csname='Conflicting A'),
              dict(ts_code='BLOCKED.SH', csname='Conflicting B')],
        observed_at=observed_at, decoder_id=execution, event_key=uuid4().hex)
    old_id, new_id = old.id, newer.id
    options = dict(execution_id=execution, runtime_digest=image, archive_root=tmp_path,
                   steps=100, publish=True)
    session.commit()
    current = advance_source(session.bind, new_id, **options)
    assert current['status'] == 'quarantined'
    with pytest.raises(FoundationError, match='较旧固定来源'):
        advance_source(session.bind, old_id, **options)
    history = advance_source(session.bind, old_id, preserve_head=True, **options)
    assert history['status'] == 'published' and not history['head_activated']
    session.expire_all()
    assert session.scalar(select(Head).where(Head.release_id == current['release_id'])) is not None
