"""Full publication resumes exact work and never treats capture as publication."""
import json
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
from sqlalchemy import select, func
from sqlalchemy.orm import Session, aliased
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, fixture, isolated_series
from tests.test_foundation_record_updates_postgresql import observation, campaign
from app.data_foundation.full_formalization import plan_campaign, run_campaign, advance_source, published_sources
from app.data_foundation.record_pipeline import register_job
from app.data_foundation.canonical import FoundationError
from app.data_foundation.source_refs import register_baseline
from app.data_foundation.record_models import OfficialRecord, RecordBlockMember, RecordSubject, CandidateRecord
from app.data_foundation.work_models import Head, BlockRef
from app.data_foundation.work_models import Work, Release, CandidateManifest, Candidate, Decision
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


def test_campaign_publishes_nonempty_fixed_table_chunks_and_unblocks_updates(session, tmp_path):
    from tests.test_foundation_table_changes_postgresql import bar
    from app.data_foundation.table_intake import capture_tables, TABLE_SOURCES
    from app.data_foundation.table_bootstrap import bootstrap_all
    from app.data_foundation.table_record_updates import ready_capture

    (_, _, execution), image = fixture(session, tmp_path)
    code = uuid4().hex[:12]
    session.add(bar(code))
    observation(session, current=True)
    campaign_id = campaign(session, execution)
    session.commit()
    spec = next(item for item in TABLE_SOURCES if item.dataset == 'etf_daily')
    with session.bind.connect().execution_options(isolation_level='REPEATABLE READ') as connection, connection.begin():
        with Session(bind=connection) as capture_session:
            capture, manifest = capture_tables(capture_session, event_key=uuid4().hex,
                decoder_id=execution, sources=(spec,))
            capture_id = capture.id
            source_id = UUID(manifest['tables'][0]['chunks'][0]['source_ref_id'])
    bootstrap = bootstrap_all(session.bind, capture_ref_id=capture_id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, datasets=['etf_daily'], minimum_free_bytes=0)
    assert bootstrap['status'] == 'completed'
    assert ready_capture(session, capture_ref_id=capture_id, native_dataset='etf_daily')['status'] == 'waiting_backfill'

    options = dict(campaign_id=campaign_id, execution_id=execution, runtime_digest=image,
                   archive_root=tmp_path, datasets=['fund_company'], table_capture_ref_id=capture_id,
                   minimum_free_bytes=0, publish=True, steps=100)
    result = run_campaign(session.bind, **options)
    table = next(group for group in result['domains'] if group['result_key'] == 'tushare:etf_daily')
    assert table['fixed_versions'] == 1 and table['results']['published'] == 1
    assert source_id in published_sources(session, [source_id], 'tushare',
        SOURCE_DATASETS[('tushare', 'etf_daily')])
    assert ready_capture(session, capture_ref_id=capture_id, native_dataset='etf_daily')['status'] == 'ready'
    again = run_campaign(session.bind, **options)
    table_again = next(group for group in again['domains'] if group['result_key'] == 'tushare:etf_daily')
    assert table_again['already_published'] == 1 and table_again['results'] == {}


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


def _company_versions(session, execution, subject, versions):
    """Freeze real source observations with one source-local business key."""
    base = datetime.now(timezone.utc) - timedelta(days=len(versions) + 1)
    observations = {}
    for ordinal, (label, names) in enumerate(versions):
        rows = [{'company_id': subject, 'company_name': name} for name in names]
        body = {'item': rows}
        row = Observation(id=uuid4(), dataset='fund_company', subject=subject, variant='default',
            observed_at=base + timedelta(days=ordinal), request_json='{}',
            data_json=exact_json(body), content_hash=content_hash(body),
            row_count=len(rows), chain_depth=0)
        session.add(row)
        observations[label] = row.id
    session.flush()
    campaign(session, execution)
    return {label: session.scalar(select(SourceRef.id).where(SourceRef.observation_id == observation_id))
            for label, observation_id in observations.items()}


def _company_member(session, subject):
    domain = SOURCE_DATASETS[('tonghuashun', 'fund_company')]
    scope = scope_key(domain, 1, 'default', record_work.series_for(domain, 'tonghuashun'))
    session.expire_all()
    head = session.get(Head, scope)
    assert head is not None
    row = session.execute(select(RecordBlockMember, OfficialRecord)
        .join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
        .join(RecordSubject, RecordSubject.id == RecordBlockMember.subject_id)
        .outerjoin(OfficialRecord, OfficialRecord.id == RecordBlockMember.official_id)
        .where(BlockRef.release_id == head.release_id, RecordSubject.source_key == subject)).one()
    return row, head


