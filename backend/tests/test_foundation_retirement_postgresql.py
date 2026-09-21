"""Retirement requires a full notice window and matching historical evidence."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4
from app.data_foundation.projection import PROJECTIONS
from app.data_foundation.retirement import review_retirement
from app.data_foundation.work import create_work
from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_daily_postgresql import setup_daily, release_daily
from tests.test_foundation_evolution_postgresql import evolve
from tests.test_foundation_holdings_postgresql import service
from app.data_foundation.query import DataRequirement


def test_retirement_blocks_missing_window_references_and_history_without_deleting(session, monkeypatch):
    major = uuid4().int % 100000000 + 10
    version, replacement = f'{major}.0', f'{major}.1'
    fixture = evolve(session, setup_daily(session), version, monkeypatch, report=False)
    release = release_daily(session, fixture)
    at = datetime(2026, 9, 21, tzinfo=timezone.utc)
    dataset = 'market.bar.daily'
    initial = review_retirement(session, dataset, version, at=at)
    assert initial['status'] == 'blocked' and initial['retained_releases'] == 1
    assert initial['deletion_allowed'] is False
    assert any('30' in reason for reason in initial['blockers'])
    assert any('批准' in reason for reason in initial['blockers'])
    assert any('替代' in reason for reason in initial['blockers'])
    evolve(session, fixture, replacement, monkeypatch, report=False)
    PROJECTIONS[(dataset, replacement)]['readable_versions'] = [version, replacement]
    req = DataRequirement(dataset_id=dataset, contract_version=replacement, profile_id='default',
        semantic_series_id=json.loads(fixture[0].parameters_json)['series'], subjects=[fixture[3]],
        business_range={'from':'2026-06-05','to':'2026-06-08'}, fields=['close'], release=release.id)
    assert json.loads(service(session).query_official(req))['items']
    evidence = dict(reference_fingerprint=initial['reference_fingerprint'], replacement_version=replacement,
                    history_verification='a'*64, restore_verification='b'*64)
    def review(days=30, proof=evidence):
        return review_retirement(session, dataset, version, notice_at=at-timedelta(days=days),
            approval_reference='isolated-fixture-only', evidence=proof, at=at)
    assert review(29)['status'] == 'blocked'
    assert review()['status'] == 'ready_for_manual_review'
    assert review(proof={**evidence, 'restore_verification':None})['status'] == 'blocked'
    # A newly admitted work invalidates the reviewed reference set even though
    # its eventual values might be identical. Nothing rewrites the old release.
    origin, execution = fixture[:2]
    create_work(session, kind='A', contract_id=origin.contract_id, execution_id=execution.id,
        dependency_id=origin.dependency_id, source_ref_id=origin.source_ref_id,
        parameters=json.loads(origin.parameters_json) | {'start':'2026-06-06'})
    blocked = review()
    assert blocked['status'] == 'blocked' and blocked['unfinished_works'] == 1
    assert any('引用清单' in reason for reason in blocked['blockers'])
    assert json.loads(service(session).query_official(req))['items']
    assert blocked['deletion_allowed'] is False
