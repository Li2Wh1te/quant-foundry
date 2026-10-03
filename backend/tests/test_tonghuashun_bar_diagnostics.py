"""Fictional failed-window evidence without supplier IO or success receipts."""

from datetime import date
from unittest.mock import Mock
from uuid import UUID

import pytest
import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_ingestion.clients.tonghuashun import TonghuashunError
from app.data_ingestion.models.tonghuashun import (
    TonghuashunCollectionState as State, TonghuashunObservation as Observation,
    TonghuashunWorkUnit,
)
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.contracts import (
    CollectionError, CollectionParameters, DATASETS, date_ms, exact_json, provider_date,
)
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun import service
from app.scheduling.registry import task_registry
from tests.test_tonghuashun_collections import NOW, bar, engine, reply, seed, ticker


@pytest.mark.parametrize("code", [1001, 1002, 1003, 1004])
def test_index_failure_logs_exact_native_request_and_business_code(engine, monkeypatch, code):
    # This invented catalogue identity intentionally has a non-numeric native
    # code. A diagnostic must retain it, never truncate it or try another code.
    subject = "900000CNY01.SH"
    seed(engine, [ticker(subject, "a-share-index")])
    client = Mock(interval_ms=0)
    client.request.side_effect = TonghuashunError("invalid_parameters", business_code=code)
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    with pytest.raises(CollectionError):
        service.collect("index_daily", CollectionParameters(subjects=[subject]), client, engine, now=NOW)
    event = log.warning.call_args.kwargs
    request = client.request.call_args.args[1]
    assert event["failure_request"] == {
        "interface": DATASETS["index_daily"].interface,
        "parameters": request, "stage": "request",
    }
    assert event["supplier_business_code"] == code
    assert event["start_date"] == provider_date(request["start"]).isoformat()
    assert event["end_date"] == provider_date(request["end"]).isoformat()
    assert event["start_date"] in event["message"] and event["end_date"] in event["message"]
    assert event["checkpoint_advanced"] is False and event["failed_count"] == 1
    assert client.request.call_count == 1
    with Session(engine) as session:
        state = session.get(State, ("index_daily", subject, "default"))
        assert state.status == "failed" and state.error_kind == "invalid_parameters"
        assert state.observation_id is None and state.succeeded_at is None
        assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_etf_unsupported_has_no_success_receipt_or_symbol_fallback(engine, monkeypatch):
    seed(engine, [ticker()])
    client = Mock(interval_ms=0)
    client.request.side_effect = TonghuashunError("unsupported", business_code=3004)
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    with pytest.raises(CollectionError):
        service.collect("etf_daily", CollectionParameters(subjects=["510300.SH"]), client, engine, now=NOW)
    event = log.warning.call_args.kwargs
    assert event["failure_request"]["parameters"] == client.request.call_args.args[1]
    assert event["failure_request"]["stage"] == "request"
    assert event["supplier_business_code"] == 3004
    assert client.request.call_count == 1
    acquisition = Acquisition(client)
    with pytest.raises(TonghuashunError):
        acquisition.fetch(DATASETS["etf_daily"], "510300.SH", CollectionParameters(), None, NOW)
    assert acquisition.requests == []


