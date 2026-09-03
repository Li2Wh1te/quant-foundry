"""Formal/internal API boundary tests without starting a database or worker."""

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.backtesting.run_admission import AdmissionResult
from app.core.auth import AuthenticatedPrincipal
from app.backtesting.run_router import (
    _runs,
    cancel,
    create_formal,
    create_internal,
    get_run,
    get_internal_run,
    list_runs,
    require_internal_capability,
)
from app.backtesting.run_schemas import InternalRunCreateRequest, RunCreateRequest


def _payload(*, internal: bool = False, key: str | None = None):
    cls = InternalRunCreateRequest if internal else RunCreateRequest
    return cls(
        spec={
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "initial_cash": "10000",
        },
        strategy_revision_id=uuid4(),
        idempotency_key=key or str(uuid4()),
    )


def test_formal_request_cannot_control_run_kind_or_profile():
    payload = _payload()
    payload.spec["run_kind"] = "backtest_run"
    with pytest.raises(HTTPException) as raised:
        create_formal(payload)
    assert raised.value.status_code == 422


def test_standard_idempotency_header_is_accepted():
    key = str(uuid4())
    with patch(
        "app.backtesting.run_router._service.admission",
        return_value=AdmissionResult(True),
    ):
        payload = RunCreateRequest(
            spec={
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "initial_cash": "10000",
            },
            strategy_revision_id=uuid4(),
            idempotency_key=None,
        )
        created = create_formal(payload, idempotency_header=key)
    assert created.run_id


def test_owner_scope_is_derived_from_authenticated_principal():
    key = str(uuid4())
    request_a = Request({"type": "http", "headers": []})
    request_a.state.authenticated_principal = AuthenticatedPrincipal("owner-a")
    request_b = Request({"type": "http", "headers": []})
    request_b.state.authenticated_principal = AuthenticatedPrincipal("owner-b")
    with patch(
        "app.backtesting.run_router._service.admission",
        return_value=AdmissionResult(True),
    ):
        owner_a = create_formal(_payload(key=key), request=request_a)
        owner_b = create_formal(_payload(key=key), request=request_b)
    assert owner_a.run_id != owner_b.run_id
    with pytest.raises(HTTPException) as raised:
        get_run(str(owner_a.run_id), request=request_b)
    assert raised.value.status_code == 404


def test_formal_create_is_idempotent_and_cancel_only_records_request():
    key = str(uuid4())
    with patch(
        "app.backtesting.run_router._service.admission",
        return_value=AdmissionResult(True),
    ):
        first = create_formal(_payload(key=key))
        retry = create_formal(
            RunCreateRequest(
                spec={
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-02",
                    "initial_cash": "10000",
                },
                strategy_revision_id=first.strategy_revision_id,
                idempotency_key=key,
            )
        )
    assert retry.run_id == first.run_id
    assert cancel(str(first.run_id)).status == "cancel_requested"
    assert get_run(str(first.run_id)).status == "cancel_requested"


def test_internal_runs_are_hidden_from_formal_list_and_need_capability():
    with patch(
        "app.backtesting.run_router._creation.internal_capacity",
        1,
    ), patch(
        "app.backtesting.run_router._service.admission",
        return_value=AdmissionResult(True),
    ):
        internal = create_internal(_payload(internal=True))
    assert all(item.run_kind == "backtest_run" for item in list_runs()["items"])
    with pytest.raises(HTTPException) as raised:
        get_run(str(internal.run_id))
    assert raised.value.status_code == 404
    assert require_internal_capability("operator") == "operator"
    internal_detail = get_internal_run(str(internal.run_id))
    assert internal_detail["visibility"] == "internal"


def teardown_module():
    # Direct-call compatibility state is process-local and must not leak into
    # unrelated test modules.
    _runs.clear()

