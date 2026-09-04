"""Production composition root for persisted formal backtest runs.

This module is the only place where storage-backed facts are composed with the
provider-independent engine. The isolated worker loads published strategy
source and invokes this composition root; the API never executes strategy code.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.backtesting.accounting import AccountState, AccountingPolicy, SettlementPolicy
from app.backtesting.analysis_finalization import AnalysisFinalizationCoordinator
from app.backtesting.fees import FeeCalculator, FeeRule, FeeSchedule
from app.backtesting.data.adapters.etf import ETF_PROVIDER_KEY, EtfFactsAdapter
from app.backtesting.data.calendar_sql import SqlCalendarAxisDataProvider
from app.backtesting.calendar_models import CalendarResolutionHeadRecord
from app.backtesting.data.errors import ProviderContractViolationError, UnsupportedCapabilityError
from app.backtesting.data.protocols import (
    ConsistencyTokenStatus,
    DataCapabilityManifest,
    DataConsistencyEvidence,
)
from app.backtesting.data.requests import (
    BarQuery, CHUNK_POLICY, CapabilitySource, ConsistencyMode, ConsistencyValidation, ContractRef,
    CorporateActionQuery, DataCapability, DataChunkQuery, DataPreflightRequest, DataRequest, DateRange,
    TradingStatusQuery,
    InstrumentScopeMode, IssueSeverity, LookbackWindow, MarketScope, PriceBasis,
    PitSupport, PreflightStatus, QueryBoundary, UniverseQuery as DataUniverseQuery,
    UniverseQueryPolicy,
)
from app.backtesting.data.reports import (
    DataCoverageReport,
    DataPreflightReport,
    PreflightIssue,
    canonical_hash,
)
from app.backtesting.domain import PositionSide, PositionState, PortfolioState
from app.backtesting.registry import (
    ANALYZER_COMPONENT_KIND, DECISION_INTERPRETER_KIND, EXECUTION_MODEL_KIND,
    RegistryError, SLIPPAGE_MODEL_KIND, TIMING_POLICY_KIND,
    build_default_component_registry,
)
from app.backtesting.result_repository import BacktestResultRepository
from app.backtesting.result_writer import BacktestResultContext, BacktestResultPersistenceService
from app.backtesting.run_admission import RunAdmissionService, build_gate_evidence
from app.backtesting.run_binding import Gate
from app.backtesting.runner_failure import build_failure_evidence
from app.backtesting.data.corporate_actions import RunCorporateActionEventSnapshot
from app.backtesting.runtime import BacktestViewFactory, DeterministicBacktestRunner, EngineDataView, InstrumentFacts, SessionQuote, run_data_session
from app.backtesting.spec import BacktestSpec, ComponentSelection, InitialPositionInput
from app.backtesting.time_axis import TradingDayAxis
from app.data_ingestion.models.corporate_action import (
    CorporateActionCoverageFact,
    CorporateActionFact,
)
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.models.etf_daily import EtfDailyBar
from app.data_ingestion.models.trading_calendar import (
    TradingStatusCoverageFact,
    TradingStatusFact,
    TradingStatusFactRevisionAudit,
    TradingCalendarDay,
)
from app.data_ingestion.repositories.corporate_action import CorporateActionRepository
from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.instruments.domain import InstrumentSpec, VersionedReference
from app.instruments.models import InstrumentCodeMappingRecord
from app.instruments.rule_facts_models import InstrumentRuleFactRecord
from app.instruments.identity_repository import InstrumentIdentityRepository
from app.instruments.repository import InstrumentCodeMappingRepository
from app.instruments.rule_exceptions_repository import RuleExceptionSetsRepository
from app.instruments.rule_facts_repository import RuleFactsRepository
from app.instruments.rule_snapshots_repository import RunRuleSnapshotRepository
from app.instruments.rules.etf_china import register_china_listed_etf_rules
from app.instruments.rules.registry import RulePackageRegistry
from app.instruments.spec_provider import InstrumentSpecProvider
from app.strategies.models import StrategyRevision
from app.strategies.service import StrategyStorageService
from app.backtesting.service import AccountProfileService, fee_schedule_from_record

DATA_TOKEN_CONTRACT = ContractRef("sql_revision_vector", 1)
DEFAULT_RULE_PACKAGE = ContractRef("china_listed_etf_rules", 1)
DEFAULT_CURRENCY = "CNY"
DEFAULT_CALENDARS = ("SSE", "SZSE")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(v) for v in value]
    if isinstance(value, (UUID, date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, ContractRef):
        return {"key": value.key, "version": value.version}
    if hasattr(value, "__dataclass_fields__"):
        return {name: _json_value(getattr(value, name)) for name in value.__dataclass_fields__}
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return _json_value(value.value)
    return value


def _ref(value: object | None) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, ContractRef):
        return {"key": value.key, "version": value.version}
    if isinstance(value, Mapping):
        return {"key": str(value["key"]), "version": int(value["version"])}
    raise ValueError("versioned reference must be a ContractRef or mapping")


def _read_ref(value: object | None) -> ContractRef | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("versioned reference must be an object")
    return ContractRef(str(value["key"]), int(value["version"]))


def serialize_data_request(request: DataRequest) -> dict[str, Any]:
    return {
        "provider_key": request.provider_key,
        "requested_window": _json_value(request.requested_window),
        "frequency": request.frequency,
        "rule_package": _ref(request.rule_package),
        "market_scope": _json_value(request.market_scope),
        "universe_query_policy": _json_value(request.universe_query_policy),
        "instrument_scope_mode": request.instrument_scope_mode.value,
        "required_capabilities": [x.value for x in request.required_capabilities],
        "strategy_price_bases": [x.value for x in request.strategy_price_bases],
        "consistency_mode": request.consistency_mode.value,
        "query_boundary": _json_value(request.query_boundary),
        "static_instrument_ids": [str(x) for x in request.static_instrument_ids],
        "mandatory_instrument_ids": [str(x) for x in request.mandatory_instrument_ids],
        "non_zero_initial_position_instrument_ids": [str(x) for x in request.non_zero_initial_position_instrument_ids],
        "warmup_sessions": request.warmup_sessions,
        "rule_exception_set": _ref(request.rule_exception_set),
        "qualification_policy_version": _ref(request.qualification_policy_version) if isinstance(request.qualification_policy_version, ContractRef) else request.qualification_policy_version,
        "universe_scope_snapshot_hash": request.universe_scope_snapshot_hash,
        "allowed_settlement_rule_class": request.allowed_settlement_rule_class,
        "adjustment_series_policy": _ref(request.adjustment_series_policy),
        "consistency_token_contract": _ref(request.consistency_token_contract),
        "data_contract_version": request.data_contract_version,
        "max_lookback_sessions": request.max_lookback_sessions,
        "calendar_axis_policy": _ref(request.calendar_axis_policy),
        "engine_price_basis": request.engine_price_basis.value,
        "quality_mode": request.quality_mode.value,
        "data_chunk_policy": _ref(request.data_chunk_policy),
        "data_chunk_size_sessions": request.data_chunk_size_sessions,
        "resolved_calendar_ids": list(request.resolved_calendar_ids),
        "resolved_timezone": request.resolved_timezone,
        "admission_calendar_session_signature": request.admission_calendar_session_signature,
        "admission_preflight_status": request.admission_preflight_status.value,
        "admission_preflight_hash": request.admission_preflight_hash,
        "accepted_degraded_preflight_hash": request.accepted_degraded_preflight_hash,
        "resolved_rule_snapshot_hash": request.resolved_rule_snapshot_hash,
    }


def deserialize_data_request(payload: Mapping[str, Any]) -> DataRequest:
    window = payload["requested_window"]
    boundary = payload["query_boundary"]
    market = payload.get("market_scope", {})
    universe = payload.get("universe_query_policy", {})
    knowledge = boundary.get("knowledge_as_of")
    return DataRequest(
        provider_key=str(payload["provider_key"]),
        requested_window=DateRange(date.fromisoformat(str(window["start_date"])), date.fromisoformat(str(window["end_date"]))),
        frequency=str(payload["frequency"]), rule_package=_read_ref(payload["rule_package"]),
        market_scope=MarketScope(**{k: tuple(v) for k, v in market.items()}),
        universe_query_policy=UniverseQueryPolicy(candidate_set_rules=tuple(_read_ref(x) for x in universe.get("candidate_set_rules", ()))),
        instrument_scope_mode=InstrumentScopeMode(str(payload["instrument_scope_mode"])),
        required_capabilities=tuple(DataCapability(x) for x in payload["required_capabilities"]),
        strategy_price_bases=tuple(PriceBasis(x) for x in payload["strategy_price_bases"]),
        consistency_mode=ConsistencyMode(str(payload["consistency_mode"])),
        query_boundary=QueryBoundary(datetime.fromisoformat(str(boundary["data_cutoff"])), knowledge_as_of=datetime.fromisoformat(str(knowledge)) if knowledge else None, include_cutoff_day=bool(boundary.get("include_cutoff_day", False))),
        static_instrument_ids=tuple(UUID(x) for x in payload.get("static_instrument_ids", ())),
        mandatory_instrument_ids=tuple(UUID(x) for x in payload.get("mandatory_instrument_ids", ())),
        non_zero_initial_position_instrument_ids=tuple(UUID(x) for x in payload.get("non_zero_initial_position_instrument_ids", ())),
        warmup_sessions=int(payload.get("warmup_sessions", 0)),
        rule_exception_set=_read_ref(payload.get("rule_exception_set")),
        qualification_policy_version=_read_ref(payload["qualification_policy_version"]) if isinstance(payload.get("qualification_policy_version"), Mapping) else payload.get("qualification_policy_version"),
        universe_scope_snapshot_hash=payload.get("universe_scope_snapshot_hash"), allowed_settlement_rule_class=payload.get("allowed_settlement_rule_class"),
        adjustment_series_policy=_read_ref(payload.get("adjustment_series_policy")), consistency_token_contract=_read_ref(payload.get("consistency_token_contract")),
        data_contract_version=int(payload.get("data_contract_version", 1)), max_lookback_sessions=int(payload.get("max_lookback_sessions", 512)),
        calendar_axis_policy=_read_ref(payload.get("calendar_axis_policy")) or ContractRef("strict_compatible", 1), engine_price_basis=PriceBasis(str(payload.get("engine_price_basis", "raw"))),
        quality_mode=__import__("app.backtesting.data.requests", fromlist=["QualityMode"]).QualityMode(str(payload.get("quality_mode", "strict"))), data_chunk_policy=_read_ref(payload.get("data_chunk_policy")) or CHUNK_POLICY, data_chunk_size_sessions=int(payload.get("data_chunk_size_sessions", 20)),
        resolved_calendar_ids=tuple(payload.get("resolved_calendar_ids", ())), resolved_timezone=str(payload["resolved_timezone"]), admission_calendar_session_signature=str(payload["admission_calendar_session_signature"]), admission_preflight_status=PreflightStatus(str(payload["admission_preflight_status"])), admission_preflight_hash=str(payload["admission_preflight_hash"]), accepted_degraded_preflight_hash=payload.get("accepted_degraded_preflight_hash"), resolved_rule_snapshot_hash=str(payload.get("resolved_rule_snapshot_hash", "")),
    )


class SqlBacktestProvider:
    """Compose SQL repositories into the formal ETF provider contract."""

    provider_key = ETF_PROVIDER_KEY

    def __init__(self, session: Session):
        self.session = session
        self.identity_repository = InstrumentIdentityRepository(session)
        self.mapping_repository = InstrumentCodeMappingRepository(session)
        self.daily_repository = EtfDailyBarRepository(session)
        self.corporate_action_repository = CorporateActionRepository(session)
        self.rule_facts_repository = RuleFactsRepository(session)
        self.exception_repository = RuleExceptionSetsRepository(session)
        self.rule_registry = RulePackageRegistry()
        register_china_listed_etf_rules(self.rule_registry)
        self.spec_provider = InstrumentSpecProvider(identity_repository=self.identity_repository, mapping_repository=self.mapping_repository, rule_fact_repository=self.rule_facts_repository, exception_repository=self.exception_repository, rule_registry=self.rule_registry)
        self.adapter = EtfFactsAdapter(
            code_mappings=self._code_mappings,
            daily_bars=self._daily_bars,
            adjustment_factors=self._adjustment_factors,
            trading_days=self._trading_days,
            trading_status_facts=self._trading_status,
            trading_status_coverage=self._trading_status_coverage,
            spec_provider=self.spec_provider,
            corporate_action_repository=self.corporate_action_repository,
        )
        self.calendar_provider = SqlCalendarAxisDataProvider(session)

    def _code_mappings(self, instrument_id, *, source, start_date, end_date, data_cutoff):
        return self.mapping_repository.resolve_code_mappings(instrument_id, source=source, start_date=start_date, end_date=end_date, data_cutoff=data_cutoff)

    def _daily_bars(self, ts_code, start_date, end_date):
        return self.daily_repository.list_bars(source="tushare", ts_code=ts_code, start_date=start_date, end_date=end_date)

    def _adjustment_factors(self, ts_code, start_date, end_date):
        return tuple(self.session.scalars(select(EtfAdjustmentFactor).where(EtfAdjustmentFactor.source == "tushare", EtfAdjustmentFactor.ts_code == ts_code, EtfAdjustmentFactor.trade_date >= start_date, EtfAdjustmentFactor.trade_date <= end_date).order_by(EtfAdjustmentFactor.trade_date)).all())

    def _trading_days(self, exchange, start_date, end_date):
        from app.data_ingestion.repositories.trading_calendar import TradingCalendarRepository
        return TradingCalendarRepository(self.session).list_open_dates(exchange=exchange, start_date=start_date, end_date=end_date)

    def capability_manifest(self) -> DataCapabilityManifest:
        """Declare the production fact families and their PIT contracts."""

        return DataCapabilityManifest(
            provider_key=self.provider_key,
            manifest_version=1,
            data_contract_version=1,
            supported_calendars=(),
            supported_calendar_axis_policies=(ContractRef("strict_compatible", 1),),
            rule_packages=(DEFAULT_RULE_PACKAGE,),
            rule_exception_sets=(),
            supported_asset_classes=("etf",),
            supported_frequencies=("1d",),
            supported_price_bases=(PriceBasis.RAW,),
            pit_support_by_capability={
                DataCapability.BARS: PitSupport.NON_STRICT,
                DataCapability.MAPPINGS: PitSupport.STRICT,
                DataCapability.RULES: PitSupport.STRICT,
                DataCapability.STATUS: PitSupport.STRICT,
                DataCapability.ACTIONS: PitSupport.STRICT,
                DataCapability.COVERAGE: PitSupport.STRICT,
                DataCapability.CALENDARS: PitSupport.STRICT,
            },
            consistency_modes=(ConsistencyMode.CHUNKED_LOGICAL_TOKEN,),
            consistency_token_contracts=(DATA_TOKEN_CONTRACT,),
            supported_chunk_policies=(CHUNK_POLICY,),
            capabilities=(
                DataCapability.BARS,
                DataCapability.MAPPINGS,
                DataCapability.RULES,
                DataCapability.STATUS,
                DataCapability.ACTIONS,
                DataCapability.COVERAGE,
                DataCapability.CALENDARS,
            ),
            instrument_rule_fact_contracts=(ContractRef("instrument_rule_facts", 1),),
            adjustment_series_policies=(
                {
                    "key": "tushare_adj_factor_native",
                    "version": 1,
                    "status": "inactive",
                    "cutoff_rule": "effective_date <= data_cutoff",
                },
            ),
            capability_sources={
                capability: CapabilitySource.PRODUCTION
                for capability in (
                    DataCapability.BARS,
                    DataCapability.MAPPINGS,
                    DataCapability.RULES,
                    DataCapability.STATUS,
                    DataCapability.ACTIONS,
                    DataCapability.COVERAGE,
                    DataCapability.CALENDARS,
                )
            },
        )

    def list_rule_facts(self, instrument_id, package_reference, *, start_date, end_date, data_cutoff):
        return self.rule_facts_repository.list_facts(instrument_id, package_reference, start_date=start_date, end_date=end_date, data_cutoff=data_cutoff)

    def resolve_exception_set(self, set_reference, *, data_cutoff):
        return self.exception_repository.load_exception_set(set_reference, data_cutoff=data_cutoff)

    def _database_revision_vector(self) -> Mapping[str, object]:
        """Return a bounded revision vector for all run fact dependencies.

        The vector contains only aggregate counts and source/timestamp
        watermarks.  It is intentionally global rather than row data: a
        mutation can expire a run safely without copying prices, payloads, or
        credentials into the consistency evidence.
        """

        # ponytail: global aggregate vector, replace with per-scope revision
        # watermarks if fact-table scans become measurable at production scale.
        def table_vector(model, *fields: str) -> Mapping[str, object]:
            expressions = [func.count().label("row_count")]
            names = []
            for field in fields:
                column = getattr(model, field, None)
                if column is not None:
                    expressions.append(func.max(column).label(field))
                    names.append(field)
            row = self.session.execute(select(*expressions)).one()
            return {
                "row_count": row[0],
                **{
                    field: _json_value(row[index + 1])
                    for index, field in enumerate(names)
                },
            }

        return {
            "mappings": table_vector(
                InstrumentCodeMappingRecord,
                "known_at", "observed_at", "source_revision",
            ),
            "bars": table_vector(EtfDailyBar, "updated_at", "source_revision"),
            "adjustments": table_vector(EtfAdjustmentFactor, "updated_at"),
            "trading_status": table_vector(
                TradingStatusFact,
                "known_at", "observed_at", "source_revision",
            ),
            "trading_status_audits": table_vector(
                TradingStatusFactRevisionAudit,
                "accepted_at", "source_revision",
            ),
            "trading_status_coverage": table_vector(
                TradingStatusCoverageFact,
                "known_at", "observed_at", "source_revision",
            ),
            "corporate_actions": table_vector(
                CorporateActionFact,
                "known_at", "observed_at", "created_at", "source_revision",
            ),
            "corporate_action_coverage": table_vector(
                CorporateActionCoverageFact,
                "known_at", "observed_at", "computed_at", "source_revision",
            ),
            "rules": table_vector(
                InstrumentRuleFactRecord,
                "known_at", "observed_at", "created_at", "source_revision",
            ),
            "calendar_days": table_vector(TradingCalendarDay, "updated_at"),
            "calendar_heads": table_vector(
                CalendarResolutionHeadRecord,
                "updated_at", "revision_digest",
            ),
        }

    def _consistency_digest(
        self,
        request: DataRequest,
        report: DataPreflightReport,
        *,
        revision_vector: Mapping[str, object],
        chunk_index: int,
        first_session_id: str,
        last_session_id: str,
        fact_types: Sequence[DataCapability],
    ) -> str:
        """Hash the complete SQL consistency scope without exposing row data."""

        dependencies = tuple(
            sorted(
                set(request.required_capabilities) | set(fact_types),
                key=lambda item: item.value,
            )
        )
        return canonical_hash(
            {
                "contract": {
                    "key": DATA_TOKEN_CONTRACT.key,
                    "version": DATA_TOKEN_CONTRACT.version,
                },
                "preflight_report_hash": report.report_hash,
                "revision_vector": revision_vector,
                "query_boundary": {
                    "data_cutoff": request.query_boundary.data_cutoff,
                    "knowledge_as_of": request.query_boundary.knowledge_as_of,
                    "include_cutoff_day": request.query_boundary.include_cutoff_day,
                },
                "formal_sessions": [
                    point.session_id for point in report.resolved_sessions
                ],
                "warmup_sessions": [
                    point.session_id for point in report.warmup_sessions
                ],
                "history_envelope": {
                    "requested_window": request.requested_window,
                    "max_lookback_sessions": request.max_lookback_sessions,
                },
                "dependencies": [item.value for item in dependencies],
                "chunk": {
                    "index": chunk_index,
                    "first_session_id": first_session_id,
                    "last_session_id": last_session_id,
                    "fact_types": [item.value for item in fact_types],
                },
            }
        )

    def _trading_status_applicability(self, request, effective_date):
        """Fold PIT rule declarations into one explicit status requirement map."""

        declarations = {
            "suspension": "not_applicable",
            "opening_availability": "not_applicable",
            "price_limit_tradability": "not_applicable",
        }
        for instrument_id in request.fixed_instrument_ids:
            try:
                spec = self.spec_provider.resolve_spec(
                    instrument_id,
                    effective_at=datetime.combine(effective_date, time(15), tzinfo=UTC),
                    data_cutoff=request.query_boundary.data_cutoff,
                    rule_package_reference=request.rule_package,
                    exception_set_reference=request.rule_exception_set,
                )
            except Exception:
                continue
            policy = getattr(spec, "trading_status_policy", {}) if spec is not None else {}
            for dimension, requirement in getattr(policy, "items", lambda: ())():
                value = getattr(requirement, "value", requirement)
                if value == "required":
                    declarations[str(dimension)] = "required"
        return declarations

    def _trading_status(
        self,
        instrument_ids,
        start_date,
        end_date,
        data_cutoff,
        knowledge_as_of=None,
    ):
        """Read the latest status revision visible at both PIT boundaries."""

        ids = tuple(instrument_ids)
        if not ids:
            return ()
        rows = self.session.scalars(
            select(TradingStatusFact)
            .where(
                TradingStatusFact.instrument_id.in_(ids),
                or_(
                    and_(
                        TradingStatusFact.valid_from <= end_date,
                        or_(
                            TradingStatusFact.valid_to.is_(None),
                            TradingStatusFact.valid_to > start_date,
                        ),
                    ),
                    and_(
                        TradingStatusFact.valid_from.is_(None),
                        TradingStatusFact.trade_date >= start_date,
                        TradingStatusFact.trade_date <= end_date,
                    ),
                ),
                TradingStatusFact.observed_at <= data_cutoff,
                (
                    and_(
                        TradingStatusFact.known_at.is_not(None),
                        TradingStatusFact.known_at <= knowledge_as_of,
                    )
                    if knowledge_as_of is not None
                    else or_(
                        TradingStatusFact.known_at.is_(None),
                        TradingStatusFact.known_at <= data_cutoff,
                    )
                ),
            )
            .order_by(TradingStatusFact.trade_date, TradingStatusFact.ts_code)
        ).all()
        candidates = []
        for row in rows:
            resolved_id = row.instrument_id
            values = {
                name: getattr(row, name, None)
                for name in (
                    "ts_code", "trade_date", "status", "dimension", "valid_from",
                    "valid_to", "source", "source_revision", "quality_status",
                    "known_at", "observed_at", "fact_version",
                )
            }
            values["instrument_id"] = resolved_id
            candidates.append(SimpleNamespace(**values))

        # Corrections are recorded as immutable superseded snapshots.  They
        # must remain queryable when the replacement was learned after the
        # requested cutoff, otherwise an old PIT run would silently lose the
        # prior status fact.
        audit_rows = self.session.scalars(
            select(TradingStatusFactRevisionAudit).where(
                TradingStatusFactRevisionAudit.previous_instrument_id.in_(ids),
                or_(
                    and_(
                        TradingStatusFactRevisionAudit.previous_valid_from <= end_date,
                        or_(
                            TradingStatusFactRevisionAudit.previous_valid_to.is_(None),
                            TradingStatusFactRevisionAudit.previous_valid_to > start_date,
                        ),
                    ),
                    and_(
                        TradingStatusFactRevisionAudit.previous_valid_from.is_(None),
                        TradingStatusFactRevisionAudit.trade_date >= start_date,
                        TradingStatusFactRevisionAudit.trade_date <= end_date,
                    ),
                ),
                TradingStatusFactRevisionAudit.previous_observed_at <= data_cutoff,
                (
                    and_(
                        TradingStatusFactRevisionAudit.previous_known_at.is_not(None),
                        TradingStatusFactRevisionAudit.previous_known_at <= knowledge_as_of,
                    )
                    if knowledge_as_of is not None
                    else or_(
                        TradingStatusFactRevisionAudit.previous_known_at.is_(None),
                        TradingStatusFactRevisionAudit.previous_known_at <= data_cutoff,
                    )
                ),
            )
        ).all()
        candidates.extend(
            SimpleNamespace(
                instrument_id=row.previous_instrument_id,
                ts_code=row.ts_code,
                trade_date=row.trade_date,
                status=row.previous_status,
                dimension=row.previous_dimension,
                valid_from=row.previous_valid_from,
                valid_to=row.previous_valid_to,
                source=row.previous_source,
                source_revision=row.previous_source_revision,
                quality_status=row.previous_quality_status,
                known_at=row.previous_known_at,
                observed_at=row.previous_observed_at,
                fact_version=None,
            )
            for row in audit_rows
        )

        def aware_rank(value):
            if not isinstance(value, datetime):
                return datetime.min.replace(tzinfo=UTC)
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

        selected = {}
        for row in candidates:
            key = (row.instrument_id, row.dimension, row.trade_date)
            rank = (
                aware_rank(row.known_at),
                aware_rank(row.observed_at),
            )
            previous = selected.get(key)
            previous_rank = (
                (aware_rank(previous.known_at), aware_rank(previous.observed_at))
                if previous is not None
                else None
            )
            if previous is None or rank > previous_rank:
                selected[key] = row
        return tuple(
            sorted(
                selected.values(),
                key=lambda row: (row.trade_date, str(row.instrument_id), row.dimension),
            )
        )

    def _trading_status_coverage(
        self,
        instrument_ids,
        start_date,
        end_date,
        data_cutoff,
        knowledge_as_of=None,
    ):
        """Read the latest status-window proof visible at both PIT boundaries."""

        rows = self.session.scalars(
            select(TradingStatusCoverageFact)
            .where(
                TradingStatusCoverageFact.instrument_id.in_(tuple(instrument_ids)),
                TradingStatusCoverageFact.start_date <= start_date,
                TradingStatusCoverageFact.end_date >= end_date,
                TradingStatusCoverageFact.observed_at <= data_cutoff,
                (
                    and_(
                        TradingStatusCoverageFact.known_at.is_not(None),
                        TradingStatusCoverageFact.known_at <= knowledge_as_of,
                    )
                    if knowledge_as_of is not None
                    else or_(
                        TradingStatusCoverageFact.known_at.is_(None),
                        TradingStatusCoverageFact.known_at <= data_cutoff,
                    )
                ),
            )
            .order_by(
                TradingStatusCoverageFact.instrument_id,
                TradingStatusCoverageFact.dimension,
                TradingStatusCoverageFact.observed_at.desc(),
            )
        ).all()
        def aware(value):
            if not isinstance(value, datetime):
                return datetime.min.replace(tzinfo=UTC)
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

        selected = {}
        for row in rows:
            key = (
                row.instrument_id,
                row.dimension,
                row.start_date,
                row.end_date,
            )
            rank = (aware(row.known_at), aware(row.observed_at))
            if key not in selected or rank > selected[key][0]:
                selected[key] = (rank, row)
        return tuple(
            row
            for _, row in sorted(
                selected.values(),
                key=lambda item: (
                    str(item[1].instrument_id),
                    item[1].dimension,
                    item[1].start_date,
                    item[1].end_date,
                ),
            )
        )

    def check_required_trading_status_facts(
        self,
        instrument_id,
        dimensions,
        *,
        start_date,
        end_date,
        data_cutoff,
        knowledge_as_of=None,
    ):
        if not dimensions:
            return ()
        required = tuple(sorted(set(dimensions)))
        coverage = self._trading_status_coverage(
            (instrument_id,), start_date, end_date, data_cutoff, knowledge_as_of
        )
        proven = {
            row.dimension
            for row in coverage
            if row.status == "complete"
        }
        # Row-level observations cannot prove that an absent status means
        # tradable.  Only a complete, PIT-visible coverage assertion can close
        # that negative-space gap.
        return tuple(dimension for dimension in required if dimension not in proven)

    def _calendar_ids(self, request):
        ids = request.fixed_instrument_ids
        calendars = set()
        effective_at = datetime.combine(request.requested_window.start_date, time(15), tzinfo=UTC)
        for instrument_id in ids:
            identity = self.identity_repository.resolve_identity_at(instrument_id, effective_at=effective_at, data_cutoff=request.query_boundary.data_cutoff)
            if identity is not None and identity.calendar_id:
                calendars.add(identity.calendar_id)
        return tuple(sorted(calendars or set(request.market_scope.exchanges or DEFAULT_CALENDARS)))

    def preflight(self, request, *, profile=None, fixtures=()):
        if isinstance(request, DataRequest):
            request = self.intent_from_data_request(request)
        calendars = self._calendar_ids(request)
        bootstrap = self._bootstrap_request(request, calendars)
        from app.backtesting.data.sessions import AuthoritativeDataSession
        calendar_report = AuthoritativeDataSession(request=bootstrap, calendar_provider=self.calendar_provider).preflight()
        expected = tuple(point.session_date for point in calendar_report.resolved_sessions)
        coverage_reports, issues, bars_by_instrument, mappings_by_instrument, source_rows = [], [], {}, {}, []
        trading_status_rows = []
        corporate_action_rows = []
        trading_status_coverage: dict[str, object] = {}
        trading_status_applicability = self._trading_status_applicability(
            request, expected[0] if expected else request.requested_window.start_date
        )
        corporate_action_coverage: dict[str, object] = {}
        for instrument_id in request.fixed_instrument_ids:
            try:
                resolution = self.adapter.resolve(instrument_id, sessions=expected, data_cutoff=request.query_boundary.data_cutoff)
                mappings_by_instrument[instrument_id] = tuple(segment.mapping for segment in resolution.segments)
                for segment in resolution.segments:
                    source_rows.extend(self.daily_repository.list_bars(source="tushare", ts_code=segment.source_code, start_date=segment.requested_sessions[0], end_date=segment.requested_sessions[-1]))
                summary = self.adapter.preflight_bars(instrument_id, resolution=resolution)
                missing = {date.fromisoformat(x) for x in summary.get("missing_sessions", ()) if isinstance(x, str)}
                bars_by_instrument[instrument_id] = [day for day in expected if day not in missing]
                coverage_reports.append(self.adapter.project_coverage_report(instrument_id, expected, summary, SimpleNamespace(formal_envelope=request.requested_window, history_envelope=request.requested_window)))
            except Exception as exc:
                issues.append(PreflightIssue(code="coverage_incomplete", severity=__import__("app.backtesting.data.requests", fromlist=["IssueSeverity"]).IssueSeverity.ERROR, scope="formal", message="ETF 行情覆盖预检失败，已阻断回测。", field="coverage.bars", instrument_id=instrument_id, details={"error_type": type(exc).__name__}))

        if expected and any(
            value == "required" for value in trading_status_applicability.values()
        ):
            for instrument_id in request.fixed_instrument_ids:
                rows = self._trading_status(
                    (instrument_id,), expected[0], expected[-1],
                    request.query_boundary.data_cutoff,
                    request.query_boundary.knowledge_as_of,
                )
                trading_status_rows.extend(rows)
                proofs = self._trading_status_coverage(
                    (instrument_id,), request.requested_window.start_date,
                    request.requested_window.end_date,
                    request.query_boundary.data_cutoff,
                    request.query_boundary.knowledge_as_of,
                )
                trading_status_coverage[str(instrument_id)] = [
                    {
                        "dimension": row.dimension,
                        "start_date": row.start_date,
                        "end_date": row.end_date,
                        "status": row.status,
                        "event_count": row.event_count,
                        "source": row.source,
                        "source_revision": row.source_revision,
                        "known_at": row.known_at,
                        "observed_at": row.observed_at,
                        "evidence": row.evidence,
                    }
                    for row in proofs
                ]
                missing_status = self.check_required_trading_status_facts(
                    instrument_id,
                    tuple(
                        dimension
                        for dimension, value in trading_status_applicability.items()
                        if value == "required"
                    ),
                    start_date=request.requested_window.start_date,
                    end_date=request.requested_window.end_date,
                    data_cutoff=request.query_boundary.data_cutoff,
                    knowledge_as_of=request.query_boundary.knowledge_as_of,
                )
                for dimension in missing_status:
                    issues.append(
                        PreflightIssue(
                            code="trading_status_coverage_incomplete",
                            severity=IssueSeverity.ERROR,
                            scope="formal",
                            message="规则声明适用的交易状态事实或覆盖证明不完整，已阻断回测。",
                            field=f"trading_status.{dimension}",
                            instrument_id=instrument_id,
                            details={
                                "dimension": dimension,
                                "start_date": request.requested_window.start_date.isoformat(),
                                "end_date": request.requested_window.end_date.isoformat(),
                            },
                        )
                    )

        if DataCapability.ACTIONS in request.required_capabilities:
            corporate_action_rows = list(
                self.corporate_action_repository.list_facts(
                    request.fixed_instrument_ids,
                    request.requested_window.start_date,
                    request.requested_window.end_date,
                    cutoff=request.query_boundary.data_cutoff,
                    knowledge_as_of=request.query_boundary.knowledge_as_of,
                )
            )
            proofs = self.corporate_action_repository.coverage(
                request.fixed_instrument_ids,
                request.requested_window.start_date,
                request.requested_window.end_date,
                cutoff=request.query_boundary.data_cutoff,
                knowledge_as_of=request.query_boundary.knowledge_as_of,
            )
            corporate_action_coverage = {
                str(instrument_id): [
                    {
                        "action_type": row.action_type,
                        "start_date": row.start_date,
                        "end_date": row.end_date,
                        "status": row.status,
                        "event_count": row.event_count,
                        "source": row.source,
                        "source_revision": row.source_revision,
                        "known_at": row.known_at,
                        "observed_at": row.observed_at,
                        "evidence": row.evidence,
                        "summary": row.summary,
                    }
                    for row in proofs
                    if row.instrument_id == instrument_id
                ]
                for instrument_id in request.fixed_instrument_ids
            }
            for instrument_id in request.fixed_instrument_ids:
                if not any(
                    row.instrument_id == instrument_id and row.status == "complete"
                    for row in proofs
                ):
                    issues.append(
                        PreflightIssue(
                            code="corporate_action_coverage_incomplete",
                            severity=IssueSeverity.ERROR,
                            scope="formal",
                            message="公司行动覆盖证明未完整覆盖请求范围，已阻断回测。",
                            field="coverage.corporate_actions",
                            instrument_id=instrument_id,
                            details={
                                "start_date": request.requested_window.start_date.isoformat(),
                                "end_date": request.requested_window.end_date.isoformat(),
                            },
                        )
                    )
        summary = self.adapter.preflight_summary(
            instrument_ids=request.fixed_instrument_ids,
            expected_sessions=expected,
            bars_by_instrument=bars_by_instrument,
            mappings_by_instrument=mappings_by_instrument,
            daily_rows=source_rows,
            trading_status_rows=trading_status_rows,
            trading_status_coverage=trading_status_coverage or None,
            corporate_action_rows=corporate_action_rows,
            corporate_action_coverage=corporate_action_coverage or None,
            data_cutoff=request.query_boundary.data_cutoff,
            required_capabilities=request.required_capabilities,
            strategy_price_bases=request.strategy_price_bases,
            trading_status_applicability=trading_status_applicability,
            preflight_profile="formal@1",
            run_kind="backtest_run",
        )
        provider_issues = list(issues)
        for item in summary.get("issues", ()):
            if not isinstance(item, Mapping):
                continue
            provider_issues.append(
                PreflightIssue(
                    code=str(item.get("code", "provider_contract_violation")),
                    severity=IssueSeverity.ERROR,
                    scope="formal",
                    message="ETF 数据准入证据未通过，已阻断回测。",
                    field=str(item.get("field")) if item.get("field") else "provider",
                    details={
                        key: value
                        for key, value in item.items()
                        if key not in {"code", "field"}
                    },
                )
            )
        if summary.get("status") == "blocked" and not provider_issues:
            provider_issues.append(
                PreflightIssue(
                    code="coverage_incomplete",
                    severity=IssueSeverity.ERROR,
                    scope="formal",
                    message="ETF 数据覆盖证据未完整覆盖请求范围，已阻断回测。",
                    field="coverage",
                )
            )
        pit_status = summary.get("pit_status")
        pit_capability_map = {
            "daily_bars": DataCapability.BARS,
            "trading_status": DataCapability.STATUS,
            "corporate_actions": DataCapability.ACTIONS,
            "corporate_action_coverage": DataCapability.ACTIONS,
        }
        non_strict_capabilities = tuple(
            sorted(
                {
                    capability
                    for family, capability in pit_capability_map.items()
                    if isinstance(pit_status, Mapping)
                    and pit_status.get(family) == "non_strict"
                    and capability in request.required_capabilities
                },
                key=lambda item: item.value,
            )
        )
        return __import__("dataclasses").replace(
            calendar_report,
            status=(
                PreflightStatus.BLOCKED
                if provider_issues
                else calendar_report.status
            ),
            static_instrument_ids=request.static_instrument_ids,
            mandatory_instrument_ids=request.mandatory_instrument_ids,
            non_zero_initial_position_instrument_ids=request.non_zero_initial_position_instrument_ids,
            resolved_instruments=request.fixed_instrument_ids,
            coverage_reports=tuple(coverage_reports),
            source_revisions=(
                summary.get("source_revisions")
                if isinstance(summary.get("source_revisions"), Mapping)
                else {}
            ),
            non_strict_pit_capabilities=non_strict_capabilities,
            non_strict_pit=bool(non_strict_capabilities),
            quantity_action_integrity=(
                summary.get("quantity_action_integrity")
                if isinstance(summary.get("quantity_action_integrity"), Mapping)
                else None
            ),
            trading_status=(
                summary.get("trading_status")
                if isinstance(summary.get("trading_status"), Mapping)
                else None
            ),
            run_kind="backtest_run",
            preflight_profile_key="formal",
            preflight_profile_version=1,
            session_summary={
                "production_capabilities": {
                    "status": "complete",
                    "provider_key": self.provider_key,
                },
                "pit_status": pit_status,
                "coverage": summary.get("coverage"),
                "source_revisions": summary.get("source_revisions"),
                "adapter_preflight_status": summary.get("status"),
            },
            issues=tuple((*calendar_report.issues, *provider_issues)),
        )

    @staticmethod
    def _bootstrap_request(request, calendars):
        return DataRequest(provider_key=request.provider_key, requested_window=request.requested_window, frequency=request.frequency, rule_package=request.rule_package, market_scope=request.market_scope, universe_query_policy=request.universe_query_policy, instrument_scope_mode=request.instrument_scope_mode, required_capabilities=request.required_capabilities, strategy_price_bases=request.strategy_price_bases, consistency_mode=request.consistency_mode, consistency_token_contract=request.consistency_token_contract, query_boundary=request.query_boundary, static_instrument_ids=(), resolved_calendar_ids=calendars, resolved_timezone="Asia/Shanghai", admission_calendar_session_signature="0" * 64, admission_preflight_status=PreflightStatus.READY, admission_preflight_hash="0" * 64)

    @staticmethod
    def intent_from_data_request(request):
        return DataPreflightRequest(**{field.name: getattr(request, field.name) for field in fields(DataPreflightRequest)})

    def open_session(self, request):
        return SqlBacktestSession(self, request)


def _corporate_action_snapshot(
    data_session: Any,
    request: DataRequest,
) -> RunCorporateActionEventSnapshot:
    """Freeze fixed-scope actions through validated provider chunks."""
    instrument_ids = tuple(request.fixed_instrument_ids)
    formal_sessions = tuple(data_session.resolved_sessions)
    if not instrument_ids or not formal_sessions:
        return RunCorporateActionEventSnapshot.from_events(
            (),
            coverage_summary={
                "instrument_ids": [str(instrument_id) for instrument_id in instrument_ids],
                "event_count": 0,
            },
        )
    action_query = CorporateActionQuery(
        instrument_ids=instrument_ids,
        window=request.requested_window,
        boundary=request.query_boundary,
    )
    chunks = []
    with ExitStack() as stack:
        for chunk_index in range(0, len(formal_sessions), request.data_chunk_size_sessions):
            chunk_sessions = formal_sessions[
                chunk_index : chunk_index + request.data_chunk_size_sessions
            ]
            chunk = stack.enter_context(
                data_session.open_chunk(
                    DataChunkQuery(
                        chunk_index=chunk_index // request.data_chunk_size_sessions,
                        first_session_id=chunk_sessions[0].session_id,
                        last_session_id=chunk_sessions[-1].session_id,
                        fact_types=(DataCapability.ACTIONS,),
                    )
                )
            )
            status = chunk.validate_consistency()
            if status.status is not ConsistencyValidation.VALID:
                raise ProviderContractViolationError(
                    "corporate-action chunk consistency validation failed",
                    details={"chunk_index": chunk_index // request.data_chunk_size_sessions},
                )
            chunks.append(chunk)
        snapshot = RunCorporateActionEventSnapshot.from_chunk_sessions(
            chunks,
            lambda _chunk: action_query,
            coverage_summary={
                "instrument_ids": [str(instrument_id) for instrument_id in instrument_ids],
                "event_count": None,
            },
        )
    return replace(
        snapshot,
        coverage_summary={
            **snapshot.coverage_summary,
            "event_count": len(snapshot.cash_dividend_events),
        },
        snapshot_hash="",
    )


class SqlBacktestSession:
    def __init__(self, provider, request):
        self.provider, self.request = provider, request
        self._state, self._report, self._resolved_sessions, self._warmup_sessions = "created", None, (), ()
        self._revision_vector = None
    def __enter__(self): return self
    def __exit__(self, *_): self._state = "closed"
    def close(self): self._state = "closed"
    @property
    def resolved_sessions(self): return self._resolved_sessions
    @property
    def warmup_sessions(self): return self._warmup_sessions
    @property
    def report(self): return self._report
    def preflight(self, _request=None):
        self._report = self.provider.preflight(
            self.provider.intent_from_data_request(self.request)
        )
        self._resolved_sessions = self._report.resolved_sessions
        self._warmup_sessions = self._report.warmup_sessions
        ready = self._report.status is PreflightStatus.READY
        self._revision_vector = (
            self.provider._database_revision_vector() if ready else None
        )
        self._state = "ready" if ready else "blocked"
        return self._report
    def open_chunk(self, query):
        if self._state != "ready":
            raise ProviderContractViolationError(
                "a blocked SQL data session exposes no official chunks"
            )
        if not isinstance(query, DataChunkQuery):
            raise ProviderContractViolationError("chunk query has an invalid type")
        size = self.request.data_chunk_size_sessions
        start = query.chunk_index * size
        end = min(start + size, len(self._resolved_sessions))
        if start < 0 or start >= end:
            raise ProviderContractViolationError("chunk index is outside frozen sessions")
        if (
            query.first_session_id != self._resolved_sessions[start].session_id
            or query.last_session_id != self._resolved_sessions[end - 1].session_id
        ):
            raise ProviderContractViolationError(
                "chunk boundaries do not match the frozen SQL sessions"
            )
        return SqlBacktestChunkSession(
            self.provider,
            self,
            query.chunk_index,
            tuple(self._resolved_sessions[start:end]),
            query.fact_types,
        )


class SqlBacktestChunkSession:
    def __init__(self, provider, session, index, sessions, fact_types):
        self.provider, self.session, self.index, self.sessions = provider, session, index, sessions
        self._fact_types = tuple(fact_types)
        self.validated = False
        self.closed = False
        self.current_date = sessions[-1].session_date
        dependencies = tuple(
            sorted(
                set(session.request.required_capabilities) | set(self._fact_types),
                key=lambda item: item.value,
            )
        )
        self._revision_vector = session._revision_vector
        self._digest = provider._consistency_digest(
            session.request,
            session.report,
            revision_vector=self._revision_vector,
            chunk_index=index,
            first_session_id=sessions[0].session_id,
            last_session_id=sessions[-1].session_id,
            fact_types=self._fact_types,
        )
        self._evidence = DataConsistencyEvidence(
            chunk_index=index,
            first_session_id=sessions[0].session_id,
            last_session_id=sessions[-1].session_id,
            mode=session.request.consistency_mode,
            validation_status=ConsistencyValidation.NOT_VALIDATED,
            fact_types=self._fact_types,
            coverage_summary={
                "chunk_session_count": len(sessions),
                "formal_session_ids": [
                    point.session_id for point in session.resolved_sessions
                ],
                "warmup_session_ids": [
                    point.session_id for point in session.warmup_sessions
                ],
                "dependency_fact_types": [item.value for item in dependencies],
                "data_cutoff": session.request.query_boundary.data_cutoff.isoformat(),
                "knowledge_as_of": (
                    session.request.query_boundary.knowledge_as_of.isoformat()
                    if session.request.query_boundary.knowledge_as_of is not None
                    else None
                ),
                "requested_window": {
                    "start_date": session.request.requested_window.start_date.isoformat(),
                    "end_date": session.request.requested_window.end_date.isoformat(),
                },
                "max_lookback_sessions": session.request.max_lookback_sessions,
                "fact_coverage_signature": canonical_hash(self._revision_vector),
            },
            token_digest=(
                self._digest
                if session.request.consistency_mode
                is ConsistencyMode.CHUNKED_LOGICAL_TOKEN
                else None
            ),
            failure_reason="consistency validation has not run yet",
        )
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    @property
    def consistency_evidence(self): return self._evidence
    def close(self): self.closed = True
    def begin_decision_step(self, step_key=None):
        if isinstance(step_key, date): self.current_date = step_key
    def authorize_step_candidates(self, *_args, **_kwargs): pass
    bind_step_candidates = authorize_step_candidates
    def validate_consistency(self):
        if self.closed:
            raise ProviderContractViolationError("data chunk is closed")
        now = datetime.now(UTC)
        current = self.provider._database_revision_vector()
        if current != self._revision_vector:
            reason = "database fact revisions changed after preflight"
            self._evidence = replace(
                self._evidence,
                validation_status=ConsistencyValidation.INVALID,
                validated_at=now,
                failure_reason=reason,
            )
            return ConsistencyTokenStatus(
                status=ConsistencyValidation.INVALID,
                validated_at=now,
                covered_chunk=self.index,
                covered_fact_types=self._evidence.fact_types,
                failure_reason=reason,
                covered_chunk_start=self.index,
                covered_chunk_end=self.index + 1,
            )
        self.validated = True
        self._evidence = replace(
            self._evidence,
            validation_status=ConsistencyValidation.VALID,
            validated_at=now,
            failure_reason=None,
        )
        return ConsistencyTokenStatus(
            status=ConsistencyValidation.VALID,
            validated_at=now,
            covered_chunk=self.index,
            covered_fact_types=self._evidence.fact_types,
            covered_chunk_start=self.index,
            covered_chunk_end=self.index + 1,
        )

    def _require(self):
        if self.closed or not self.validated:
            raise ProviderContractViolationError(
                "data chunk must pass consistency validation before reads"
            )
        if self.provider._database_revision_vector() != self._revision_vector:
            self.validated = False
            self._evidence = replace(
                self._evidence,
                validation_status=ConsistencyValidation.EXPIRED,
                failure_reason="database fact revisions changed during the chunk",
            )
            raise ProviderContractViolationError(
                "database fact revisions changed during the chunk"
            )
    def _dates(self, window):
        dates = tuple(x.session_date for x in (*self.session.warmup_sessions, *self.session.resolved_sessions))
        if isinstance(window, DateRange): return tuple(x for x in dates if window.start_date <= x <= window.end_date)
        return tuple(x for x in dates if x <= window.end_at.date())[-window.sessions:]
    def bars(self, query):
        self._require(); dates = self._dates(query.window); rows = []
        for instrument_id in query.instrument_ids:
            resolution = self.provider.adapter.resolve(instrument_id, sessions=dates, data_cutoff=query.boundary.data_cutoff); rows.extend(self.provider.adapter.bars(instrument_id, resolution=resolution).bars)
        return tuple(sorted(rows, key=lambda x: (x.trade_date, str(x.instrument_id))))
    def universe(self, query):
        self._require(); result = []
        for instrument_id in self.session.request.fixed_instrument_ids:
            spec = self.provider.spec_provider.resolve_spec(instrument_id, effective_at=datetime.combine(query.effective_date, time(15), tzinfo=ZoneInfo(self.session.request.resolved_timezone)), data_cutoff=query.boundary.data_cutoff, rule_package_reference=query.rule, exception_set_reference=query.rule_exception_set)
            if isinstance(spec, InstrumentSpec): result.append(spec)
        return tuple(result)
    def instrument_facts(self, instrument_ids, session_date=None):
        self._require()
        day = session_date or self.current_date
        result = {}
        status_by_instrument: dict[UUID, dict[str, str]] = {}
        if DataCapability.STATUS in self._fact_types:
            status_query = TradingStatusQuery(
                instrument_ids=tuple(instrument_ids),
                window=DateRange(day, day),
                boundary=self.session.request.query_boundary,
            )
            for fact in self.trading_status(status_query):
                dimension = str(fact.attributes.get("dimension", "suspension"))
                status_by_instrument.setdefault(fact.instrument_id, {})[dimension] = fact.status
        for instrument_id in instrument_ids:
            spec = self.provider.spec_provider.resolve_spec(
                instrument_id,
                effective_at=datetime.combine(
                    day, time(15), tzinfo=ZoneInfo(self.session.request.resolved_timezone)
                ),
                data_cutoff=self.session.request.query_boundary.data_cutoff,
                rule_package_reference=self.session.request.rule_package,
                exception_set_reference=self.session.request.rule_exception_set,
            )
            if spec is None:
                raise ProviderContractViolationError(
                    f"instrument spec is unavailable for {instrument_id}"
                )
            statuses = status_by_instrument.get(instrument_id, {})
            suspended = statuses.get("suspension") == "suspended"
            buy_allowed = statuses.get(
                "opening_availability", "available"
            ) not in {"unavailable", "closed", "blocked"}
            sell_allowed = buy_allowed
            result[instrument_id] = InstrumentFacts(
                instrument_id=instrument_id,
                price_tick=Decimal(str(spec.price_tick)),
                calendar_id=spec.calendar_id,
                suspended=suspended,
                buy_allowed=buy_allowed,
                sell_allowed=sell_allowed,
                board_lot=Decimal(str(spec.lot_size)),
                contract_multiplier=Decimal(str(spec.contract_multiplier)),
                fee_applicability_context={
                    "asset_class": spec.asset_class,
                    "exchange": spec.exchange,
                    "currency": spec.currency,
                },
            )
        return result
    def adjusted_series(self, _query): raise UnsupportedCapabilityError("formal v1 does not enable adjusted series")
    def trading_rules(self, _query): return ()
    def trading_status(self, query):
        self._require()
        if DataCapability.STATUS not in self._fact_types:
            raise ProviderContractViolationError(
                "trading-status reads were not declared for this chunk"
            )
        if not isinstance(query, TradingStatusQuery):
            raise ProviderContractViolationError(
                "trading-status query has an invalid type"
            )
        return self.provider.adapter.trading_status(query)
    def corporate_actions(self, query):
        self._require()
        if DataCapability.ACTIONS not in self._fact_types:
            raise ProviderContractViolationError(
                "corporate-action reads were not declared for this chunk"
            )
        if not isinstance(query, CorporateActionQuery):
            raise ProviderContractViolationError(
                "corporate-action query has an invalid type"
            )
        first = self.sessions[0].session_date
        last = self.sessions[-1].session_date
        start = max(first, query.window.start_date)
        end = min(last, query.window.end_date)
        if start > end:
            return ()
        return self.provider.adapter.corporate_actions(
            replace(query, window=DateRange(start, end))
        )


class SqlRuntimeViewFactory:
    def __init__(self, provider, data_session, request): self.provider, self.data_session, self.request, self.chunk, self._scope_ids = provider, data_session, request, None, request.fixed_instrument_ids
    def bind_chunk(self, chunk): self.chunk = chunk
    def unbind_chunk(self, _chunk): self.chunk = None
    def _require_chunk(self):
        if self.chunk is None: raise ProviderContractViolationError("runtime data view is outside a data chunk")
        return self.chunk
    def session_quotes(self, instrument_ids, session_date):
        rows = self._require_chunk().bars(BarQuery(instrument_ids=tuple(instrument_ids), frequency="1d", boundary=QueryBoundary(datetime.combine(session_date, time(15), tzinfo=ZoneInfo(self.request.resolved_timezone)), include_cutoff_day=True), window=DateRange(session_date, session_date)))
        return {row.instrument_id: SessionQuote(row.instrument_id, row.trade_date, row.open, row.close, {"source": row.evidence.source, "source_revision": row.evidence.source_revision}) for row in rows}
    def instrument_facts(self, instrument_ids): return self._require_chunk().instrument_facts(instrument_ids)
    def refresh_market_data(self, instrument_ids, session_date): return self.session_quotes(instrument_ids, session_date), self._require_chunk().instrument_facts(instrument_ids, session_date)
    def universe(self): return SimpleNamespace(query=lambda **_: ())
    def for_phase(self, instruction, step, *, next_step):
        day = date.fromisoformat(str(step.metadata["session_date"])); self._require_chunk().begin_decision_step(day)
        if instruction.data_view is None: return None
        if instruction.data_view.value == "strategy":
            raw = DataUniverseQuery(rule=self.request.rule_package, market_scope=self.request.market_scope, effective_date=day, boundary=QueryBoundary(instruction.timestamp, knowledge_as_of=self.request.query_boundary.knowledge_as_of, include_cutoff_day=True), allowed_calendar_ids=self.request.resolved_calendar_ids, universe_query_policy=self.request.universe_query_policy, rule_exception_set=self.request.rule_exception_set, scope_mode=self.request.instrument_scope_mode)
            view = __import__("app.backtesting.data.views", fromlist=["ChunkStrategyDataView"]).ChunkStrategyDataView(chunk=self._require_chunk(), frequency=self.request.frequency, data_cutoff=instruction.timestamp, include_cutoff_day=True, effective_date=day, universe_query=raw)
            return __import__("app.strategy_protocol.data_view", fromlist=["StrategyDataDTO"]).StrategyDataDTO(view, data_cutoff=instruction.timestamp, universe=view.universe(raw))
        return EngineDataView(quotes=self.session_quotes(self._scope_ids, day), facts=self._require_chunk().instrument_facts(self._scope_ids, day), session_date=day)


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    runner: DeterministicBacktestRunner
    data_session: SqlBacktestSession
    writer: Any


def default_components(
    slippage_model: ComponentSelection | None = None,
) -> dict[str, Any]:
    """Return the complete v1 snapshot with the selected registered slippage."""

    selected_slippage = slippage_model or ComponentSelection(
        "none", 1, {"price_tick": "0.01"}
    )
    selections = {
        TIMING_POLICY_KIND: (
            "after_close_to_next_open",
            1,
            {},
        ),
        EXECUTION_MODEL_KIND: (
            "bar_market",
            1,
            {
                "commission_rate": "0.0003",
                "commission_minimum": "5",
            },
        ),
        # Formal sizing must use the interpreter that consumes frozen
        # per-instrument rules, including contract_multiplier.
        DECISION_INTERPRETER_KIND: (
            "long_only_target_weights",
            1,
            {"weight_sum_tolerance": "0"},
        ),
        SLIPPAGE_MODEL_KIND: (
            selected_slippage.key,
            selected_slippage.version,
            dict(selected_slippage.parameters),
        ),
    }
    registry = build_default_component_registry()
    snapshot: dict[str, Any] = {}
    for kind, (key, version, requested_parameters) in selections.items():
        entry = registry.resolve(key, version)
        if entry.component_kind != kind:
            raise RegistryError(f"component {key}@{version} is not a {kind}")
        parameters = dict(requested_parameters)
        properties = entry.parameter_schema.get("properties", {})
        if isinstance(properties, Mapping):
            unknown = set(parameters) - set(properties)
            if unknown:
                raise RegistryError(
                    f"component {key}@{version} has unsupported parameters: "
                    f"{', '.join(sorted(unknown))}"
                )
            for name, definition in properties.items():
                if (
                    name not in parameters
                    and isinstance(definition, Mapping)
                    and "default" in definition
                ):
                    parameters[name] = definition["default"]
        missing = [
            name
            for name in entry.parameter_schema.get("required", ())
            if name not in parameters
        ]
        if missing:
            raise RegistryError(
                f"component {key}@{version} is missing parameters: "
                f"{', '.join(sorted(missing))}"
            )
        # Construction validates values after schema defaults are materialized;
        # the instance is discarded because workers rebuild from the snapshot.
        entry.construct(parameters)
        snapshot[kind] = {
            "key": entry.key,
            "version": entry.version,
            "kind": entry.component_kind,
            "name_zh": entry.name_zh,
            "name_en": entry.name_en,
            "display_name": entry.display_name,
            "parameter_schema": _json_value(entry.parameter_schema),
            "capabilities": _json_value(entry.capabilities),
            "parameters": _json_value(parameters),
        }
    snapshot[ANALYZER_COMPONENT_KIND] = []
    return snapshot


def _account_snapshot(session: Session, profile_id: UUID | None) -> dict[str, Any]:
    """Resolve one current account and detach every value needed by a run."""

    if profile_id is None:
        raise ValueError("account_profile_id is required for a formal run")
    record = AccountProfileService(session).get(profile_id)
    if str(record.status) != "active":
        raise ValueError("selected account profile is not active")
    schedule = fee_schedule_from_record(record)
    schedule.validate_for_run()
    snapshot = {
        "profile_id": str(record.id),
        "version": int(record.version or 1),
        "display_name": record.name,
        "status": str(record.status),
        "metadata": _json_value(record.profile_metadata or {}),
        "fee_schedule_key": record.fee_schedule_key,
        "fee_schedule_version": int(record.fee_schedule_version or 1),
        "fee_schedule": _json_value(schedule),
    }
    snapshot["snapshot_hash"] = canonical_hash(snapshot)
    return snapshot


def _fee_schedule_from_snapshot(account: Mapping[str, Any]) -> FeeSchedule:
    """Rehydrate the exact fee schedule embedded in a run snapshot."""

    payload = account.get("fee_schedule")
    if not isinstance(payload, Mapping):
        raise ProviderContractViolationError(
            "formal run is missing its frozen fee schedule snapshot"
        )
    try:
        schedule = FeeSchedule(
            key=str(payload["key"]),
            version=int(payload["version"]),
            metadata=dict(payload.get("metadata", {})),
            test_only=bool(payload.get("test_only", False)),
            fee_rules=tuple(
                FeeRule(**dict(rule))
                for rule in payload.get("fee_rules", ())
            ),
        )
        schedule.validate_for_run()
        return schedule
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderContractViolationError(
            "formal run fee schedule snapshot is invalid"
        ) from exc


def _behavior_versions(
    *, data_request: DataRequest, components: Mapping[str, Any], strategy: Mapping[str, Any], account: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the audit projection from the same resolved inputs as execution."""

    def ref(kind: str) -> dict[str, Any] | None:
        value = components.get(kind)
        if not isinstance(value, Mapping):
            return None
        return {"key": value.get("key"), "version": value.get("version")}

    return {
        "engine": {"key": "quant_foundry_backtesting_engine", "version": 1},
        "strategy_contract": strategy.get("contract_version"),
        "data_contract": data_request.data_contract_version,
        "calendar_axis_policy": _json_value(data_request.calendar_axis_policy),
        "time_axis": {"key": "trading_day", "version": 1},
        "timing_policy": ref(TIMING_POLICY_KIND),
        "execution_model": ref(EXECUTION_MODEL_KIND),
        "slippage_model": ref(SLIPPAGE_MODEL_KIND),
        "accounting_policy": {"key": "accounting_policy", "version": 1},
        "rule_package": _json_value(data_request.rule_package),
        "account_profile": {
            "id": account.get("profile_id"),
            "version": account.get("version"),
        },
        "fee_schedule": {
            "key": account.get("fee_schedule_key"),
            "version": account.get("fee_schedule_version"),
        },
    }


