"""Regression tests for the persisted run-input snapshot boundary."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.backtesting.data.requests import (
    ConsistencyMode,
    ContractRef,
    DataCapability,
    DataRequest,
    DateRange,
    InstrumentScopeMode,
    MarketScope,
    PriceBasis,
    PreflightStatus,
    QueryBoundary,
    UniverseQueryPolicy,
)
from app.backtesting.data.reports import canonical_hash
from pathlib import Path

from app.backtesting.production_runtime import (
    _behavior_versions,
    _json_value,
    binding_from_row,
    default_components,
    serialize_data_request,
)
from app.backtesting.registry import UnknownComponentError
from app.backtesting.run_binding import RunBindingBuilder
from app.backtesting.spec import BacktestSpec, ComponentSelection


def _data_request() -> DataRequest:
    return DataRequest(
        provider_key="etf_ingestion",
        requested_window=DateRange(date(2026, 1, 1), date(2026, 1, 2)),
        frequency="1d",
        rule_package=ContractRef("china_listed_etf_rules", 1),
        market_scope=MarketScope(exchanges=("SSE",), asset_classes=("etf",)),
        universe_query_policy=UniverseQueryPolicy(),
        instrument_scope_mode=InstrumentScopeMode.FIXED,
        required_capabilities=(DataCapability.BARS,),
        strategy_price_bases=(PriceBasis.RAW,),
        consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
        consistency_token_contract=ContractRef("sql_revision_vector", 1),
        query_boundary=QueryBoundary(
            datetime(2026, 1, 3, tzinfo=timezone.utc)
        ),
        resolved_calendar_ids=("SSE",),
        resolved_timezone="Asia/Shanghai",
        admission_calendar_session_signature="a" * 64,
        admission_preflight_status=PreflightStatus.READY,
        admission_preflight_hash="b" * 64,
        static_instrument_ids=(uuid4(),),
        resolved_rule_snapshot_hash="c" * 64,
    )


def _row_for(binding):
    return SimpleNamespace(
        id=uuid4(),
        tenant_id="owner-a",
        run_kind="internal_link_acceptance",
        profile="internal_link_acceptance@1",
        config_hash=binding.config_hash,
        backtest_config=_json_value(binding.config),
        status="running",
    )


def test_migration_installs_snapshot_identity_and_database_guard() -> None:
    migration = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/20260902_01_freeze_backtest_run_inputs.py"
    ).read_text(encoding="utf-8")
    assert 'sa.Column("random_seed"' in migration
    assert "reject_backtest_run_input_update" in migration
    assert "backtest_run_inputs_immutable" in migration


def test_binding_snapshot_contains_all_run_level_inputs_and_is_stable() -> None:
    revision_id = uuid4()
    account_id = uuid4()
    data_request = _data_request()
    components = default_components()
    spec = BacktestSpec(
        date(2026, 1, 1),
        date(2026, 1, 2),
        "100000",
        [],
        instrument_ids=data_request.static_instrument_ids,
        exchanges=("SSE",),
        strategy_price_bases=("raw",),
        strategy_revision_id=revision_id,
        strategy_parameters={"window": 20},
        account_profile_id=account_id,
        currency="CNY",
        timezone="Asia/Shanghai",
        warmup_sessions=12,
        random_seed=42,
    )
    strategy = {
        "strategy_id": str(uuid4()),
        "revision_id": str(revision_id),
        "revision_number": 3,
        "source_hash": "d" * 64,
        "contract_version": 1,
        "parameters": {"window": 20},
        "parameter_schema": {"type": "object"},
        "published": True,
    }
    account = {
        "profile_id": str(account_id),
        "version": 2,
        "fee_schedule_key": "etf-cny",
        "fee_schedule_version": 4,
        "fee_schedule": {"key": "etf-cny", "version": 4, "fee_rules": []},
    }
    binding = RunBindingBuilder().build(
        spec,
        run_kind="internal_link_acceptance",
        strategy=strategy,
        components=components,
        data_request=serialize_data_request(data_request),
        account=account,
        metadata={"behavior_versions": {"engine": {"key": "engine", "version": 1}}},
        random_seed=42,
    )

    snapshot = _json_value(binding.config)
    assert snapshot["schema_version"] == 2
    assert snapshot["spec"]["warmup_sessions"] == 12
    assert snapshot["spec"]["strategy_revision_id"] == str(revision_id)
    assert snapshot["spec"]["strategy_parameters"] == {"window": 20}
    assert snapshot["spec"]["account_profile_id"] == str(account_id)
    assert snapshot["spec"]["instrument_ids"] == [
        str(data_request.static_instrument_ids[0])
    ]
    assert snapshot["strategy"]["parameters"] == {"window": 20}
    assert snapshot["account"]["fee_schedule_version"] == 4
    assert snapshot["random_seed"] == 42
    assert snapshot["components"]["decision_interpreter"]["key"] == "long_only_target_weights"
    assert snapshot["components"]["slippage_model"]["key"] == "none"
    assert canonical_hash(snapshot) == binding.config_hash

    # Mutating caller-owned mappings after construction cannot alter the
    # already-computed canonical snapshot or its identity.
    strategy["parameters"]["window"] = 99
    components["slippage_model"]["parameters"]["price_tick"] = "0.1"
    assert _json_value(binding.config)["strategy"]["parameters"] == {"window": 20}
    assert _json_value(binding.config)["components"]["slippage_model"]["parameters"]["price_tick"] == "0.01"


def test_slippage_selection_is_resolved_and_frozen_from_the_registry() -> None:
    components = default_components(
        ComponentSelection(
            "bps",
            1,
            {"slippage_bps": "10", "price_tick": "0.01"},
        )
    )

    assert components["slippage_model"]["key"] == "bps"
    assert components["slippage_model"]["version"] == 1
    assert components["slippage_model"]["parameters"]["slippage_bps"] == "10"
    with pytest.raises(UnknownComponentError):
        default_components(ComponentSelection("arbitrary_python_class", 1))


def test_worker_binding_rehydrates_components_from_snapshot_not_defaults() -> None:
    binding = RunBindingBuilder().build(
        BacktestSpec(
            date(2026, 1, 1),
            date(2026, 1, 2),
            100,
            [],
            slippage_model=ComponentSelection("snapshot_slippage", 10),
            random_seed=7,
        ),
        run_kind="internal_link_acceptance",
        strategy={"revision_id": str(uuid4()), "source_hash": "e" * 64, "published": True},
        components={
            "timing_policy": {"key": "snapshot_timing", "version": 7, "parameters": {}},
            "execution_model": {"key": "snapshot_execution", "version": 8, "parameters": {}},
            "decision_interpreter": {"key": "snapshot_interpreter", "version": 9, "parameters": {}},
            "slippage_model": {"key": "snapshot_slippage", "version": 10, "parameters": {}},
            "analyzer": [],
        },
        data_request=serialize_data_request(_data_request()),
    )

    restored = binding_from_row(_row_for(binding))

    assert restored.components["timing_policy"]["key"] == "snapshot_timing"
    assert restored.components["timing_policy"]["version"] == 7
    assert restored.random_seed == 7


def test_behavior_versions_include_all_runtime_semantic_identities() -> None:
    versions = _behavior_versions(
        data_request=_data_request(),
        components=default_components(),
        strategy={"contract_version": 1},
        account={"profile_id": str(uuid4()), "version": 2, "fee_schedule_key": "fees", "fee_schedule_version": 3},
    )

    assert versions["time_axis"] == {"key": "trading_day", "version": 1}
    assert versions["decision_interpreter"]["key"] == "long_only_target_weights"
    assert versions["accounting_policy"] == {"key": "accounting_policy", "version": 1}
    assert versions["analyzer_specs"] == []


def test_worker_binding_rejects_tampered_snapshot() -> None:
    binding = RunBindingBuilder().build(
        BacktestSpec(date(2026, 1, 1), date(2026, 1, 2), 100, []),
        run_kind="internal_link_acceptance",
        strategy={"revision_id": str(uuid4()), "published": True},
    )
    row = _row_for(binding)
    row.backtest_config["spec"]["initial_cash"] = "999"

    with pytest.raises(ValueError, match="snapshot hash mismatch"):
        binding_from_row(row)
