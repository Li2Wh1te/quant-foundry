"""Bounded collector tests use invented source identities and no supplier IO."""
from datetime import date
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.data_ingestion.clients.tonghuashun import TonghuashunClient, TonghuashunError
from app.data_ingestion.models.tonghuashun import (
    TonghuashunTicker, TonghuashunObservation, TonghuashunCollectionState,
    TonghuashunWorkUnit,
)
from app.data_sources.models import DataSourceConfig
from app.data_ingestion.tonghuashun import bounded
from tests.test_tonghuashun_collections import NOW, bar, reply, seed, ticker
from app.data_ingestion.scheduler_tasks import tonghuashun as scheduler


@pytest.fixture
def scope(request):
    dataset = getattr(request, "param", "index_daily")
    return bounded.Scope(dataset, "510777.SH" if dataset == "etf_daily" else "900000CNY01.SH", date(2026, 9, 1), date(2026, 9, 3))


@pytest.fixture
def fixture_engine(scope, tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "source-fixture.sqlite"))
    for table in (DataSourceConfig.__table__, TonghuashunTicker.__table__,
                  TonghuashunObservation.__table__, TonghuashunCollectionState.__table__, TonghuashunWorkUnit.__table__):
        table.create(engine)
    # Admission queries only the immutable parameters of active task runs.
    # This fixture avoids SQLite emulation of the unrelated PostgreSQL schema.
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE task_runs (task_type TEXT, status TEXT, parameters JSON)"))
    with Session(engine) as session:
        session.add(DataSourceConfig(key="tonghuashun", initialized=True, enabled=True,
                                     encrypted_secrets="fixture-ciphertext", values={}, version=7))
        session.commit()
    seed(engine, [ticker(scope.subject, scope.asset_type)])
    yield engine
    engine.dispose()


def test_scope_keeps_exact_native_identity_and_fixed_registered_budget(scope):
    parameters = scope.parameters()
    assert parameters.subjects == ["900000CNY01.SH"]
    assert (parameters.batch_size, parameters.max_requests, parameters.max_seconds) == (1, 1, 180)
    assert scope.variant == "2026-09-01_2026-09-03"
    for invalid in (bounded.Scope("stock_daily", scope.subject, scope.start_date, scope.end_date),
                    bounded.Scope(scope.dataset, "900000CNY01", scope.start_date, scope.end_date),
                    bounded.Scope(scope.dataset, scope.subject, scope.end_date, scope.start_date)):
        with pytest.raises(ValueError):
            invalid.parameters()


@pytest.mark.parametrize("problem", ["disabled", "unconfigured", "wrong_asset", "raw_identity", "variant", "active", "broad", "checkpoint"])
def test_admission_blocks_without_creating_a_successful_state(fixture_engine, scope, problem):
    with Session(fixture_engine) as session:
        source = session.get(DataSourceConfig, "tonghuashun")
        native = session.get(TonghuashunTicker, scope.subject)
        if problem == "disabled":
            source.enabled = False
        elif problem == "unconfigured":
            source.encrypted_secrets = None
        elif problem == "wrong_asset":
            native.asset_type = "fund-etf"
        elif problem == "raw_identity":
            native.raw_json = '{"thscode":"different.SH","asset_type":"a-share-index"}'
        elif problem == "variant":
            session.add(TonghuashunCollectionState(dataset=scope.dataset, subject=scope.subject,
                variant=scope.variant, revision=1, status="failed", attempted_at=NOW))
        elif problem in ("active", "broad"):
            parameters = {"subjects": [scope.subject]} if problem == "active" else {}
            parameters.update(start_date=scope.start_date.isoformat(), end_date=scope.end_date.isoformat())
            session.execute(text("INSERT INTO task_runs VALUES (:type, 'running', :parameters)"),
                            {"type": scope.task_type, "parameters": json.dumps(parameters)})
        elif problem == "checkpoint":
            session.add(TonghuashunWorkUnit(scope=bounded._scope_hash(scope), request_key="fixture",
                                           data_json="{}", created_at=NOW))
        session.commit()
    with pytest.raises(bounded.AdmissionRejected):
        with bounded.admission(fixture_engine, scope):
            pytest.fail("blocked scope entered the collector")
    with Session(fixture_engine) as session:
        assert session.scalar(select(TonghuashunObservation.id)) is None


