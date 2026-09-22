"""Finite reconciliation must recover late IDs and roll back failed pages."""
from uuid import UUID
import pytest
from sqlalchemy import select
from app.data_foundation.intake import register_scope, set_paused, start_scan, scan_page
from app.data_foundation.intake_models import IntakeItem, IntakeScope, Scan, ScanEntry, SourcePointer, IntakeControl, ScanTarget, ScanFailure, ScanSeal, IntakeCampaign, CampaignScan
from app.data_foundation.models import SourceRef
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State
from tests.test_foundation_sources import session, execution, observations


@pytest.fixture
def setup(session):
    for model in (IntakeScope, IntakeControl, Scan, IntakeItem, ScanEntry, SourcePointer, State, ScanTarget, ScanFailure, ScanSeal, IntakeCampaign, CampaignScan):
        model.__table__.create(session.bind)
    ex = execution(session)
    scope = register_scope(session, dataset='etf_daily', subject='159001.SZ', variant='daily',
        selector={'start': '2026-06-24', 'end': '2026-09-15'}, decoder_id=ex.id)
    return session, scope


def test_late_smaller_id_is_found_in_next_full_pass(setup):
    session, scope = setup
    anchor, delta = observations(session)
    set_paused(session, scope.id, False)
    first = start_scan(session, scope.id, 'first')
    assert scan_page(session, first.id)['registered'] == 2
    late = Observation(id=UUID(int=1), dataset=anchor.dataset, subject=anchor.subject, variant=anchor.variant,
        observed_at=anchor.observed_at, request_json='{}', data_json=anchor.data_json,
        content_hash=anchor.content_hash, row_count=anchor.row_count, chain_depth=0)
    session.add(late); session.flush()
    # A completed pass never pretends that a late commit belonged to its input.
    assert scan_page(session, first.id)['seen'] == 2
    second = start_scan(session, scope.id, 'second')
    assert scan_page(session, second.id)['registered'] == 1
    assert second.seen == 3
    items = session.scalars(select(IntakeItem)).all()
    assert len(items) == 3
    assert start_scan(session, scope.id, 'second').id == second.id
    assert len({item.source_ref_id for item in items}) == 3


def test_pause_resume_and_page_budget(setup):
    session, scope = setup
    observations(session)
    scan = start_scan(session, scope.id, 'paused')
    assert scan_page(session, scan.id)['paused'] is True
    assert scan.seen == 0
    set_paused(session, scope.id, False)
    assert scan_page(session, scan.id, limit=1)['seen'] == 1
    set_paused(session, scope.id, True)
    assert scan_page(session, scan.id)['seen'] == 1
    set_paused(session, scope.id, False)
    assert scan_page(session, scan.id)['seen'] == 2
    assert scan.status == 'completed'


def test_failure_does_not_advance_checkpoint_or_keep_partial_refs(setup):
    session, scope = setup
    anchor, delta = observations(session)
    delta.content_hash = '0' * 64
    session.flush()
    set_paused(session, scope.id, False)
    scan = start_scan(session, scope.id, 'failure')
    result = scan_page(session, scan.id)
    assert result['status'] == 'failed'
    assert scan.seen == 0 and scan.cursor_id is None
    assert session.scalar(select(SourceRef)) is None
    assert session.scalar(select(IntakeItem)) is None
    assert session.scalar(select(ScanEntry)) is None


def test_coverage_union_does_not_hide_internal_date_holes():
    from app.data_foundation.coverage import covers_request,merge_coverage
    def part(start,end):
        return dict(status='pass',start=start,end=end,subjects=[dict(instrument_id='one',code='x',exchange='SSE')],expected_keys=[])
    combined=merge_coverage([part('2026-01-01','2026-01-02'),part('2026-01-04','2026-01-05')])
    assert not covers_request(combined,{'one'},'2026-01-01','2026-01-05')
    assert covers_request(combined,{'one'},'2026-01-04','2026-01-05')
    adjacent=merge_coverage([part('2026-01-01','2026-01-02'),part('2026-01-03','2026-01-05')])
    assert covers_request(adjacent,{'one'},'2026-01-01','2026-01-05')
    assert not covers_request(adjacent,{'unknown'},'2026-01-01','2026-01-05')


