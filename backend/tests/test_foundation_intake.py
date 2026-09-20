"""Finite reconciliation must recover late IDs and roll back failed pages."""
from uuid import UUID
import pytest
from sqlalchemy import select
from app.data_foundation.intake import register_scope, set_paused, start_scan, scan_page
from app.data_foundation.intake_models import IntakeItem, IntakeScope, Scan, ScanEntry, SourcePointer, IntakeControl
from app.data_foundation.models import SourceRef
from app.data_ingestion.models.tonghuashun import TonghuashunObservation as Observation, TonghuashunCollectionState as State
from tests.test_foundation_sources import session, execution, observations


@pytest.fixture
def setup(session):
    for model in (IntakeScope, IntakeControl, Scan, IntakeItem, ScanEntry, SourcePointer, State):
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
