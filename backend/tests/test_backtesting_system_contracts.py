"""Cross-boundary regressions for persisted run semantics and research output."""

from datetime import date, datetime, timezone
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.backtesting.component_config import resolve_components
from app.backtesting.comparison import configuration_difference, metric_projection
from app.backtesting.data.adapters.etf import EtfFactsAdapter
from app.backtesting.models import BacktestAccountProfileVersionRecord
from app.backtesting.service import AccountProfileService
from app.backtesting.account_profiles import AccountProfileStatus
from app.backtesting.spec import ComponentSelection
from app.strategies.parameter_contract import validate_parameters
from app.strategies.validation import validate_strategy_draft
from tests.test_backtesting_account_profile_storage import fee_schedule_payload


def test_coverage_projection_preserves_authoritative_window_and_pit():
    iid = uuid4()
    row = SimpleNamespace(instrument_id=iid, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), status="complete", event_count=0, action_type="split", validation_rule="source_complete@1", summary={}, evidence={"end_date": "2099-01-01"}, observed_at=datetime(2026, 2, 1, tzinfo=timezone.utc), known_at=datetime(2026, 2, 1, tzinfo=timezone.utc), source="verified_source", source_revision="revision-2")
    adapter = object.__new__(EtfFactsAdapter)
    with patch.object(EtfFactsAdapter, "corporate_action_coverage", return_value=(row,)):
        fact, = adapter.corporate_action_coverage_facts((iid,), row.start_date, row.end_date)
    assert fact.details["start_date"] == "2026-01-01"
    assert fact.details["end_date"] == "2026-01-31"
    assert fact.details["action_type"] == "split"
    assert fact.evidence.source_revision == "revision-2"
    assert fact.evidence.known_at == row.known_at


@pytest.mark.parametrize("parameters", [{}, {"risk": True}, {"risk": -1}, {"risk": 11}, {"risk": 1, "extra": 2}])
def test_publication_and_execution_share_parameter_rejections(parameters):
    schema = {"type": "object", "properties": {"risk": {"type": "number", "minimum": 0, "maximum": 10}}, "required": ["risk"], "additionalProperties": False}
    assert validate_parameters(schema, parameters)
    assert not validate_strategy_draft("def run(context, parameters):\n    return None\n", parameter_schema=schema, default_parameters=parameters).valid
    assert not validate_parameters(schema, {"risk": 2})


def test_schema_rejects_unsupported_constraints_and_validates_nested_arrays():
    assert validate_parameters({"oneOf": []}, {})
    schema = {"properties": {"weights": {"type": "array", "items": {"type": "number", "minimum": 0}, "minItems": 1}}}
    assert validate_parameters(schema, {"weights": [-1]})
    assert not validate_parameters(schema, {"weights": [0, .2]})


def test_component_kind_version_and_analyzer_parameters_fail_before_worker():
    with pytest.raises(ValueError):
        resolve_components(selections={"time_axis": ComponentSelection("trading_day", 2)})
    with pytest.raises(ValueError):
        resolve_components(analyzers=(ComponentSelection("sharpe_config_rf", 1, {"rf_annual": "-1", "rf_source_note": "fixture"}),))
    result = resolve_components(analyzers=(ComponentSelection("sharpe_simple", 1), ComponentSelection("performance", 1)))
    assert result["time_axis"]["version"] == 1
    assert [item["key"] for item in result["analyzer"]] == ["sharpe_simple", "performance"]


def test_comparison_preserves_rate_conventions_and_missingness():
    row = SimpleNamespace(run_id=uuid4(), metric_key="sharpe", formula_version="v1", value=Decimal("1.2"), unit="ratio", sample_count=20, unavailable_reason=None, analyzer_key="sharpe_config_rf", analyzer_version=1, analyzer_metadata={"annualization_factor": "252", "rf_annual": "0.03", "rf_source_note": "frozen"})
    assert metric_projection(row)["analyzer_metadata"]["rf_annual"] == "0.03"
    diff = configuration_difference({"data_request": {"non_strict_pit": None}}, {"data_request": {}})
    assert diff["data_request.non_strict_pit"]["baseline_present"] is True
    assert diff["data_request.non_strict_pit"]["current_present"] is False