def test_admission_releases_source_transaction_before_handler(fixture_engine, scope):
    with bounded.admission(fixture_engine, scope) as proof:
        assert proof["source_version"] == 7 and proof["new_explicit_variant"] is True
        assert len(proof["native_identity_sha256"]) == 64
        with Session(fixture_engine) as session:
            assert session.get(TonghuashunCollectionState, (scope.dataset, scope.subject, scope.variant)) is None


def test_admission_keeps_default_and_other_explicit_variants_independent(fixture_engine, scope):
    with fixture_engine.begin() as connection:
        for parameters in ({"subjects": [scope.subject]}, {"subjects": [scope.subject],
            "start_date": "2025-09-01", "end_date": "2025-09-03"}):
            connection.execute(text("INSERT INTO task_runs VALUES (:type, 'running', :parameters)"),
                {"type": scope.task_type, "parameters": json.dumps(parameters)})
    with bounded.admission(fixture_engine, scope):
        pass


def test_admission_refuses_excess_enumeration(fixture_engine, scope):
    with fixture_engine.begin() as connection:
        connection.execute(text("INSERT INTO task_runs VALUES (:type, 'queued', '{}')"),
                           [{"type": scope.task_type} for _ in range(201)])
    with pytest.raises(bounded.AdmissionRejected, match="active_run_limit"):
        with bounded.admission(fixture_engine, scope):
            pytest.fail("unbounded active-run enumeration admitted")


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("scope", ["index_daily", "etf_daily"], indirect=True)
def test_worker_uses_formal_handler_and_only_sanitized_actual_evidence(fixture_engine, scope, tmp_path, monkeypatch, failure):
    from app.db import session as database
    secret = "fictional-provider-secret-must-not-be-persisted"
    client = Mock(interval_ms=0)
    client.request.return_value = reply([bar(date(2026, 9, 2))], raw=secret)
    if failure:
        client.request.side_effect = TonghuashunError("invalid_parameters", business_code=1002)
    monkeypatch.setattr(database, "get_engine", lambda: fixture_engine)
    monkeypatch.setattr(scheduler, "get_engine", lambda: fixture_engine)
    monkeypatch.setattr(scheduler, "get_settings", lambda: Mock())
    monkeypatch.setattr(TonghuashunClient, "from_settings", lambda settings: client)
    operation_id = str(uuid4())
    code = bounded.run_worker(scope, tmp_path, operation_id, time.monotonic() + 20)
    record = json.loads((tmp_path / "worker.json").read_text())
    assert record["operation_id"] == operation_id and record["phase"] == "terminal"
    assert record["outcome"] == ("failed" if failure else "succeeded")
    assert code == (2 if failure else 0)
    assert secret not in json.dumps(record)
    assert client.request.call_count == 1
    assert record["counts"] == {"logical_requests": 1, "http_attempts": 0, "reused": 0}
    assert record["counts_complete"] is True
    assert record["publication"]["status"] == ("none" if failure else "verified")
    assert record["unknown_publication"] is False
    assert scope.subject in record["message"] and "checkpoint" in record["message"]
    if failure:
        assert record["diagnostic"]["error_kind"] == "invalid_parameters"
        assert record["diagnostic"]["supplier_business_code"] == 1002
        assert record["diagnostic"]["failure_stage"] == "request"
        assert record["diagnostic"]["failure_request"]["parameters"] == client.request.call_args.args[1]
        assert "请求参数不符合" in record["diagnostic"]["reason"]
    with Session(fixture_engine) as session:
        assert session.get(TonghuashunCollectionState, (scope.dataset, scope.subject, "default")) is None
        state = session.get(TonghuashunCollectionState, (scope.dataset, scope.subject, scope.variant))
        assert (state.observation_id is None) is failure


def test_response_evidence_drops_payload_values_urls_and_unknown_shapes():
    limit = Mock(response_evidence={"digest": "a" * 64, "row_count": 3, "shape": "reflected-secret",
                                    "api_key": "secret", "item": [{"close_price": "secret"}]})
    assert bounded._response_evidence(limit) == {"digest": "a" * 64, "row_count": 3}


