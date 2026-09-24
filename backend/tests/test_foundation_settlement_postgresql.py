"""Settlement is durable published evidence, not a counter or a visit watermark."""
from datetime import datetime, timezone, date
from uuid import uuid4, UUID
import json

import pytest
from sqlalchemy import select, text, event, func
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_foundation_publication_postgresql import pytestmark
from tests.test_foundation_record_pipeline_postgresql import pipeline_engine, session, fixture, isolated_series
from tests.test_foundation_record_updates_postgresql import observation, campaign
from app.data_foundation import backfill_settlement as settlement
from app.data_foundation import record_work, record_updates
from app.data_foundation.intake_models import CampaignScan, ScanSeal
from app.data_foundation.performance_models import BackfillProbe, BackfillReceipt, BackfillCompletion, TableSettlementReceipt
from app.data_foundation.record_pipeline import register_job, advance_job
from app.data_foundation.source_refs import register_observation
from app.data_foundation.work import scope_key
from app.data_foundation.work_models import Release
from app.data_foundation.catalog import now


def test_frozen_ths_membership_requires_real_releases_and_never_accepts_quarantine(session, tmp_path, monkeypatch):
    from app.data_ingestion.models.tonghuashun import TonghuashunObservation
    from app.data_ingestion.tonghuashun.contracts import exact_json, content_hash
    (_, _, execution), image = fixture(session, tmp_path)
    good = observation(session, current=True)
    pending = observation(session)
    bad_body = {'item': [{}]}
    bad = TonghuashunObservation(id=uuid4(), dataset='fund_company', subject='bad-'+uuid4().hex,
        variant='default', observed_at=now(), request_json='{}', data_json=exact_json(bad_body),
        content_hash=content_hash(bad_body), row_count=1, chain_depth=0)
    session.add(bad); session.flush()
    campaign_id = campaign(session, execution)
    scan_id = session.get(CampaignScan, (campaign_id, 'fund_company')).scan_id
    dataset = record_updates.SOURCE_DATASETS[('tonghuashun', 'fund_company')]
    scope = scope_key(dataset, 1, 'default', record_work.series_for(dataset, 'tonghuashun'))
    args = dict(native_dataset='fund_company', scan_id=scan_id, scope=scope, limit=1)
    selected = []
    original = record_updates.publication_receipts
    def bounded(*args, **kwargs):
        selected.append(list(kwargs['observation_ids']))
        return original(*args, **kwargs)
    monkeypatch.setattr(record_updates, 'publication_receipts', bounded)
    for _ in range(4):
        initial = settlement.probe_page(session, **args)
        assert not initial['processed_complete'] and initial['pending_backfill'] == 3
    assert all(len(ids) == 1 for ids in selected)
    assert set(oid for ids in selected for oid in ids) == {good.id, pending.id, bad.id}
    jobs = {}
    for row in (good, pending, bad):
        source = register_observation(session, row.id, execution)
        jobs[row.id] = register_job(session, source_ref_id=source.id, execution_id=execution,
            runtime_digest=image, archive_root=tmp_path, preserve_head=True).id
    good_id, pending_id, bad_id = good.id, pending.id, bad.id
    session.commit()
    options = dict(runtime_digest=image, archive_root=tmp_path, steps=20)
    good_result = advance_job(session.bind, jobs[good_id], publish=True, **options)
    bad_result = advance_job(session.bind, jobs[bad_id], publish=True, **options)
    staged = advance_job(session.bind, jobs[pending_id], publish=False, **options)
    assert good_result['status'] == bad_result['status'] == 'published'
    assert staged['status'] == 'awaiting_publication'
    session.expire_all()
    probe = session.get(BackfillProbe, (scan_id, scope))
    assert probe.processed == 2 and probe.accepted == 1
    assert session.get(BackfillReceipt, (scan_id, scope, bad_id, 'processed')) is not None
    assert session.get(BackfillReceipt, (scan_id, scope, bad_id, 'accepted')) is None
    # A sealed draft and a published quarantine are both rejected by the
    # database qualification guard, even when inserted outside Python helpers.
    for oid, rid in ((bad_id, UUID(str(bad_result['release_id']))),
                     (pending_id, session.scalar(select(Release.id).where(Release.work_id == staged['work_id'])))):
        with pytest.raises(DBAPIError, match='qualified published'), session.begin_nested():
            session.add(BackfillReceipt(scan_id=scan_id, scope_key=scope, observation_id=oid,
                quality='accepted', release_id=rid, created_at=now()))
            session.flush()
    session.rollback()
    assert advance_job(session.bind, jobs[pending_id], publish=True, **options)['status'] == 'published'
    with Session(session.bind) as check, check.begin():
        result = settlement.probe_page(check, **args)
        assert result['processed_complete'] and not result['accepted_complete']
        assert result['pending_backfill'] == 0 and result['backfill_issues'] == 1
        # Inflating mutable counters cannot forge an accepted completion.
        with pytest.raises(DBAPIError, match='every fixed source'), check.begin_nested():
            check.execute(text('UPDATE foundation_backfill_probes SET accepted=target_count WHERE scan_id=:id'), {'id': scan_id})
            check.add(BackfillCompletion(scan_id=scan_id, scope_key=scope, quality='accepted',
                                        target_count=3, verified_at=now()))
            check.flush()
        with pytest.raises(DBAPIError), check.begin_nested():
            check.execute(text('UPDATE foundation_backfill_probes SET target_count=target_count+1 WHERE scan_id=:id'), {'id': scan_id})
        with pytest.raises(DBAPIError), check.begin_nested():
            check.execute(text('DELETE FROM foundation_backfill_receipts WHERE scan_id=:id'), {'id': scan_id})


