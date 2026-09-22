"""Bounded governance pages must not rederive an immutable complete plan."""
import json
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tests.test_foundation_publication_postgresql import session, pytestmark
from tests.test_foundation_records_postgresql import setup_records
from app.data_foundation import record_work
from app.data_foundation.work import claim, create_work
from app.data_foundation.work_models import CandidateManifest
from app.data_foundation.record_models import RecordPlanVerification
from app.data_foundation.canonical import FoundationError


def test_pages_reuse_derivation_and_explicit_audit_rederives(session, monkeypatch):
    origin, execution, policy = setup_records(session, [dict(ts_code=f'P{i}', csname='Plan') for i in range(210)])
    original = record_work.plan_actions
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(record_work, 'plan_actions', counted)
    work = record_work.create_governance(session, normalization_id=origin.id,
        execution_id=execution.id, policy_id=policy.id)
    assert len(calls) == 1
    lease = claim(session, work_id=work.id)
    assert record_work.stage_decisions(session, work.id, lease.lease_epoch) is None
    lease = claim(session, work_id=work.id)
    assert record_work.stage_decisions(session, work.id, lease.lease_epoch).status == 'sealed'
    assert work.cursor == 210 and len(calls) == 1
    record_work.verify_governance_plan(session, work, force=True)
    assert len(calls) == 2
    monkeypatch.setattr(record_work, 'validation_hash', lambda: 'f'*64)
    record_work.verify_governance_plan(session, work)
    assert len(calls) == 3


def test_generic_work_cannot_bypass_plan_derivation(session):
    origin, execution, policy = setup_records(session)
    manifest = session.scalar(select(CandidateManifest).where(CandidateManifest.work_id == origin.id))
    actions = record_work.plan_actions(session, manifest.id, policy.id)
    actions[0]['reason'] = 'UNVERIFIED_PLAN_OVERRIDE'
    work = create_work(session, kind='B', contract_id=origin.contract_id, execution_id=execution.id,
        dependency_id=origin.dependency_id, candidate_manifest_id=manifest.id, policy_id=policy.id,
        parameters={**json.loads(origin.parameters_json), 'actions':actions})
    lease = claim(session, work_id=work.id)
    with pytest.raises(FoundationError, match='固定输入不一致'):
        record_work.stage_decisions(session, work.id, lease.lease_epoch)
    assert session.scalar(select(RecordPlanVerification).where(RecordPlanVerification.work_id == work.id)) is None


def test_plan_receipt_and_underlying_parameters_are_immutable(session):
    origin, execution, policy = setup_records(session)
    work = record_work.create_governance(session, normalization_id=origin.id,
        execution_id=execution.id, policy_id=policy.id)
    for statement in (
        'UPDATE foundation_record_plan_verifications SET action_count=0 WHERE work_id=:id',
        'DELETE FROM foundation_record_plan_verifications WHERE work_id=:id',
        "UPDATE foundation_work SET parameters_json='{}' WHERE id=:id",
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(statement), {'id':work.id})