def test_unchanged_newer_observation_keeps_its_order_after_older_replays(session, tmp_path, monkeypatch):
    (_, _, execution), image = fixture(session, tmp_path)
    subject = 'same-value-' + uuid4().hex
    ids = _company_versions(session, execution, subject,
        [('T0', ['Before']), ('T1', ['X']), ('T2', ['Y']), ('T3', ['X']), ('T4', ['Z'])])
    options = dict(execution_id=execution, runtime_digest=image, archive_root=tmp_path,
                   steps=100, publish=True)
    session.commit()

    first = advance_source(session.bind, ids['T1'], **options)
    original_encode = record_work.encode
    def legacy_decision_evidence(value):
        # Older immutable decisions have no explicit selected-source receipt.
        # Only the test's decision writer is adapted to exercise that read path.
        if isinstance(value, dict) and 'policy_id' in value and 'effective_source_ref_id' in value:
            value = {key: item for key, item in value.items() if key != 'effective_source_ref_id'}
        return original_encode(value)

    with monkeypatch.context() as patch:
        patch.setattr(record_work, 'encode', legacy_decision_evidence)
        same = advance_source(session.bind, ids['T3'], **options)
    assert first['status'] == same['status'] == 'published'
    same_evidence = json.loads(session.scalar(select(Decision.evidence_json)
        .where(Decision.work_id == session.get(Release, same['release_id']).work_id)))
    assert same_evidence['reason'] == 'UNCHANGED_CANONICAL_RECORD'
    assert 'effective_source_ref_id' not in same_evidence
    (member, value), _ = _company_member(session, subject)
    assert member.state == 'value' and json.loads(value.body_json)['name'] == 'X'

    # A restarted older backfill must compare against T3's confirmation, not
    # the T1 official value that the unchanged decision reused.
    options_without_steps = {key: value for key, value in options.items() if key != 'steps'}
    with monkeypatch.context() as patch:
        patch.setattr(record_work, 'encode', legacy_decision_evidence)
        assert advance_source(session.bind, ids['T2'], steps=1, **options_without_steps)['status'] == 'step_ready'
        older = advance_source(session.bind, ids['T2'], **options)
    assert older['status'] == 'published' and older['requires_history']
    (member, value), _ = _company_member(session, subject)
    assert json.loads(value.body_json)['name'] == 'X'
    assert json.loads(session.get(Decision, member.decision_id).evidence_json)['reason'] == record_work.OLDER_SOURCE_RETAINED
    assert 'effective_source_ref_id' not in json.loads(session.get(Decision, member.decision_id).evidence_json)

    # A second stale retain inherits T3 as well, while T2 remains readable in
    # an independent historical release and the original T1 release survives.
    assert advance_source(session.bind, ids['T0'], steps=1, **options_without_steps)['status'] == 'step_ready'
    oldest = advance_source(session.bind, ids['T0'], **options)
    assert oldest['status'] == 'published' and oldest['requires_history']
    (member, value), _ = _company_member(session, subject)
    assert json.loads(value.body_json)['name'] == 'X'
    assert json.loads(session.get(Decision, member.decision_id).evidence_json)['effective_source_ref_id'] == str(ids['T3'])
    history = advance_source(session.bind, ids['T2'], preserve_head=True, **options)
    assert history['status'] == 'published' and not history['head_activated']

    def historical_name(release_id):
        value = session.scalar(select(OfficialRecord).join(RecordBlockMember,
            RecordBlockMember.official_id == OfficialRecord.id)
            .join(BlockRef, BlockRef.block_id == RecordBlockMember.block_id)
            .join(RecordSubject, RecordSubject.id == RecordBlockMember.subject_id)
            .where(BlockRef.release_id == release_id, RecordSubject.source_key == subject))
        return json.loads(value.body_json)['name']

    assert historical_name(first['release_id']) == 'X'
    assert historical_name(history['release_id']) == 'Y'
    latest = advance_source(session.bind, ids['T4'], **options)
    assert latest['status'] == 'published'
    (_, value), head = _company_member(session, subject)
    assert head.release_id == latest['release_id'] and json.loads(value.body_json)['name'] == 'Z'


def test_older_locatable_quarantine_cannot_block_newer_value(session, tmp_path):
    (_, _, execution), image = fixture(session, tmp_path)
    subject = 'duplicate-order-' + uuid4().hex
    ids = _company_versions(session, execution, subject,
        [('T1', ['Bad A', 'Bad B']), ('T2', ['Good']), ('T3', ['Later A', 'Later B'])])
    options = dict(execution_id=execution, runtime_digest=image, archive_root=tmp_path,
                   steps=100, publish=True)
    session.commit()

    healthy = advance_source(session.bind, ids['T2'], **options)
    assert healthy['status'] == 'published'
    older = advance_source(session.bind, ids['T1'], **options)
    assert older['status'] == 'quarantined' and older['candidate_counts']['quarantined'] == 2
    assert older['requires_history']
    (member, value), _ = _company_member(session, subject)
    assert member.state == 'value' and json.loads(value.body_json)['name'] == 'Good'
    assert session.scalar(select(func.count()).select_from(CandidateRecord)
        .join(Candidate, Candidate.id == CandidateRecord.candidate_id)
        .where(Candidate.source_ref_id == ids['T1'], Candidate.readiness == 'quarantined')) == 2
    domain = SOURCE_DATASETS[('tonghuashun', 'fund_company')]
    assert ids['T1'] not in published_sources(session, [ids['T1']], 'tonghuashun', domain)

    # A genuinely later duplicate still blocks the current key by policy.
    newer_bad = advance_source(session.bind, ids['T3'], **options)
    assert newer_bad['status'] == 'quarantined'
    (member, value), head = _company_member(session, subject)
    assert member.state == 'blocked' and value is None and head.release_id == newer_bad['release_id']