def account_database():
    """Use real SQL transactions with portable equivalents of PostgreSQL JSONB."""
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    source = sa.Table("backtest_account_profiles", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String()), sa.Column("status", sa.String()),
        sa.Column("version", sa.Integer()), sa.Column("fee_schedule_version", sa.Integer()),
        sa.Column("fee_schedule_key", sa.String()), sa.Column("fee_rules", sa.JSON()),
        sa.Column("fee_schedule_metadata", sa.JSON()), sa.Column("metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    metadata.create_all(engine)
    return engine, source


def test_account_migration_backfill_and_new_versions_are_independent():
    engine, source = account_database()
    migration = import_module("app.db.migrations.versions.20260908_01_account_version_catalog")
    existing_id = uuid4()
    with engine.begin() as connection:
        connection.execute(source.insert().values(id=existing_id, name="迁移账户", status="active", version=7, fee_schedule_version=3, fee_schedule_key="etf-cny", fee_rules=fee_schedule_payload()["fee_rules"], fee_schedule_metadata={}, metadata={}))
        with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
            migration.upgrade()
    with Session(engine) as session:
        service = AccountProfileService(session)
        old = service.get_version(existing_id, 7)
        assert old.name == "迁移账户" and old.fee_schedule_version == 3
        service.update(existing_id, name="新名称", fee_schedule=fee_schedule_payload())
        session.commit()
        assert service.get_version(existing_id, 7).name == "迁移账户"
        assert service.get_version(existing_id, 8).fee_schedule_version == 4
        assert len(service.versions(existing_id)) == 2
        service.set_version_status(existing_id, 7, "inactive")
        assert service.get_version(existing_id, 7).status == "inactive"
        record = session.get(BacktestAccountProfileVersionRecord, (existing_id, 7))
        record.snapshot = {**record.snapshot, "name": "forged"}
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()
        fresh = service.create(name="首次账户", status=AccountProfileStatus.ACTIVE, fee_schedule=fee_schedule_payload(), metadata={})
        session.commit()
        assert service.get_version(fresh.id, 1).fee_schedule_version == 1
        service.delete(existing_id)
        session.commit()
        assert service.get_version(existing_id, 8).status == "retired"
    engine.dispose()


def test_performance_analyzer_uses_e0_and_never_splices_invalid_equity():
    from app.backtesting.analyzers import AnalyzerEngine, build_performance_spec
    from tests.test_backtesting_analyzers import e0, observation, SESSIONS
    engine = AnalyzerEngine.create(e0("100", formal_sessions=SESSIONS[:3]), [build_performance_spec()])
    for index, amount in enumerate(("110", "88", "99")):
        engine.observe_equity(observation(SESSIONS[index], amount, step=index))
    results = {row.metric_key: row for row in engine.snapshot().compute_provisional_results()}
    assert results["total_return"].value == Decimal("-0.01")
    assert results["max_drawdown"].value == Decimal("0.2")
    assert results["volatility"].sample_count == 3
    assert results["volatility"].value > 0


def test_comparison_route_serializes_real_result_contract_and_owner_filter():
    from app.backtesting.result_router import compare_runs
    from app.backtesting.comparison import BacktestComparison
    from app.backtesting.models import BacktestRunRecord
    from app.backtesting.result_records import BacktestEquityCurveRecord, BacktestMetricRecord, BacktestDataPreflightResultRecord
    from app.core.auth import AuthenticatedPrincipal
    from unittest.mock import Mock
    ids = [uuid4(), uuid4()]
    roots = [BacktestRunRecord(id=rid, run_kind="backtest_run", profile="formal@1", status="succeeded", terminal_status="succeeded", idempotency_scope="owner-a", config_hash="a" * 64, parameters={"risk": i}, backtest_config={"account": {"version": i + 1}}, data_request={"frequency": "1d"}, behavior_versions={}, result_summary={}, data_evidence={}) for i, rid in enumerate(ids)]
    points = [BacktestEquityCurveRecord(run_id=rid, as_of=datetime(2026, 1, 2, tzinfo=timezone.utc), sequence=0, equity=Decimal(100), drawdown=Decimal(0), valuation_status="complete") for rid in ids]
    metrics = [BacktestMetricRecord(run_id=rid, metric_key="sharpe", formula_version="v1", value=Decimal(1), unit="ratio", sample_count=2, analyzer_key="sharpe_simple", analyzer_version=1, analyzer_metadata={"annualization_factor": "252"}) for rid in ids]
    reports = [BacktestDataPreflightResultRecord(run_id=rid, phase="session", status="ready", report_hash="b" * 64, hash_schema_version=2, capabilities={"__pit__": {"non_strict_pit": True, "non_strict_pit_capabilities": ["bars"]}}, calendar_summary={}, session_summary={}, coverage={}, source_revisions={}) for rid in ids]
    tables = {BacktestRunRecord: roots, BacktestEquityCurveRecord: points, BacktestMetricRecord: metrics, BacktestDataPreflightResultRecord: reports}
    statements = []
    session = Mock()
    def select_rows(statement):
        statements.append(statement)
        return tables[statement.column_descriptions[0]["entity"]]
    session.scalars.side_effect = select_rows
    request = SimpleNamespace(state=SimpleNamespace(authenticated_principal=AuthenticatedPrincipal("owner-a")))
    payload = BacktestComparison.model_validate(compare_runs({"run_ids": [str(rid) for rid in ids]}, session, request)).model_dump(mode="json")
    assert "owner-a" in str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert payload["run_summaries"][0]["data_evidence"]["reports"][0]["non_strict_pit"] is True
    assert payload["run_summaries"][1]["config_diff"]["account_snapshot.version"]["current"] == 2
    assert payload["equity_curve_series"][0]["points"][0]["equity"] == "100"


def test_kernel_import_does_not_load_sqlalchemy():
    import subprocess
    import sys
    subprocess.run([sys.executable, "-c", "import app.backtesting.runtime; import sys; assert 'sqlalchemy' not in sys.modules"], check=True)


def test_schema_accepts_arbitrary_precision_integers_without_float_overflow():
    assert not validate_parameters({"properties": {"count": {"type": "integer", "minimum": 0}}}, {"count": 10 ** 400})
    assert validate_parameters({"properties": {"count": {"type": ["integer"]}}}, {"count": 1})


def test_invalid_equity_makes_every_performance_metric_unavailable():
    from app.backtesting.analyzers import AnalyzerEngine, build_performance_spec
    from tests.test_backtesting_analyzers import e0, observation, SESSIONS
    engine = AnalyzerEngine.create(e0("100", formal_sessions=SESSIONS[:3]), [build_performance_spec()])
    for index, amount in enumerate(("110", "0", "99")):
        engine.observe_equity(observation(SESSIONS[index], amount, step=index))
    results = engine.snapshot().compute_provisional_results()
    assert len(results) == 4
    assert all(row.value is None and row.analyzer_metadata["reason_code"] == "INVALID_EQUITY" for row in results)
    from app.backtesting.analysis_finalization import _metric_result_to_dto
    from app.backtesting.result_repository import _validate_metric_producer_contract
    for result in results:
        _validate_metric_producer_contract(_metric_result_to_dto(result, uuid4()))


def test_runtime_rejects_fixed_policy_and_request_mismatch():
    from app.backtesting.component_config import validate_runtime_policies
    from tests.test_backtesting_frozen_run_config import _data_request
    from dataclasses import replace
    request = _data_request()
    components = resolve_components()
    validate_runtime_policies(components, request, require_all=True)
    with pytest.raises(ValueError, match="time_axis"):
        validate_runtime_policies({**components, "time_axis": {"key": "trading_day", "version": 2}}, request, require_all=True)
    with pytest.raises(ValueError, match="data provider"):
        validate_runtime_policies(components, replace(request, provider_key="another_provider"))


def test_binding_rejects_analyzer_snapshot_different_from_spec():
    from app.backtesting.run_binding import RunBindingBuilder
    from app.backtesting.spec import BacktestSpec
    spec = BacktestSpec(start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), initial_cash="100", initial_positions=())
    components = resolve_components(analyzers=(ComponentSelection("sharpe_simple", 1),))
    with pytest.raises(ValueError, match="analyzers"):
        RunBindingBuilder().build(spec, components=components)