def test_response_numeric_projection_retains_bad_values_but_not_reflected_strings():
    limit = Mock(response_evidence={"numeric_samples": [{"shape": "object", "numeric_fields": {
        "volume": {"type": "int", "value_text": "-123"},
        "turnover": {"type": "decimal", "value_text": "-0.100000000000000000001"},
        "open_price": {"type": "decimal", "value_text": "fictional-secret"},
        "close_price": {"type": "other", "value_text": "fictional-secret"},
        "api_key": {"type": "int", "value_text": "123"}}}]})
    record = bounded._response_evidence(limit)
    fields = record["numeric_samples"][0]["numeric_fields"]
    assert fields["volume"]["value_text"] == "-123"
    assert fields["turnover"]["value_text"] == "-0.100000000000000000001"
    assert fields["open_price"] == {"type": "decimal", "value_omitted": True}
    assert fields["close_price"] == {"type": "other", "value_omitted": True}
    assert "api_key" not in fields and "fictional-secret" not in json.dumps(record)


def test_worker_cannot_claim_another_publication_as_its_completed_scope(fixture_engine, scope, tmp_path, monkeypatch):
    from dataclasses import replace
    from app.db import session as database
    from app.scheduling.registry import task_registry
    from app.data_ingestion.tonghuashun.repository import CollectionRepository
    def conflicting_handler(context, parameters):
        # Simulate a competing collector winning CAS. The formal collector's
        # conflict outcome reports skipped, not a success owned by this attempt.
        with Session(fixture_engine) as session:
            CollectionRepository(session).publish(scope.dataset, scope.subject, scope.variant,
                expected=0, data={"item": [bar(date(2026, 9, 2))]}, requests=[], now=NOW)
            session.commit()
        return {"succeeded": 0, "failed": 0, "skipped": 1}
    definition = task_registry.require(scope.task_type)
    monkeypatch.setitem(task_registry._definitions, scope.task_type, replace(definition, handler=conflicting_handler))
    monkeypatch.setattr(database, "get_engine", lambda: fixture_engine)
    assert bounded.run_worker(scope, tmp_path, str(uuid4()), time.monotonic() + 20) == 2
    record = json.loads((tmp_path / "worker.json").read_text())
    assert record["outcome"] == "blocked" and record["problem"] == "no_completed_scope"
    assert record["publication"]["status"] == "verified"
    assert record["counts"]["logical_requests"] == 0


def test_worker_cancellation_preserves_no_publication(fixture_engine, scope, tmp_path, monkeypatch):
    from dataclasses import replace
    from app.db import session as database
    from app.scheduling.registry import task_registry
    from app.data_ingestion.tonghuashun.control import check_execution
    def cancelling_handler(context, parameters):
        os.kill(os.getpid(), signal.SIGTERM)
        check_execution()
    definition = task_registry.require(scope.task_type)
    monkeypatch.setitem(task_registry._definitions, scope.task_type, replace(definition, handler=cancelling_handler))
    monkeypatch.setattr(database, "get_engine", lambda: fixture_engine)
    assert bounded.run_worker(scope, tmp_path, str(uuid4()), time.monotonic() + 20) == 2
    record = json.loads((tmp_path / "worker.json").read_text())
    assert record["outcome"] == "cancelled" and record["publication"]["status"] == "none"
    assert record["counts"]["logical_requests"] == 0 and not record["unknown_publication"]


def test_child_ipc_revalidates_nested_fields_before_publication(scope, tmp_path):
    operation_id = str(uuid4())
    child = bounded._base(scope, operation_id)
    child.update(phase="terminal", outcome="failed", publication={"status": "none"},
        counts={key: 0 for key in bounded.COUNT_KEYS}, counts_complete=True,
        diagnostic={"error_kind": "invalid_parameters", "supplier_business_code": 1002,
                    "exception": "fictional-secret"}, problem="fictional-secret",
        response_evidence={"sha256": "a" * 64, "item": [{"value": "fictional-secret"}]})
    child["provenance"]["api_key"] = "fictional-secret"
    bounded._write(tmp_path / "worker.json", child)
    safe = bounded._read_worker(tmp_path / "worker.json", scope, operation_id)
    assert "fictional-secret" not in json.dumps(safe)
    assert safe["diagnostic"]["error_kind"] == "invalid_parameters"
    assert safe["diagnostic"]["supplier_business_code"] == 1002
    assert safe["response_evidence"] == {"sha256": "a" * 64}


