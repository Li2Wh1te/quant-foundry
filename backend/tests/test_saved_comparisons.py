"""Owned definition CRUD and validation without executing any backtest."""
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.backtesting.saved_comparisons import ComparisonDefinition, ComparisonStore, CreateComparison, SavedComparison, SavedComparisonMember, UpdateComparison
from app.db.base import Base


@pytest.fixture
def stores():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[SavedComparison.__table__, SavedComparisonMember.__table__])
    with Session(engine) as session:
        yield ComparisonStore(session, "owner-a"), ComparisonStore(session, "owner-b")
    engine.dispose()


def definition(**changes):
    ids = [uuid4(), uuid4()]
    return dict(name="参数优化比较", run_ids=ids, baseline_run_id=ids[0], **changes)


def test_definition_validation():
    valid = definition()
    assert ComparisonDefinition(**{**valid, "name": "  参数优化  "}).name == "参数优化"
    for changes in [{"name": " "}, {"run_ids": valid["run_ids"] * 2}, {"run_ids": [valid["run_ids"][0]]}, {"baseline_run_id": uuid4()}, {"run_ids": [uuid4() for _ in range(11)]}, {"owner_scope": "other"}]:
        with pytest.raises(ValidationError):
            ComparisonDefinition(**{**valid, **changes})
    with pytest.raises(ValidationError):
        UpdateComparison(version=1, name=None)


def test_crud_order_baseline_isolation_and_concurrency(stores):
    a, b = stores
    payload = CreateComparison(**definition())
    with patch.object(a, "check_runs") as checks:
        first = a.create(payload)
        assert first.version == 1 and first.run_ids == payload.run_ids
        assert a.create(payload).id == first.id
        assert checks.call_count == 1
        with pytest.raises(HTTPException) as error:
            a.create(payload.model_copy(update={"name": "changed retry"}))
        assert error.value.status_code == 409
        assert b.page(20, 0).total == 0
        for operation in [lambda: b.get(first.id), lambda: b.update(first.id, UpdateComparison(version=1, name="stolen")), lambda: b.remove(first.id, 1), lambda: b.create(payload)]:
            with pytest.raises(HTTPException) as error:
                operation()
            assert error.value.status_code == 404
        updated = a.update(first.id, UpdateComparison(version=1, name="新名称", run_ids=list(reversed(first.run_ids)), baseline_run_id=first.run_ids[1]))
        assert updated.name == "新名称" and updated.version == 2
        assert updated.run_ids == list(reversed(first.run_ids)) and updated.baseline_run_id == first.run_ids[1]
        for operation in [lambda: a.update(first.id, UpdateComparison(version=1, name="stale")), lambda: a.remove(first.id, 1)]:
            with pytest.raises(HTTPException) as error:
                operation()
            assert error.value.status_code == 409
        with pytest.raises(HTTPException) as error:
            a.update(first.id, UpdateComparison(version=2, baseline_run_id=uuid4()))
        assert error.value.status_code == 422
        assert a.page(1, 0).total == 1 and not a.page(1, 1).items
        a.remove(first.id, 2)
        assert a.page(20, 0).total == 0
        assert a.session.query(SavedComparisonMember).count() == 0


@pytest.mark.parametrize("changes,code", [({"status": "failed"}, 409), ({"terminal_status": "indeterminate"}, 409), ({"run_kind": "internal_link_acceptance"}, 404), ({"profile": "internal_link_acceptance@1"}, 404)])
def test_only_completed_formal_runs_are_saved(changes, code):
    root = dict(status="succeeded", terminal_status="succeeded", run_kind="backtest_run", profile="formal@1")
    session = Mock()
    session.scalars.return_value = [SimpleNamespace(**root), SimpleNamespace(**{**root, **changes})]
    with pytest.raises(HTTPException) as error:
        ComparisonStore(session, "owner").check_runs([uuid4(), uuid4()])
    assert error.value.status_code == code
    assert "idempotency_scope" in str(session.scalars.call_args.args[0])