def test_comparison_preserves_overall_pit_independently_of_calendar_pit():
    from app.backtesting.result_schemas import BacktestDataPreflightItem
    report = BacktestDataPreflightItem.model_validate({
        "run_id": uuid4(), "phase": "session", "status": "degraded", "report_hash": "a" * 64,
        "capabilities": {"__pit__": {"non_strict_pit": True, "non_strict_pit_capabilities": ["bars"]}},
        "calendar_summary": {"non_strict_pit": False, "data_cutoff": "2026-01-03T00:00:00Z"},
    })
    assert report.non_strict_pit is True
    assert report.non_strict_pit_capabilities == ["bars"]
    assert report.data_cutoff == "2026-01-03T00:00:00Z"


def test_initial_holdings_use_official_predecessor_even_without_strategy_warmup():
    from datetime import time
    from unittest.mock import Mock
    from app.backtesting.calendar_axis import SessionPoint, SessionWindow
    from app.backtesting.production_runtime import _admit_formal_analysis, _initial_portfolio
    from app.backtesting.spec import BacktestSpec, InitialPositionInput
    from app.backtesting.analysis_admission import AdmissionBlockedError, verify_initial_portfolio_consistency
    from tests.test_backtesting_analysis_admission import evidence
    from tests.test_backtesting_frozen_run_config import _data_request
    request = _data_request()
    instrument = request.static_instrument_ids[0]
    day = date(2026, 1, 2)
    previous = date(2026, 1, 1)
    report = SimpleNamespace(
        resolved_sessions=(SessionPoint(session_date=day, session_id="fixture-open", timezone="Asia/Shanghai", sessions=(SessionWindow(time(9, 30), time(15)),)),),
        warmup_sessions=(),
    )
    spec = BacktestSpec(day, day, "10000", (InitialPositionInput(instrument, "long", "100", "100", "9"),))
    components = resolve_components(analyzers=(ComponentSelection("sharpe_simple", 1), ComponentSelection("performance", 1)))
    provider = SimpleNamespace(calendar_provider=Mock(), adapter=Mock())
    provider.calendar_provider.load_calendar_snapshot.return_value = SimpleNamespace(warmup_sessions=(SimpleNamespace(session_date=previous),))
    bar = SimpleNamespace(trade_date=previous, close=Decimal("10"), evidence=evidence(datetime(2026, 1, 1, 8, tzinfo=timezone.utc)))
    provider.adapter.bars.return_value = SimpleNamespace(bars=(bar,))
    admission = _admit_formal_analysis(spec, components, report, provider, uuid4(), request)
    assert admission.initial_equity_snapshot.equity_e0 == Decimal("11000")
    portfolio = _initial_portfolio(SimpleNamespace(spec=spec), admission.initial_equity_snapshot)
    verify_initial_portfolio_consistency(admission.initial_equity_snapshot, portfolio)
    assert portfolio.positions[instrument].mark_price == Decimal("10")
    assert portfolio.positions[instrument].unrealized_pnl == Decimal("100")
    assert provider.adapter.resolve.call_args.kwargs["sessions"] == (previous,)
    calendar_request = provider.calendar_provider.prepare_calendar_snapshot.call_args.args[0]
    assert calendar_request.warmup_sessions == 1 and spec.warmup_sessions == 0
    provider.calendar_provider.open_calendar_snapshot.assert_not_called()
    # Observed time alone must never be substituted for strict PIT knowledge.
    bar.evidence = evidence(None)
    with pytest.raises(AdmissionBlockedError) as failure:
        _admit_formal_analysis(spec, components, report, provider, uuid4(), request)
    assert failure.value.reason_code == "MISSING_INITIAL_MARK"


