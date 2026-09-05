"""Focused checks for the PIT/provenance closure on ingestion and adapters."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.backtesting.data.adapters.etf import EtfFactsAdapter
from app.backtesting.data.errors import ProviderContractViolationError
from app.backtesting.data.protocols import CoverageEnvelope, DataCapabilityManifest
from app.backtesting.data.requests import (
    CorporateActionQuery,
    DataCapability,
    DateRange,
    QueryBoundary,
    QualityStatus,
    CALENDAR_AXIS_POLICY,
    CHUNK_POLICY,
    ConsistencyMode,
    ContractRef,
    PitSupport,
    PriceBasis,
    TradingStatusQuery,
)
from app.backtesting.production_runtime import SqlBacktestChunkSession
from app.data_ingestion.models.trading_calendar import (
    TradingStatusCoverageFact,
    TradingStatusFact,
    TradingStatusFactRevisionAudit,
)
from app.data_ingestion.schemas.trading_status import normalize_suspend_row
from app.data_ingestion.services.trading_status import sync_suspend_d


INSTRUMENT_ID = uuid4()
CUTOFF = datetime(2026, 9, 1, tzinfo=UTC)


class _Session:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = []

    def get(self, _model, _key):
        return self.existing

    def add(self, value):
        self.added.append(value)


class _Client:
    def __init__(self, rows):
        self.rows = rows

    def suspend_d(self, **_kwargs):
        return self.rows


def test_status_normalization_preserves_unknown_source_values_as_invalid():
    item = normalize_suspend_row(
        {
            "ts_code": "510300.SH",
            "trade_date": "2026-08-31",
            "suspend_type": "provider-new-value",
        }
    )

    assert item.status == "unknown"
    assert item.quality_status == "invalid"
    assert item.source_revision.startswith("derived:tushare:suspend_d_row@1:sha256:")
    assert item.raw["suspend_type"] == "provider-new-value"


def test_status_sync_does_not_infer_negative_coverage_from_empty_response():
    session = _Session()
    result = sync_suspend_d(
        _Client([]),
        session=session,
        instrument_map={"510300.SH": INSTRUMENT_ID},
        accepted_at=CUTOFF,
        start_date="2026-08-31",
        end_date="2026-08-31",
    )

    assert result["coverage_status"] == "unknown"
    assert not any(
        isinstance(item, TradingStatusCoverageFact) for item in session.added
    )


def test_status_sync_persists_effective_interval_and_negative_coverage_proof():
    session = _Session()
    result = sync_suspend_d(
        _Client(
            [
                {
                    "ts_code": "510300.SH",
                    "trade_date": "2026-08-31",
                    "suspend_type": "S",
                }
            ]
        ),
        session=session,
        instrument_map={"510300.SH": INSTRUMENT_ID},
        accepted_at=CUTOFF,
        coverage_confirmed=True,
        start_date="2026-08-31",
        end_date="2026-08-31",
    )

    fact = next(item for item in session.added if isinstance(item, TradingStatusFact))
    coverage = next(
        item for item in session.added if isinstance(item, TradingStatusCoverageFact)
    )
    assert result["failed"] == 0
    assert fact.instrument_id == INSTRUMENT_ID
    assert fact.valid_from == date(2026, 8, 31)
    assert fact.valid_to == date(2026, 9, 1)
    assert fact.known_at == CUTOFF
    assert fact.source_revision
    assert coverage.event_count == 1
    assert coverage.start_date == date(2026, 8, 31)
    assert coverage.end_date == date(2026, 8, 31)
    assert coverage.known_at == CUTOFF
    assert coverage.status == "complete"


def test_status_correction_adds_append_only_revision_audit():
    existing = SimpleNamespace(
        instrument_id=INSTRUMENT_ID,
        dimension="suspension",
        status="suspended",
        valid_from=date(2026, 8, 31),
        valid_to=date(2026, 9, 1),
        source="tushare",
        source_revision="old-revision",
        quality_status="complete",
        known_at=CUTOFF,
        observed_at=CUTOFF,
        raw={"suspend_type": "S"},
    )
    session = _Session(existing)
    sync_suspend_d(
        _Client(
            [
                {
                    "ts_code": "510300.SH",
                    "trade_date": "2026-08-31",
                    "suspend_type": "R",
                }
            ]
        ),
        session=session,
        accepted_at=CUTOFF,
        start_date="2026-08-31",
        end_date="2026-08-31",
    )

    audit = next(
        item
        for item in session.added
        if isinstance(item, TradingStatusFactRevisionAudit)
    )
    assert existing.status == "tradable"
    assert existing.instrument_id == INSTRUMENT_ID
    assert audit.previous_source_revision == "old-revision"
    assert audit.previous_status == "suspended"
    assert audit.source_revision != "old-revision"
    assert audit.change_kind == "correction"
    assert "status" in audit.changed_fields


def test_corporate_action_projection_keeps_pit_and_validity_metadata():
    row = SimpleNamespace(
        event_id=uuid4(),
        instrument_id=INSTRUMENT_ID,
        action_type="cash_dividend",
        ex_date=date(2026, 8, 31),
        valid_from=date(2026, 8, 31),
        valid_to=date(2026, 9, 1),
        effective_time=None,
        record_date=date(2026, 8, 31),
        source_payment_date=date(2026, 9, 1),
        source_arrival_date=None,
        cash_effective_date=date(2026, 9, 1),
        cash_effective_phase="after_open_match",
        cash_amount_per_unit=Decimal("0.10"),
        currency="CNY",
        entitlement_rule="record_date_entitlement",
        cash_date_rule="tushare_fund_div_cash_date@1",
        timing_rule="after_open_match@1",
        source="tushare",
        source_revision="source-revision-1",
        known_at=CUTOFF,
        observed_at=CUTOFF,
        created_at=CUTOFF,
        quality="complete",
        evidence={
            "calendar_id": "XSHG",
            "timezone": "Asia/Shanghai",
            "cash_date_rule": "tushare_fund_div_cash_date@1",
            "timing_rule": "after_open_match@1",
        },
        fact_version=2,
    )

    class Repository:
        def list_facts(self, *_args, **_kwargs):
            return (row,)

    adapter = EtfFactsAdapter(
        code_mappings=lambda *_args, **_kwargs: (),
        daily_bars=lambda *_args, **_kwargs: (),
        adjustment_factors=lambda *_args, **_kwargs: (),
        trading_days=lambda *_args, **_kwargs: (),
        corporate_action_repository=Repository(),
    )
    actions = adapter.corporate_actions(
        CorporateActionQuery(
            instrument_ids=(INSTRUMENT_ID,),
            window=DateRange(date(2026, 8, 31), date(2026, 8, 31)),
            boundary=QueryBoundary(CUTOFF, include_cutoff_day=True),
        )
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.valid_from == date(2026, 8, 31)
    assert action.valid_to == date(2026, 9, 1)
    assert action.evidence.known_at == CUTOFF
    assert action.evidence.source_revision == "source-revision-1"
    assert action.cash_effective_date == date(2026, 9, 1)
    assert action.cash_amount_per_unit == Decimal("0.10")
    assert action.attributes["cash_effective_date"] == "2026-09-01"


def test_status_adapter_returns_typed_fact_with_dimension_and_pit_evidence():
    row = SimpleNamespace(
        instrument_id=INSTRUMENT_ID,
        ts_code="510300.SH",
        trade_date=date(2026, 8, 31),
        status="suspended",
        dimension="suspension",
        valid_from=date(2026, 8, 31),
        valid_to=date(2026, 9, 1),
        source="tushare",
        source_revision="revision-1",
        quality_status="complete",
        known_at=CUTOFF,
        observed_at=CUTOFF,
        fact_version=1,
    )
    adapter = EtfFactsAdapter(
        code_mappings=lambda *_args, **_kwargs: (),
        daily_bars=lambda *_args, **_kwargs: (),
        adjustment_factors=lambda *_args, **_kwargs: (),
        trading_days=lambda *_args, **_kwargs: (),
        trading_status_facts=lambda *_args, **_kwargs: (row,),
        trading_status_coverage=lambda *_args, **_kwargs: (),
    )

    facts = adapter.trading_status(
        TradingStatusQuery(
            instrument_ids=(INSTRUMENT_ID,),
            window=DateRange(date(2026, 8, 31), date(2026, 8, 31)),
            boundary=QueryBoundary(CUTOFF, include_cutoff_day=True),
        )
    )

    assert len(facts) == 1
    assert facts[0].status == "suspended"
    assert facts[0].attributes["dimension"] == "suspension"
    assert facts[0].evidence.known_at == CUTOFF
    assert facts[0].evidence.quality_status is QualityStatus.COMPLETE


def test_sql_chunk_expires_when_fact_revision_vector_changes():
    point = SimpleNamespace(
        session_id="XSHG:2026-08-31",
        session_date=date(2026, 8, 31),
    )

    class Provider:
        vector = {"bars": {"row_count": 1, "updated_at": CUTOFF.isoformat()}}

        def _database_revision_vector(self):
            return self.vector

        def _consistency_digest(self, *_args, **_kwargs):
            return "a" * 64

    provider = Provider()
    request = SimpleNamespace(
        required_capabilities=(DataCapability.BARS,),
        consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
        query_boundary=QueryBoundary(CUTOFF, include_cutoff_day=True),
        requested_window=DateRange(date(2026, 8, 31), date(2026, 8, 31)),
        max_lookback_sessions=512,
    )
    session = SimpleNamespace(
        request=request,
        report=SimpleNamespace(report_hash="b" * 64),
        resolved_sessions=(point,),
        warmup_sessions=(),
        _revision_vector=dict(provider.vector),
    )
    chunk = SqlBacktestChunkSession(
        provider,
        session,
        0,
        (point,),
        (DataCapability.BARS,),
    )

    assert chunk.validate_consistency().status.value == "valid"
    provider.vector = {
        "bars": {"row_count": 1, "updated_at": (CUTOFF.replace(day=2)).isoformat()}
    }
    with pytest.raises(ProviderContractViolationError, match="revisions changed during the chunk"):
        chunk.bars(SimpleNamespace())
    assert chunk.consistency_evidence.validation_status.value == "expired"


def test_manifest_and_envelope_expose_revision_scope_contracts():
    manifest = DataCapabilityManifest(
        provider_key="test",
        manifest_version=1,
        data_contract_version=1,
        supported_calendars=(),
        supported_calendar_axis_policies=(CALENDAR_AXIS_POLICY,),
        rule_packages=(ContractRef("rules", 1),),
        rule_exception_sets=(),
        supported_asset_classes=("etf",),
        supported_frequencies=("1d",),
        supported_price_bases=(PriceBasis.RAW,),
        pit_support_by_capability={DataCapability.BARS: PitSupport.NON_STRICT},
        consistency_modes=(ConsistencyMode.CHUNKED_LOGICAL_TOKEN,),
        consistency_token_contracts=(ContractRef("token", 1),),
        supported_chunk_policies=(CHUNK_POLICY,),
        capabilities=(DataCapability.BARS,),
        instrument_rule_fact_contracts=(ContractRef("instrument_rule_facts", 1),),
        adjustment_series_policies=(
            {
                "key": "tushare_adj_factor_native",
                "version": 1,
                "status": "active",
                "cutoff_rule": "effective_date <= data_cutoff",
            },
        ),
    )
    assert manifest.instrument_rule_fact_contracts == (
        ContractRef("instrument_rule_facts", 1),
    )
    assert manifest.adjustment_series_policies[0]["status"] == "active"

    envelope = CoverageEnvelope(
        chunk_first_session_date=date(2026, 8, 31),
        chunk_last_session_date=date(2026, 8, 31),
        fact_coverage_signature="a" * 64,
        data_cutoff=CUTOFF,
    )
    assert envelope.to_summary()["fact_coverage_signature"] == "a" * 64