@pytest.mark.parametrize("malformed", ["value", "subject", "date"])
def test_invalid_bar_response_logs_validation_window_without_payload(engine, monkeypatch, malformed):
    seed(engine, [ticker()])
    secret = "reflected-credential-must-not-enter-diagnostics"
    row = bar(date(2026, 9, 1))
    metadata = {"extra": secret, "api_key": secret}
    if malformed == "value":
        row["volume"] = secret
    elif malformed == "subject":
        metadata["thscode"] = secret
    else:
        row = bar(date(1900, 1, 1))
    client = Mock(interval_ms=0)
    client._api_key = secret
    client._api_url = "https://provider.invalid/" + secret
    client.request.return_value = reply([row], **metadata)
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    parameters = CollectionParameters(subjects=["510300.SH"], start_date=date(2026, 8, 1), end_date=date(2026, 9, 10))
    with pytest.raises(CollectionError):
        service.collect("etf_daily", parameters, client, engine, now=NOW)
    event = log.warning.call_args.kwargs
    assert event["failure_request"]["stage"] == "response_validation"
    assert set(event["failure_request"]["parameters"]) == {"thscode", "interval", "start", "end"}
    assert event["supplier_business_code"] is None
    assert secret not in exact_json(event)
    assert event["error_type"] == "invalid_data" and event["checkpoint_advanced"] is False
    with Session(engine) as session:
        state = session.get(State, ("etf_daily", "510300.SH", "2026-08-01_2026-09-10"))
        assert state.status == "failed" and state.observation_id is None
        assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_second_window_failure_retains_last_good_observation_and_exact_window(engine, monkeypatch):
    seed(engine, [ticker()])
    with Session(engine) as session:
        result = CollectionRepository(session).publish("etf_daily", "510300.SH", "default", expected=0,
            data={"item": [bar(date(2020, 1, 2))]}, requests=[], now=NOW)
        session.commit()
        original = session.get(Observation, UUID(result["version_id"]))
        original_bytes = (original.data_json, original.request_json)
    client = Mock(interval_ms=0)
    client.request.side_effect = [reply([bar(date(2020, 1, 2), "2")]), TonghuashunError("unsupported", business_code=3004)]
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    with pytest.raises(CollectionError):
        service.collect("etf_daily", CollectionParameters(mode="reconcile", subjects=["510300.SH"]), client, engine, now=NOW)
    event = log.warning.call_args.kwargs
    assert event["failure_request"]["parameters"] == client.request.call_args_list[1].args[1]
    assert event["failure_request"]["parameters"] != client.request.call_args_list[0].args[1]
    assert event["fetched_count"] == 1 and client.request.call_count == 2
    assert event["start_date"] == provider_date(client.request.call_args_list[1].args[1]["start"]).isoformat()
    with Session(engine) as session:
        state = session.get(State, ("etf_daily", "510300.SH", "default"))
        assert state.status == "failed" and str(state.observation_id) == result["version_id"]
        assert state.succeeded_at.replace(tzinfo=NOW.tzinfo) == NOW
        original = session.get(Observation, state.observation_id)
        assert (original.data_json, original.request_json) == original_bytes
        assert session.scalar(select(func.count()).select_from(Observation)) == 1


def test_empty_index_history_stays_failed_and_reports_final_validation(engine, monkeypatch):
    seed(engine, [ticker("000300.SH", "a-share-index")])
    client = Mock(interval_ms=0)
    client.request.return_value = reply([])
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    with pytest.raises(CollectionError):
        service.collect("index_daily", CollectionParameters(subjects=["000300.SH"]), client, engine, now=NOW)
    event = log.warning.call_args.kwargs
    assert event["failure_request"]["stage"] == "history_validation"
    assert event["failure_request"]["parameters"] == client.request.call_args.args[1]
    assert event["failure_request"]["validation_range"] == {
        "start_date": "2016-09-14", "end_date": "2026-09-14"}
    assert event["start_date"] == "2016-09-14" and event["end_date"] == "2026-09-14"
    assert event["error_type"] == "invalid_data" and event["supplier_business_code"] is None
    assert client.request.call_count == 13
    with Session(engine) as session:
        state = session.get(State, ("index_daily", "000300.SH", "default"))
        assert state.status == "failed" and state.observation_id is None and state.succeeded_at is None
        assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_request_diagnostic_snapshots_only_generated_parameters():
    client = Mock(interval_ms=0)
    issued = []

    def request(interface, parameters):
        issued.append(dict(parameters))
        # A client-side mutation after invocation must not add reflected
        # credentials or rewrite the diagnostic of the generated request.
        parameters["api_key"] = "fictional-secret"
        parameters["start"] = "fictional-secret"
        raise TonghuashunError("invalid_parameters", business_code=1002)

    client.request.side_effect = request
    acquisition = Acquisition(client)
    with pytest.raises(TonghuashunError):
        acquisition.fetch(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, NOW)
    assert acquisition.bar_request_context["parameters"] == issued[0]
    assert "fictional-secret" not in exact_json(acquisition.bar_request_context)
    assert acquisition.requests == []


def test_successful_acquisition_clears_failure_context_and_keeps_real_receipts():
    client = Mock(interval_ms=0)
    acquisition = Acquisition(client)
    client.request.side_effect = TonghuashunError("invalid_parameters", business_code=1002)
    with pytest.raises(TonghuashunError):
        acquisition.fetch(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, NOW)
    assert acquisition.bar_request_context is not None and acquisition.requests == []
    client.request.side_effect = None
    client.request.return_value = reply([bar(date(2026, 9, 1))])
    result = acquisition.fetch(DATASETS["index_daily"], "000300.SH",
        CollectionParameters(start_date=date(2026, 8, 1), end_date=date(2026, 9, 10)), None, NOW)
    assert result["item"] == [bar(date(2026, 9, 1))]
    assert acquisition.bar_request_context is None and len(acquisition.requests) == 1
    assert acquisition.requests[0]["key_receipt"] == "actual_returned_keys_v1"
    assert set(acquisition.requests[0]) == {"interface", "parameters", "request_id", "key_receipt", "returned_keys"}