def test_supervisor_requires_new_output_and_rejects_budget_override(scope, tmp_path):
    with pytest.raises(FileExistsError):
        bounded.supervise(scope, tmp_path)
    for budget in (181, float("inf"), float("nan"), 0):
        with pytest.raises(ValueError):
            bounded.supervise(scope, tmp_path / "new", _budget=budget)


def test_supervisor_kills_only_its_child_within_reserved_deadline(scope, tmp_path):
    children = []
    innocent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    def launcher(command, **options):
        assert command[:3] == [sys.executable, "-m", bounded.__name__]
        assert options["shell"] is False and options["start_new_session"] is True
        assert options["stdout"] == options["stderr"] == subprocess.DEVNULL
        # This invented child does no database or provider work. Ignoring TERM
        # forces the supervisor's KILL/reap path instead of a cooperative exit.
        # Inherit SIG_IGN across exec so a loaded CI runner cannot deliver
        # TERM before the invented child's Python handler has initialized.
        # Restore the supervisor's handler immediately; this is fixture-only
        # setup, not a change to the production signal or wall-time budgets.
        previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            child = subprocess.Popen([sys.executable, "-c",
                "import time; time.sleep(10)"], **options)
        finally:
            signal.signal(signal.SIGTERM, previous)
        children.append(child)
        return child
    started = time.monotonic()
    try:
        record = bounded.supervise(scope, tmp_path / "attempt", _budget=2, _popen=launcher)
        assert time.monotonic() - started < 3
        assert record["outcome"] == "timed_out"
        assert record["kill_sent"] and record["process_exited"]
        assert record["exit_code"] == -signal.SIGKILL
        assert record["unknown_publication"] is True and record["counts_complete"] is False
        assert record["counts"] == {key: None for key in bounded.COUNT_KEYS}
        assert len(children) == 1 and innocent.poll() is None
        assert (tmp_path / "attempt").stat().st_mode & 0o777 == 0o700
    finally:
        for child in children + [innocent]:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


def test_supervisor_does_not_treat_zero_exit_as_publication(scope, tmp_path):
    def launcher(_command, **options):
        return subprocess.Popen([sys.executable, "-c", "pass"], **options)
    record = bounded.supervise(scope, tmp_path / "missing-result", _budget=2, _popen=launcher)
    assert record["exit_code"] == 0 and record["process_exited"]
    assert record["outcome"] == "worker_error" and record["unknown_publication"]


def test_signal_refuses_an_existing_process_outside_owned_session(monkeypatch):
    process = Mock(pid=42)
    process.poll.return_value = None
    monkeypatch.setattr(os, "getpgid", lambda _: 43)
    assert bounded._signal_owned(process, kill=True) is False
    process.kill.assert_not_called()


def test_cli_has_no_budget_or_extra_request_override(scope, tmp_path, capsys):
    with pytest.raises(SystemExit):
        bounded.main(["--dataset", scope.dataset, "--subject", scope.subject,
            "--start-date", "2026-09-01", "--end-date", "2026-09-03", "--output", str(tmp_path / "a"),
            "--max-seconds", "181"])


def test_validation_reason_allows_only_authored_categories_and_known_fields():
    message = "行情或净值存在缺失、负值或非法数值：日期 2026-09-02，字段 volume，原因 负值。"
    assert bounded._reason("invalid_data", message) == message
    assert bounded._reason("invalid_data", message.replace("volume", "fictional-secret")) is None
    assert bounded._reason("invalid_data", message.replace("负值。", "fictional-secret。")) is None
    assert bounded._reason("invalid_data", "fictional-secret") is None


def test_owned_pg_sessions_get_finite_timeouts_and_operation_tag(monkeypatch):
    from sqlalchemy import event
    handlers = {}
    monkeypatch.setattr(event, "listen", lambda engine, name, callback: handlers.update({name: callback}))
    engine = Mock(dialect=Mock(name="postgresql"))
    engine.dialect.name = "postgresql"
    operation_id = str(uuid4())
    bounded._tag_engine(engine, operation_id)
    parameters = {"options": "-csearch_path=fixture", "password": "fictional-secret"}
    handlers["do_connect"](None, None, None, parameters)
    assert parameters["connect_timeout"] == 5
    assert parameters["options"] == "-csearch_path=fixture -cstatement_timeout=5000 -clock_timeout=5000"
    assert parameters["application_name"] == "ths-bounded:" + operation_id
    assert parameters["password"] == "fictional-secret"
