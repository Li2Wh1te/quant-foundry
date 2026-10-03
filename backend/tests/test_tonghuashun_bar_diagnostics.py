"""Fictional failed-window evidence without supplier IO or success receipts."""

from datetime import date
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data_ingestion.clients.tonghuashun import TonghuashunError
from app.data_ingestion.models.tonghuashun import (
    TonghuashunCollectionState as State, TonghuashunObservation as Observation,
)
from app.data_ingestion.tonghuashun.acquisition import Acquisition
from app.data_ingestion.tonghuashun.contracts import (
    CollectionError, CollectionParameters, DATASETS, exact_json, provider_date,
)
from app.data_ingestion.tonghuashun.repository import CollectionRepository
from app.data_ingestion.tonghuashun import service
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