def diagnostic_context(subject="000300.SH", stage="request"):
    return {"interface": DATASETS["index_daily"].interface, "stage": stage,
            "parameters": {"thscode": subject, "interval": "1d", "start": 1756656000000, "end": 1756828800000}}


@pytest.mark.parametrize("code", [9999, True, "fictional-secret", 2001])
def test_failure_log_sink_whitelists_fields_and_rechecks_business_code(monkeypatch, code):
    secret = "fictional-secret-" * 1000
    context = diagnostic_context(stage="history_validation")
    context.update(api_key=secret, response={"payload": secret}, request_id=secret, headers={"X-api-key": secret})
    context["parameters"].update(api_key=secret, url=secret, adjust=secret)
    context["validation_range"] = {"start_date": "2025-09-01", "end_date": "2025-09-03", "response": secret}
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    service.log_result(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, {}, False,
                       "invalid_parameters", failure_request=context, supplier_business_code=code)
    event = log.warning.call_args.kwargs
    safe = event["failure_request"]
    assert set(safe) == {"interface", "parameters", "stage", "validation_range"}
    assert set(safe["parameters"]) == {"thscode", "interval", "start", "end"}
    assert set(safe["validation_range"]) == {"start_date", "end_date"}
    assert event["supplier_business_code"] is None
    assert secret not in exact_json(event) and "fictional-secret" not in exact_json(event)
    assert len(exact_json(safe).encode("utf-8")) <= 512
    assert len(safe["parameters"]["thscode"]) <= 64
    assert all(len(value) == 10 for value in safe["validation_range"].values())
    assert event["error_type"] == "invalid_parameters" and event["checkpoint_advanced"] is False


@pytest.mark.parametrize("change", [
    {"interface": "x" * 4096}, {"stage": "x" * 4096}, {"parameters": []},
    {"parameters": {"thscode": "000300.SH", "interval": "1d", "start": True, "end": 1756828800000}},
    {"parameters": {"thscode": "000300.SH", "interval": "1d", "start": 10 ** 100, "end": 10 ** 101}},
    {"parameters": {"thscode": "000300.SH", "interval": "1d", "start": 1756828800000, "end": 1756656000000}},
    {"stage": "history_validation", "validation_range": {"start_date": "x" * 4096, "end_date": "2025-09-03"}},
    {"stage": "history_validation", "validation_range": {"start_date": "2025-02-30", "end_date": "2025-09-03"}},
    {"stage": "history_validation", "validation_range": {"start_date": "2025-09-02", "end_date": "2025-09-03"}},
])
def test_malformed_failure_context_is_omitted_without_changing_failure(monkeypatch, change):
    context = {**diagnostic_context(), **change}
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    service.log_result(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, {}, False,
                       "invalid_data", failure_request=context)
    event = log.warning.call_args.kwargs
    assert "failure_request" not in event and event["failure_request_omitted"] is True
    assert event["error_type"] == "invalid_data" and event["failed_count"] == 1
    assert event["checkpoint_advanced"] is False
    assert len(exact_json(event).encode("utf-8")) < 4096


def test_native_identity_limit_rejects_instead_of_truncating(monkeypatch):
    maximum = "A" * 61 + ".SH"
    assert service.bounded_bar_failure(DATASETS["index_daily"], maximum, diagnostic_context(maximum)) is not None
    oversized = "A" * 62 + ".SH"
    assert service.bounded_bar_failure(DATASETS["index_daily"], oversized, diagnostic_context(oversized)) is None
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    service.log_result(DATASETS["index_daily"], oversized, CollectionParameters(), None, {}, False,
                       "invalid_parameters", failure_request=diagnostic_context(oversized))
    event = log.warning.call_args.kwargs
    assert event["subject"] == "无效标的（已省略）" and oversized not in exact_json(event)
    assert event["failure_request_omitted"] is True and event["checkpoint_advanced"] is False


