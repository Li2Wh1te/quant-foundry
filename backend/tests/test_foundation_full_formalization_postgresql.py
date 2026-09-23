"""Full publication resumes exact work and never treats capture as publication."""
import json
from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, fixture, isolated_series
from tests.test_foundation_record_updates_postgresql import observation, campaign
from app.data_foundation.full_formalization import plan_campaign, run_campaign, advance_source
from app.data_foundation.record_pipeline import register_job
from app.data_foundation.canonical import FoundationError
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