def _formal_gate_checks(
    report: DataPreflightReport,
    *,
    preflight_allowed: bool,
    strategy: Mapping[str, Any] | None = None,
    account: Mapping[str, Any] | None = None,
    components: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    """Project one formal preflight into the four production gate checks.

    DataPreflightService remains the authority for data facts.  This adapter
    only turns its frozen outcome and the already-resolved run inputs into the
    single GateOrchestrator contract consumed by the creation path.
    """

    phase1 = bool(strategy) and bool(account) and bool(components)
    phase2a = bool(preflight_allowed)
    formal_basic = phase1 and phase2a
    summary = report.session_summary if isinstance(report.session_summary, Mapping) else {}
    production = summary.get("production_capabilities")
    formal_complete = formal_basic and isinstance(production, Mapping) and production.get("status") == "complete"
    declared = summary.get("formal_gates")
    if isinstance(declared, Mapping) and "formal_complete" in declared:
        value = declared["formal_complete"]
        if isinstance(value, Mapping):
            value = value.get("allowed", value.get("status"))
        value = getattr(value, "value", value)
        formal_complete = formal_basic and value in {True, "ready", "available", "complete"}
    return {
        Gate.PHASE1.value: phase1,
        Gate.PHASE2A.value: phase2a,
        Gate.FORMAL_BASIC.value: formal_basic,
        Gate.FORMAL_COMPLETE.value: formal_complete,
    }


def _formal_gate_error(
    message: str,
    *,
    decision,
    report: DataPreflightReport,
) -> ValueError:
    """Attach structured gate evidence before the API translates the error."""

    error = ValueError(message)
    error.formal_gate_evidence = build_gate_evidence(
        decision,
        report_hash=report.report_hash,
        report_status=report.status,
        issues=report.issues,
    )
    return error


def binding_from_row(row, *, session: Session | None = None):
    """Rehydrate a binding exclusively from its persisted configuration."""

    config = row.backtest_config if isinstance(row.backtest_config, Mapping) else None
    if not config or config.get("schema_version") not in {1, 2}:
        raise ValueError("persisted run has no supported frozen configuration snapshot")
    if canonical_hash(config) != row.config_hash:
        raise ValueError("persisted run configuration snapshot hash mismatch")

    spec_payload = config.get("spec")
    strategy = config.get("strategy")
    components = config.get("components")
    data_payload = config.get("data_request")
    account = config.get("account")
    metadata = config.get("metadata")
    if not all(
        isinstance(value, Mapping)
        for value in (spec_payload, strategy, components, data_payload, account, metadata)
    ):
        raise ValueError("persisted run configuration snapshot is incomplete")

    request = deserialize_data_request(data_payload)
    positions = tuple(
        InitialPositionInput(
            instrument_id=UUID(str(item["instrument_id"])),
            side=PositionSide(str(item["side"])),
            quantity=item["quantity"],
            available_quantity=item.get("available_quantity", item["quantity"]),
            average_price=item.get("average_price"),
        )
        for item in spec_payload.get("initial_positions", [])
    )
    slippage_payload = spec_payload.get("slippage_model")
    if not isinstance(slippage_payload, Mapping):
        slippage_payload = components.get(SLIPPAGE_MODEL_KIND, {})
    strategy_revision_id = spec_payload.get("strategy_revision_id") or strategy.get(
        "revision_id"
    )
    account_profile_id = spec_payload.get("account_profile_id") or account.get(
        "profile_id"
    )
    spec = BacktestSpec(
        start_date=date.fromisoformat(str(spec_payload["start_date"])),
        end_date=date.fromisoformat(str(spec_payload["end_date"])),
        currency=str(spec_payload.get("currency", DEFAULT_CURRENCY)),
        timezone=str(spec_payload.get("timezone", request.resolved_timezone)),
        frequency=str(spec_payload.get("frequency", request.frequency)),
        warmup_sessions=int(
            spec_payload.get("warmup_sessions", request.warmup_sessions)
        ),
        initial_cash=spec_payload["initial_cash"],
        initial_positions=positions,
        dynamic_universe=bool(spec_payload.get("dynamic_universe", False)),
        instrument_ids=tuple(
            UUID(str(value))
            for value in spec_payload.get("instrument_ids", request.static_instrument_ids)
        ),
        exchanges=tuple(
            spec_payload.get("exchanges", request.market_scope.exchanges)
        ),
        strategy_price_bases=tuple(
            spec_payload.get(
                "strategy_price_bases",
                (basis.value for basis in request.strategy_price_bases),
            )
        ),
        strategy_revision_id=(
            UUID(str(strategy_revision_id)) if strategy_revision_id else None
        ),
        strategy_parameters=spec_payload.get(
            "strategy_parameters", strategy.get("parameters", {})
        ),
        account_profile_id=(
            UUID(str(account_profile_id)) if account_profile_id else None
        ),
        slippage_model=ComponentSelection(
            str(slippage_payload.get("key", "none")),
            int(slippage_payload.get("version", 1)),
            dict(slippage_payload.get("parameters", {})),
        ),
        random_seed=config.get("random_seed"),
    )
    rule_snapshot_bundle = None
    if session is not None and row.run_kind == "backtest_run":
        provider = SqlBacktestProvider(session)
        definition = provider.rule_registry.require(request.rule_package)
        rule_snapshot_bundle = RunRuleSnapshotRepository(session).load_bundle(
            UUID(str(row.id)),
            rule_package_definition=definition,
        )
    return SimpleNamespace(
        run_id=row.id,
        owner_scope=row.tenant_id,
        spec=spec,
        run_kind=row.run_kind,
        profile=row.profile,
        strategy=dict(strategy),
        components=dict(components),
        data_request=dict(data_payload),
        account=dict(account),
        metadata=dict(metadata),
        random_seed=config.get("random_seed"),
        config_hash=row.config_hash,
        status=getattr(row, "status", "queued"),
        rule_snapshot_bundle=rule_snapshot_bundle,
    )


def build_runtime(binding, *, session, launch_id, strategy_module, worker_id, progress_reporter=None):
    """Build the worker runtime from persisted, immutable run inputs."""

    del worker_id  # Worker identity belongs to supervision, not engine semantics.
    request = deserialize_data_request(binding.data_request)
    provider = SqlBacktestProvider(session)
    data_session = provider.open_session(request)
    data_session.preflight()
    corporate_actions = _corporate_action_snapshot(data_session, request)
    axis = TradingDayAxis(data_session.resolved_sessions)
    registry = build_default_component_registry()
    components = binding.components
    if not isinstance(components, Mapping):
        raise ProviderContractViolationError(
            "persisted run has no frozen component snapshot"
        )

    def component(kind, fallback):
        selected = components.get(kind)
        if selected is None:
            if binding.run_kind == "backtest_run":
                raise ProviderContractViolationError(
                    f"formal run is missing frozen component {kind}"
                )
            selected = {"key": fallback, "version": 1, "parameters": {}}
        if isinstance(selected, list):
            return ()
        entry = registry.resolve(selected["key"], int(selected["version"]))
        return entry.factory(selected.get("parameters", {}))

    timing = component(TIMING_POLICY_KIND, "after_close_to_next_open")
    execution = component(EXECUTION_MODEL_KIND, "bar_market")
    interpreter = component(
        DECISION_INTERPRETER_KIND, "long_only_target_weights"
    )
    slippage = component(SLIPPAGE_MODEL_KIND, "none")
    if binding.run_kind == "backtest_run":
        execution = replace(
            execution,
            fee_calculator=FeeCalculator(
                _fee_schedule_from_snapshot(binding.account)
            ),
        )
    if hasattr(execution, "slippage_model"):
        # Slippage is selected as its own versioned component; the execution
        # model must not keep a second hidden slippage configuration.
        execution = replace(execution, slippage_model=slippage)

    rule_snapshot_bundle = None
    if binding.run_kind == "backtest_run":
        definition = provider.rule_registry.require(request.rule_package)
        rule_snapshot_bundle = RunRuleSnapshotRepository(session).load_bundle(
            UUID(str(binding.run_id)),
            rule_package_definition=definition,
        )
        if rule_snapshot_bundle is None:
            raise ProviderContractViolationError(
                "formal run has no persisted frozen rule snapshot"
            )

    view = SqlRuntimeViewFactory(provider, data_session, request)
    writer = BacktestResultPersistenceService(
        session,
        BacktestResultContext(
            run_id=UUID(str(binding.run_id)),
            run_kind=binding.run_kind,
            profile=binding.profile,
            config_hash=binding.config_hash,
            owner_scope=binding.owner_scope,
            launch_id=launch_id,
        ),
    )
    strategy = __import__(
        "app.strategy_protocol.adapter", fromlist=["FunctionStrategyAdapter"]
    ).FunctionStrategyAdapter(
        strategy_module, parameters=binding.strategy.get("parameters", {})
    )
    dates = {
        calendar_id: tuple(
            item.session_date for item in data_session.resolved_sessions
        )
        for calendar_id in request.resolved_calendar_ids
    }
    settlement = SimpleNamespace(
        next_open_session=lambda calendar_id, after_session: next(
            (
                item
                for item in dates.get(calendar_id, ())
                if item > after_session
            ),
            None,
        )
    )
    runner = DeterministicBacktestRunner(
        run_id=str(binding.run_id),
        axis=axis,
        timing_policy=timing,
        view_factory=view,
        strategy=strategy,
        interpreter=interpreter,
        execution_model=execution,
        accounting=AccountingPolicy(
            currency=binding.spec.currency,
            settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH,
        ),
        initial_portfolio=_initial_portfolio(binding),
        settlement_calendar=settlement,
        corporate_actions=corporate_actions,
        fixed_authorized_instrument_ids=request.fixed_instrument_ids,
        rule_snapshot_bundle=rule_snapshot_bundle,
        result_sink=writer,
        progress_sink=progress_reporter,
    )
    return RuntimeBundle(runner, data_session, writer)


def _initial_portfolio(binding):
    positions = {
        item.instrument_id: PositionState(
            instrument_id=item.instrument_id,
            side=item.side,
            quantity=item.quantity,
            available_quantity=item.available_quantity,
            average_price=item.average_price,
        )
        for item in binding.spec.initial_positions
    }
    cash = Decimal(str(binding.spec.initial_cash))
    return PortfolioState(
        account=AccountState(
            cash_balances={binding.spec.currency: cash},
            available_cash=cash,
            frozen_cash=0,
            margin_used=0,
            margin_available=0,
            equity=cash,
        ),
        as_of=datetime.combine(
            binding.spec.start_date,
            time(9, 30),
            tzinfo=ZoneInfo(binding.spec.timezone),
        ),
        positions=positions,
    )


def execute_runtime(binding, *, session, launch_id, strategy_module, worker_id, progress_reporter=None):
    """Execute a run and convert runtime failures into auditable worker results."""

    from app.backtesting.runner_integrity import compute_result_integrity

    try:
        bundle = build_runtime(
            binding,
            session=session,
            launch_id=launch_id,
            strategy_module=strategy_module,
            worker_id=worker_id,
            progress_reporter=progress_reporter,
        )
        with bundle.data_session as data_session:
            result = run_data_session(
                data_session,
                bundle.runner,
                # Status is an engine dependency even when a decision does not
                # explicitly query it; a complete coverage proof makes the N/A path
                # explicit instead of treating missing rows as tradable.
                fact_types=(
                    DataCapability.BARS,
                    DataCapability.UNIVERSE,
                    DataCapability.STATUS,
                ),
                view_factory=bundle.runner._view_factory,
                analysis_coordinator=AnalysisFinalizationCoordinator(),
                analysis_session_factory=lambda: Session(bind=session.get_bind()),
            )
        session.commit()
        rows = BacktestResultRepository(session).read_integrity_rows(
            UUID(str(binding.run_id))
        )
        return {
            "category": "succeeded",
            "integrity": compute_result_integrity(
                rows, config_hash=binding.config_hash
            ).as_dict(),
            "result": result,
        }
    except Exception as exc:
        # ``run_steps`` has already flushed any completed prefix through the
        # result sink.  Re-read that durable prefix and publish a failed
        # marker so Supervisor can make a determinate failed decision instead
        # of losing the original exception behind a bare exit code.
        try:
            session.rollback()
        except Exception:
            pass
        rows = BacktestResultRepository(session).read_integrity_rows(
            UUID(str(binding.run_id))
        )
        evidence = build_failure_evidence(exc, default_phase="backtest_execution")
        return {
            "category": "failed",
            "integrity": compute_result_integrity(
                rows, config_hash=binding.config_hash
            ).as_dict(),
            "failure_phase": evidence["failure_phase"],
            "failure_type": evidence["error_type"],
            "failure_evidence": evidence,
            "result": None,
        }


@dataclass(frozen=True, slots=True)
class FormalBindingResult:
    binding: Any
    rule_snapshot_bundle: Any | None = None
    # Keep volatile audit timestamps outside the binding so config_hash remains
    # stable for idempotent retries of the same request.
    formal_gate_evidence: Mapping[str, Any] | None = None


def build_formal_binding(
    *,
    spec: BacktestSpec,
    revision: StrategyRevision,
    session: Session,
    degraded: bool = False,
    confirmed_report_hash: str | None = None,
) -> FormalBindingResult:
    """Resolve every mutable dependency once and return its frozen binding."""

    if spec.strategy_revision_id != revision.id:
        raise ValueError("strategy revision does not match the immutable run spec")
    provider = SqlBacktestProvider(session)
    ids = tuple(
        sorted(
            {
                *spec.instrument_ids,
                *(item.instrument_id for item in spec.initial_positions),
            },
            key=str,
        )
    )
    cutoff = datetime.now(UTC)
    intent = DataPreflightRequest(
        provider_key=provider.provider_key,
        requested_window=DateRange(spec.start_date, spec.end_date),
        frequency=spec.frequency,
        rule_package=DEFAULT_RULE_PACKAGE,
        market_scope=MarketScope(
            exchanges=tuple(spec.exchanges),
            asset_classes=("etf",),
            currencies=(spec.currency,),
        ),
        universe_query_policy=UniverseQueryPolicy(),
        instrument_scope_mode=(
            InstrumentScopeMode.DYNAMIC
            if spec.dynamic_universe
            else InstrumentScopeMode.FIXED
        ),
        required_capabilities=tuple(
            dict.fromkeys(
                (
                    DataCapability.BARS,
                    *(
                        (DataCapability.ADJUSTED_SERIES,)
                        if any(
                            basis != "raw" for basis in spec.strategy_price_bases
                        )
                        else ()
                    ),
                )
            )
        ),
        strategy_price_bases=tuple(
            PriceBasis(value) for value in spec.strategy_price_bases
        ),
        consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN,
        consistency_token_contract=DATA_TOKEN_CONTRACT,
        query_boundary=QueryBoundary(cutoff, include_cutoff_day=True),
        static_instrument_ids=ids,
        non_zero_initial_position_instrument_ids=tuple(
            item.instrument_id for item in spec.initial_positions
        ),
        warmup_sessions=spec.warmup_sessions,
    )
    rule_report = None
    if ids:
        from app.instruments.rule_preflight import (
            FixedInstrumentRulePreflightRequest,
            FixedInstrumentRulePreflightService,
        )

        rule_report = FixedInstrumentRulePreflightService(
            provider.rule_registry,
            provider,
            provider.spec_provider,
        ).run(
            FixedInstrumentRulePreflightRequest(
                instrument_ids=ids,
                start_date=spec.start_date,
                end_date=spec.end_date,
                data_cutoff=cutoff,
                rule_package_reference=DEFAULT_RULE_PACKAGE,
            )
        )
    from app.backtesting.data.preflight_service import (
        DataPreflightService,
        PreflightContext,
    )

    outcome = DataPreflightService(provider, profile="formal@1").admit(
        PreflightContext(
            request=intent,
            provider=provider,
            profile="formal@1",
            run_kind="backtest_run",
            rule_preflight_report=rule_report,
        ),
        confirmed_report_hash=confirmed_report_hash if degraded else None,
    )
    if not outcome.allowed:
        error = ValueError("formal backtest admission was blocked")
        error.admission_result = outcome
        raise error
    frozen = DataRequest.from_admission(
        intent,
        outcome.outcome.report,
        accepted_degraded=degraded,
        rule_preflight_report=rule_report,
    )
    # Calendar resolution is authoritative.  The request cannot retain a
    # caller-supplied timezone or frequency that disagrees with the admitted
    # data contract.
    spec = replace(
        spec,
        timezone=frozen.resolved_timezone,
        frequency=frozen.frequency,
        warmup_sessions=frozen.warmup_sessions,
    )

    from app.backtesting.run_binding import RunBindingBuilder

    strategy_binding = StrategyStorageService(session).bind_published_revision(
        revision.strategy_id,
        revision.id,
        parameters=spec.strategy_parameters,
    )
    # Defaults resolved from the immutable revision become explicit run input;
    # workers never re-read revision defaults after this point.
    spec = replace(spec, strategy_parameters=dict(strategy_binding.parameters))
    strategy = RunBindingBuilder().build_strategy(
        {
            "strategy_id": str(strategy_binding.strategy_id),
            "revision_id": str(strategy_binding.revision_id),
            "revision_number": strategy_binding.revision_number,
            "source_hash": strategy_binding.source_hash,
            "contract_version": strategy_binding.strategy_contract_version,
            "parameter_schema": _json_value(strategy_binding.parameter_schema),
            "parameters": _json_value(strategy_binding.parameters),
            "runtime_manifest": _json_value(strategy_binding.runtime_manifest),
            "published": True,
            "is_draft": False,
        }
    )
    account = _account_snapshot(session, spec.account_profile_id)
    components = default_components(spec.slippage_model)
    resolved_slippage = components[SLIPPAGE_MODEL_KIND]
    spec = replace(
        spec,
        slippage_model=ComponentSelection(
            str(resolved_slippage["key"]),
            int(resolved_slippage["version"]),
            dict(resolved_slippage["parameters"]),
        ),
    )
    gate_checks = _formal_gate_checks(
        outcome.outcome.report,
        preflight_allowed=outcome.allowed,
        strategy=strategy,
        account=account,
        components=components,
    )
    gate_decision, formal_gate_evidence = RunAdmissionService().evaluate_gates(
        run_kind="backtest_run",
        checks=gate_checks,
        report_hash=outcome.report_hash,
        report_status=outcome.outcome.status,
        issues=outcome.outcome.report.issues,
    )
    if not gate_decision.allowed:
        raise _formal_gate_error(
            "formal backtest admission gate was blocked",
            decision=gate_decision,
            report=outcome.outcome.report,
        )
    metadata = {
        "admission_report_hash": frozen.admission_preflight_hash,
        "preflight_hash": frozen.admission_preflight_hash,
        "data_evidence": _json_value(outcome.outcome.as_dict()),
        "data_preflight_status": frozen.admission_preflight_status.value,
        "behavior_versions": _behavior_versions(
            data_request=frozen,
            components=components,
            strategy=strategy,
            account=account,
        ),
    }
    binding = RunBindingBuilder().build(
        spec,
        run_kind="backtest_run",
        strategy=strategy,
        components=components,
        data_request=serialize_data_request(frozen),
        account=account,
        metadata=metadata,
    )
    return FormalBindingResult(
        binding,
        getattr(rule_report, "snapshot_bundle", None),
        formal_gate_evidence,
    )


__all__ = ["DATA_TOKEN_CONTRACT", "DEFAULT_RULE_PACKAGE", "FormalBindingResult", "RuntimeBundle", "SqlBacktestProvider", "SqlBacktestSession", "build_formal_binding", "build_runtime", "default_components", "deserialize_data_request", "execute_runtime", "serialize_data_request", "binding_from_row"]