def test_full_scan_is_periodic_safety_net_after_recent_pass(setup):
    from app.data_foundation.intake import next_scan
    session,scope=setup
    set_paused(session,scope.id,False)
    first=next_scan(session,scope.id,'one')
    assert first.mode=='full'
    scan_page(session,first.id)
    second=next_scan(session,scope.id,'two')
    assert second.mode=='recent'
    assert next_scan(session,scope.id,'another').id==second.id


def test_explicit_source_priority_does_not_fallback_or_merge_fields():
    from app.data_foundation.governance import choose
    policy={'source_order':['primary','secondary'],'series':'series','comparison':{'enabled':True,'mode':'source_priority'},'fallback':{'enabled':False}}
    primary={'id':'p','source':'primary','series':'series','ready':True}
    secondary={'id':'s','source':'secondary','series':'series','ready':True}
    assert choose([secondary,primary],policy)==('select','p','PRIMARY_SOURCE_PRIORITY')
    assert choose([secondary,{**primary,'ready':False}],policy)==('block',None,'CORE_VALUE_INVALID')
    assert choose([secondary],policy)==('gap',None,'PRIMARY_SOURCE_MISSING')
    assert choose([primary,primary],policy)==('block',None,'SOURCE_REVISION_AMBIGUOUS')


def test_frozen_full_local_membership_excludes_late_versions_until_next_pass(setup):
    from app.data_foundation.intake import next_scan
    session, _ = setup
    anchor, delta = observations(session)
    scope = register_scope(session, dataset=anchor.dataset, subject=anchor.subject,
        variant=anchor.variant, selector={'scope': 'all_local'}, decoder_id=execution(session).id)
    set_paused(session, scope.id, False)
    first = next_scan(session, scope.id, 'frozen-one')
    assert first.mode == 'frozen'
    assert len(session.scalars(select(ScanTarget)).all()) == 2
    # Both lower and higher UUIDs can commit after capture; neither belongs to
    # this run, even if its provider observation timestamp predates the run.
    for value in (1, 2**128 - 1):
        session.add(Observation(id=UUID(int=value), dataset=anchor.dataset, subject=anchor.subject,
            variant=anchor.variant, observed_at=anchor.observed_at, request_json='{}',
            data_json=anchor.data_json, content_hash=anchor.content_hash,
            row_count=anchor.row_count, chain_depth=0))
    session.flush()
    assert start_scan(session, scope.id, 'frozen-one', mode='frozen').id == first.id
    while first.status != 'completed':
        scan_page(session, first.id, limit=1)
    assert first.seen == 2
    second = next_scan(session, scope.id, 'frozen-two')
    result = scan_page(session, second.id)
    assert result['seen'] == 4 and result['registered'] == 2


def test_frozen_hash_change_is_isolated_without_substituting_latest(setup):
    session, scope = setup
    anchor, _ = observations(session)
    set_paused(session, scope.id, False)
    scan = start_scan(session, scope.id, 'fixed-hash', mode='frozen')
    anchor.content_hash = 'f' * 64
    session.flush()
    result = scan_page(session, scan.id)
    assert result['status'] == 'completed' and result['traversal_complete']
    assert result['seen'] == 2 and not result['registration_complete']
    assert result['failed'] == 2 and result['reused'] == 0
    assert session.scalar(select(IntakeItem)) is None