def test_empty_sealed_capture_has_constant_cost_completion_and_no_publication_join(session, tmp_path, monkeypatch):
    from app.data_foundation.local_intake import register_campaign, capture_dataset
    from app.data_foundation.intake import scan_page
    (_, _, execution), image = fixture(session, tmp_path)
    native = 'fund_manager'
    capture = register_campaign(session, event_key=uuid4().hex, decoder_id=execution)
    scan = capture_dataset(session, capture.id, native)
    scan_page(session, scan.id)
    assert session.get(ScanSeal, scan.id).target_count == 0
    domain = record_updates.SOURCE_DATASETS[('tonghuashun', native)]
    scope = scope_key(domain, 1, 'default', record_work.series_for(domain, 'tonghuashun'))
    first = settlement.probe_page(session, native_dataset=native, scan_id=scan.id, scope=scope)
    assert first['accepted_complete']
    monkeypatch.setattr(record_updates, 'publication_receipts', lambda *_a, **_k: pytest.fail('completed capture rescanned'))
    second = settlement.probe_page(session, native_dataset=native, scan_id=scan.id, scope=scope)
    assert second['accepted_complete'] and second['pending_backfill'] == 0
    from types import SimpleNamespace
    from app.data_foundation.update_resumption import _ready, TASK_TYPES
    from app.data_foundation.scope_settlement import freeze_empty_scan
    from app.data_foundation.full_formalization import advance_source
    task = SimpleNamespace(task_type=TASK_TYPES[0], parameters=dict(native_dataset=native,
                           backfill_campaign_id=str(capture.id)))
    assert not _ready(session, task)
    empty = freeze_empty_scan(session, campaign_id=capture.id, native_dataset=native, execution_id=execution)
    empty_id = empty.id
    session.commit()
    assert advance_source(session.bind, empty_id, execution_id=execution, runtime_digest=image,
        archive_root=tmp_path, steps=100, publish=True)['status'] == 'published'
    assert _ready(session, task)


def test_fixed_tushare_receipts_skip_business_joins_once_accepted(pipeline_engine, tmp_path):
    from tests.test_foundation_table_changes_postgresql import bar
    from app.data_foundation.table_intake import capture_tables, TABLE_SOURCES
    from app.data_foundation.table_bootstrap import bootstrap_all
    from app.data_foundation.table_record_updates import ready_capture
    from app.data_foundation.full_formalization import advance_source
    with Session(pipeline_engine) as session:
        (_, _, execution), image = fixture(session, tmp_path)
    code = uuid4().hex[:12]
    with Session(pipeline_engine) as session, session.begin():
        session.add(bar(code))
    spec = next(item for item in TABLE_SOURCES if item.dataset == 'etf_daily')
    with pipeline_engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn, conn.begin():
        with Session(bind=conn) as session:
            capture, manifest = capture_tables(session, event_key=uuid4().hex, decoder_id=execution, sources=(spec,))
            capture_id, source_id = capture.id, manifest['tables'][0]['chunks'][0]['source_ref_id']
    assert bootstrap_all(pipeline_engine, capture_ref_id=capture_id, execution_id=execution,
        runtime_digest=image, archive_root=tmp_path, datasets=['etf_daily'], minimum_free_bytes=0)['status'] == 'completed'
    with Session(pipeline_engine) as session, session.begin():
        assert ready_capture(session, capture_ref_id=capture_id, native_dataset='etf_daily')['pending_backfill'] == 1
    assert advance_source(pipeline_engine, source_id, execution_id=execution, runtime_digest=image,
        archive_root=tmp_path, steps=100, publish=True)['status'] == 'published'
    statements = []
    def traced(conn, cursor, sql, *args):
        statements.append(sql)
    with Session(pipeline_engine) as session, session.begin():
        event.listen(session.connection(), 'before_cursor_execute', traced)
        result = ready_capture(session, capture_ref_id=capture_id, native_dataset='etf_daily')
        assert result['status'] == 'ready'
        assert not any('JOIN foundation_candidate_manifests' in sql for sql in statements)
        assert session.scalar(select(func.count()).select_from(TableSettlementReceipt).where(
            TableSettlementReceipt.capture_ref_id == capture_id)) == 1
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text('DELETE FROM foundation_table_settlement_receipts WHERE capture_ref_id=:id'), {'id': capture_id})