def test_nested_frozen_parameters_keep_their_published_json_types():
    from app.backtesting.spec import BacktestSpec
    from app.strategies.service import _normalize_json_object, _validate_runtime_parameters
    schema = {"properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"weight": {"type": "number"}}}}}}
    spec = BacktestSpec(date(2026, 1, 1), date(2026, 1, 2), "100", (), strategy_parameters={"items": [{"weight": 2}]})
    plain = _normalize_json_object(spec.strategy_parameters, field_name="parameters")
    assert plain == {"items": [{"weight": 2}]}
    assert not _validate_runtime_parameters(schema, plain)


def test_preflight_returns_confirmation_hash_and_reuses_the_same_cutoff():
    from unittest.mock import Mock
    from app.backtesting.run_router import preflight, _build_spec, _request_fingerprint
    from app.backtesting.production_runtime import build_formal_binding
    from app.backtesting.data.requests import QueryBoundary, PreflightStatus
    from tests.test_backtesting_run_api import _payload
    cutoff = datetime(2026, 1, 3, tzinfo=timezone.utc)
    payload = _payload()
    session = Mock()
    repository = Mock()
    repository.get_revision.return_value = SimpleNamespace(id=payload.strategy_revision_id)
    qualification_hash, base_hash = "a" * 64, "b" * 64
    blocked = ValueError("confirmation required")
    blocked.admission_result = SimpleNamespace(
        outcome=SimpleNamespace(status=PreflightStatus.DEGRADED, report=SimpleNamespace(query_boundary=QueryBoundary(cutoff), issues=())),
        report_hash=qualification_hash, formal_gates={}, reason_code="formal_degraded_confirmation_required",
    )
    with patch("app.backtesting.run_router.StrategyRepository", return_value=repository), patch("app.backtesting.run_router.build_formal_binding", side_effect=blocked):
        initial = preflight(payload, session)
    assert initial["report_hash"] == qualification_hash
    assert datetime.fromisoformat(initial["data_cutoff"]) == cutoff
    confirmed = payload.model_copy(update={"data_cutoff": cutoff, "degraded": True, "confirmed_admission_report_hash": qualification_hash})
    success = SimpleNamespace(
        binding=SimpleNamespace(metadata={"data_preflight_status": "degraded", "admission_report_hash": base_hash, "admission_qualification_hash": qualification_hash}, data_request={"query_boundary": {"data_cutoff": cutoff.isoformat()}}),
        formal_gate_evidence={"allowed": True},
    )
    with patch("app.backtesting.run_router.StrategyRepository", return_value=repository), patch("app.backtesting.run_router.build_formal_binding", return_value=success):
        checked = preflight(confirmed, session)
    assert checked["report_hash"] == qualification_hash  # Never switch to the base hash after confirmation.
    assert checked["data_cutoff"] == cutoff.isoformat()
    from dataclasses import replace
    spec = replace(_build_spec(confirmed), instrument_ids=(uuid4(),))
    # Stop immediately before provider I/O and assert the production composition
    # uses the client-returned cutoff instead of taking a fresh wall-clock time.
    provider = SimpleNamespace(provider_key="etf_ingestion")
    captured = []
    class BoundaryReached(Exception):
        pass
    from app.backtesting.data.requests import DataPreflightRequest
    def capture(**kwargs):
        intent = DataPreflightRequest(**kwargs)
        captured.append(intent.query_boundary.data_cutoff)
        raise BoundaryReached
    with patch("app.backtesting.production_runtime.SqlBacktestProvider", return_value=provider), patch("app.backtesting.production_runtime.DataPreflightRequest", side_effect=capture):
        with pytest.raises(BoundaryReached):
            build_formal_binding(spec=spec, revision=repository.get_revision.return_value, session=session, degraded=True, confirmed_report_hash=qualification_hash)
    assert captured == [cutoff]
    assert _request_fingerprint(payload, "backtest_run") != _request_fingerprint(confirmed, "backtest_run")


def test_sql_calendar_admission_keeps_fixed_scope_warmup_and_capability_gates():
    from dataclasses import replace
    from unittest.mock import Mock
    from app.backtesting.production_runtime import SqlBacktestProvider
    from tests.test_backtesting_frozen_run_config import _data_request
    request = replace(_data_request(), warmup_sessions=2)
    provider = object.__new__(SqlBacktestProvider)
    provider.calendar_provider = Mock()
    captured = []
    class CalendarReached(Exception):
        pass
    def project(intent, snapshot, **kwargs):
        captured.append((intent, kwargs))
        raise CalendarReached
    with patch.object(SqlBacktestProvider, "_calendar_ids", return_value=("SSE",)), patch("app.backtesting.data.sessions.evaluate_calendar_capability_gate", return_value=(("blocked-capability",), ("evidence",))), patch("app.backtesting.data.sessions._snapshot_report", side_effect=project):
        with pytest.raises(CalendarReached):
            provider.preflight(request)
    snapshot_request = provider.calendar_provider.open_calendar_snapshot.call_args.args[0]
    assert snapshot_request.instrument_ids == request.fixed_instrument_ids
    assert snapshot_request.warmup_sessions == 2
    assert captured[0][0].static_instrument_ids == request.static_instrument_ids
    assert captured[0][1]["extra_issues"] == ("blocked-capability",)
    assert captured[0][1]["capability_evidence"] == ("evidence",)