def test_intake_accepts_new_local_domains_without_claiming_formal_support(setup):
    session, _ = setup
    scope = register_scope(session, dataset='future_local_source', subject='source-subject',
        variant='native', selector={'scope': 'all_local'}, decoder_id=execution(session).id)
    set_paused(session, scope.id, False)
    scan = start_scan(session, scope.id, 'empty-local', mode='frozen')
    result = scan_page(session, scan.id)
    assert result['seen'] == 0 and result['status'] == 'completed'
    assert '本地完整历史范围' in result['message']
    assert 'published' not in result and 'release_id' not in result


def test_corrupt_version_does_not_block_unrelated_subject_and_is_retried(setup):
    session, _ = setup
    anchor, delta = observations(session)
    bad = Observation(id=UUID(int=1), dataset=anchor.dataset, subject='BROKEN', variant=anchor.variant,
        observed_at=anchor.observed_at, request_json='{}', data_json=anchor.data_json,
        content_hash='0'*64, row_count=1, chain_depth=0)
    session.add(bad)
    session.flush()
    scope = register_scope(session, dataset=anchor.dataset, subject='*', variant='*',
        selector={'scope': 'all_local'}, decoder_id=execution(session).id)
    set_paused(session, scope.id, False)
    first = start_scan(session, scope.id, 'isolate', mode='frozen')
    result = scan_page(session, first.id)
    assert result['seen'] == 3 and result['registered'] == 2 and result['failed'] == 1
    assert session.get(ScanFailure, (first.id, bad.id)).error_code
    second = start_scan(session, scope.id, 'retry-without-hiding', mode='frozen')
    result = scan_page(session, second.id)
    assert result['seen'] == 3 and result['registered'] == 0 and result['reused'] == 2
    assert result['failed'] == 1 and not result['registration_complete']


def test_campaign_retains_empty_unknown_and_channel_source_denominators(setup):
    import json
    from app.data_foundation.local_intake import register_campaign, capture_dataset, campaign_summary
    session, _ = setup
    anchor, _ = observations(session)
    anchor.dataset = 'unexpected_local_key'
    session.flush()
    campaign = register_campaign(session, event_key='all-local', decoder_id=execution(session).id)
    datasets = json.loads(campaign.datasets_json)
    assert 'unexpected_local_key' in datasets and 'anomaly_stock' in datasets
    assert 'stock_daily_dump' in datasets and 'stock_daily' in datasets
    empty = capture_dataset(session, campaign.id, 'anomaly_stock')
    scan_page(session, empty.id)
    assert capture_dataset(session, campaign.id, 'anomaly_stock').id == empty.id
    result = campaign_summary(session, campaign.id)
    assert not result['traversal_complete']
    item = next(item for item in result['items'] if item['dataset'] == 'anomaly_stock')
    assert item['fixed_versions'] == 0 and item['traversal_complete']
    channel = next(item for item in result['items'] if item['dataset'] == 'stock_daily_dump')
    assert channel['channel_target'] == 'stock_daily'
    # A same-event retry cannot expand the source-key list or change decoding.
    assert register_campaign(session, event_key='all-local', decoder_id=execution(session).id).id == campaign.id


def test_campaign_driver_restarts_and_processes_all_registered_sources(setup):
    from app.data_foundation.local_intake import register_campaign, advance, campaign_summary
    from app.data_foundation.__main__ import local_execution
    session, _ = setup
    observations(session)
    campaign = register_campaign(session, event_key='driver', decoder_id=local_execution(session, 'a'*40).id)
    campaign_id = campaign.id
    session.commit()
    assert len(advance(session.bind, campaign_id, steps=1, page_size=1)) == 1
    # A fresh invocation works entirely from durable campaign/scan state.
    advance(session.bind, campaign_id, steps=100, page_size=1)
    session.expire_all()
    result = campaign_summary(session, campaign_id)
    assert result['traversal_complete'] and result['registration_complete']
    daily = next(item for item in result['items'] if item['dataset'] == 'etf_daily')
    assert daily['registered'] == 2 and daily['fixed_versions'] == 2
    assert result['formal_publication_status'] == 'not_performed_by_intake'