def test_failure_text_and_trace_are_bounded_without_rendering_provider_exception_chain(monkeypatch):
    secret = "fictional-reflected-key-" * 1000
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    try:
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            raise TonghuashunError("invalid_parameters", business_code=1002) from None
    except TonghuashunError as exc:
        service.log_result(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, {}, False,
                           exc.kind, error_message=str(exc) + "本地说明" * 1000,
                           failure_request=diagnostic_context(), supplier_business_code=exc.business_code)
        event = log.warning.call_args.kwargs
        rendered = structlog.processors.format_exc_info(None, "warning", dict(event))
    assert len(event["error_message"]) == 512 and len(event["exception"]) <= 4096
    assert event["exception"] and event["exc_info"] is False
    assert "TonghuashunError" in event["exception"] and "RuntimeError" not in event["exception"]
    assert secret not in exact_json(rendered)
    assert event["supplier_business_code"] == 1002 and event["checkpoint_advanced"] is False


def test_oversized_call_stack_is_bounded_independently_of_error_text(monkeypatch):
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    monkeypatch.setattr(service.traceback, "format_tb", Mock(return_value=["fixed-local-frame\n" * 1000]))
    service.log_result(DATASETS["index_daily"], "000300.SH", CollectionParameters(), None, {}, False,
                       "invalid_data", error_message="历史行情为空，尚不能确认该范围覆盖。",
                       failure_request=diagnostic_context())
    event = log.warning.call_args.kwargs
    assert len(event["exception"]) == 4096 and event["exc_info"] is False
    assert event["error_message"] == "历史行情为空，尚不能确认该范围覆盖。"


@pytest.mark.parametrize("dataset,subject,kind", [
    ("index_daily", "900000CNY01.SH", "invalid_parameters"),
    ("etf_daily", "510300.SH", "unsupported"),
    ("index_daily", "000300.SH", "invalid_data"),
])
def test_formal_single_scope_handler_uses_one_read_and_persists_no_failed_payload(engine, monkeypatch, dataset, subject, kind):
    from app.data_ingestion.clients.tonghuashun import TonghuashunClient
    from app.data_ingestion.scheduler_tasks import tonghuashun as scheduler

    # Invoke the registered collector, while replacing both configuration and
    # transport before execution. All writes belong to this fixture's database;
    # no production settings, credentials, scheduler or provider is accessed.
    TonghuashunWorkUnit.__table__.create(engine)
    asset = "fund-etf" if dataset == "etf_daily" else "a-share-index"
    seed(engine, [ticker(subject, asset)])
    client = Mock(interval_ms=0)
    secret = "fictional-response-secret"
    if kind == "invalid_data":
        row = bar(date(2026, 9, 2))
        row["volume"] = secret
        client.request.return_value = reply([row], api_key=secret, payload=secret)
    else:
        client.request.side_effect = TonghuashunError(kind, business_code=1002 if kind == "invalid_parameters" else 3004)
    monkeypatch.setattr(scheduler, "get_engine", lambda: engine)
    monkeypatch.setattr(scheduler, "get_settings", lambda: Mock())
    monkeypatch.setattr(TonghuashunClient, "from_settings", lambda settings: client)
    log = Mock()
    monkeypatch.setattr(service, "logger", log)
    definition = task_registry.require(f"data.ths.{dataset}")
    parameters = definition.parameters_model.model_validate({
        "subjects": [subject], "asset_types": [asset], "mode": "incremental", "refresh_today": False,
        "start_date": "2026-09-01", "end_date": "2026-09-03", "batch_size": 1,
        "max_requests": 1, "max_seconds": 10,
    })
    with pytest.raises(CollectionError):
        definition.handler(Mock(run_id=None), parameters)
    assert client.request.call_count == 1
    assert client.request.call_args.args == (DATASETS[dataset].interface, {
        "thscode": subject, "interval": "1d", "start": date_ms(date(2026, 9, 1)), "end": date_ms(date(2026, 9, 3)),
    })
    event = log.warning.call_args.kwargs
    assert secret not in exact_json(event) and event["error_type"] == kind
    assert event["checkpoint_advanced"] is False
    with Session(engine) as session:
        state = session.get(State, (dataset, subject, "2026-09-01_2026-09-03"))
        assert state.status == "failed" and state.observation_id is None and state.succeeded_at is None
        assert session.scalar(select(func.count()).select_from(Observation)) == 0
        assert session.scalar(select(func.count()).select_from(TonghuashunWorkUnit)) == 0
