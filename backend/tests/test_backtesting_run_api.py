"""Formal/internal API boundary tests without starting a database or worker."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.backtesting.run_admission import AdmissionResult
from app.core.auth import AuthenticatedPrincipal
from app.backtesting.run_router import (
    _build_spec,
    _request_fingerprint,
    _response,
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
        backtest_config={
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "initial_cash": "10000",
        },
        strategy_revision_id=uuid4(),
        idempotency_key=key or str(uuid4()),
    )


def test_formal_request_cannot_control_run_kind_or_profile():
    with pytest.raises(ValidationError):
        RunCreateRequest(
            strategy_revision_id=uuid4(),
            idempotency_key=str(uuid4()),
            backtest_config={
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "initial_cash": "10000",
                "run_kind": "backtest_run",
            },
        )


def test_legacy_and_canonical_run_inputs_normalize_to_the_same_spec():
    revision_id = uuid4()
    account_id = uuid4()
    canonical = RunCreateRequest(
        strategy_revision_id=revision_id,
        parameters={"window": 20},
        account_profile_id=account_id,
        slippage_model={
            "key": "bps",
            "version": 1,
            "parameters": {"slippage_bps": "10", "price_tick": "0.01"},
        },
        backtest_config={
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "initial_cash": "10000",
            "dynamic_universe": True,
            "instrument_ids": [uuid4()],
            "exchanges": ["SSE"],
            "strategy_price_bases": ["raw"],
        },
        idempotency_key="same-request",
    )
    legacy = RunCreateRequest(
        strategy_revision_id=revision_id,
        account_profile_id=account_id,
        spec={
            **canonical.backtest_config.model_dump(mode="json"),
            "parameters": {"window": 20},
            "slippage_model_key": "bps",
            "slippage_model_version": 1,
            "slippage_model_parameters": {
                "slippage_bps": "10",
                "price_tick": "0.01",
            },
        },
        idempotency_key="same-request",
    )

    spec = _build_spec(canonical)
    assert spec.strategy_revision_id == revision_id
    assert spec.strategy_parameters == {"window": 20}
    assert spec.account_profile_id == account_id
    assert spec.exchanges == ("SSE",)
    assert spec.strategy_price_bases == ("raw",)
    assert spec.slippage_model.key == "bps"
    assert _request_fingerprint(canonical, "backtest_run") == _request_fingerprint(
        legacy, "backtest_run"
    )


def test_standard_idempotency_header_is_accepted():
    key = str(uuid4())
    with patch(
        "app.backtesting.run_router._service.admission",
        return_value=AdmissionResult(True),
    ):
        payload = RunCreateRequest(
            backtest_config={
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
                backtest_config={
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


def test_persisted_gate_evidence_is_projected_from_immutable_data_evidence():
    gates = {"phase1": {"allowed": True, "status": "ready"}}
    row = SimpleNamespace(
        id=uuid4(),
        run_kind="backtest_run",
        profile="formal@1",
        status="queued",
        config_hash="a" * 64,
        backtest_config={},
        formal_gate_evidence=gates,
        data_evidence={},
    )

    response = _response(row)

    assert response.formal_gates == gates


def test_internal_creation_fails_closed_without_phase_two_a_evidence():
    with patch("app.backtesting.run_router._creation.internal_capacity", 1):
        with pytest.raises(HTTPException) as raised:
            create_internal(_payload(internal=True))

    assert raised.value.status_code == 422
    detail = raised.value.detail
    assert detail["code"] == "preflight_blocked"
    assert detail["gates"]["checks"] == {"phase1": True, "phase2a": False}


def test_internal_runs_are_hidden_from_formal_list_and_need_capability():
    observed = {}
    gates = {"phase1": {"allowed": True, "status": "ready"}}

    def admit(binding, checks, **_kwargs):
        observed.update(checks)
        return AdmissionResult(True, formal_gates=gates)

    with patch(
        "app.backtesting.run_router._creation.internal_capacity",
        1,
    ), patch(
        "app.backtesting.run_router._service.admission",
        side_effect=admit,
    ):
        internal = create_internal(_payload(internal=True))
    assert observed == {"phase1": True, "phase2a": False}
    assert internal.formal_gates == gates
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

