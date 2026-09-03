"""Production composition root for persisted formal backtest runs.

This module is the only place where storage-backed facts are composed with the
provider-independent engine. The isolated worker loads published strategy
source and invokes this composition root; the API never executes strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.backtesting.accounting import AccountState, AccountingPolicy, SettlementPolicy
from app.backtesting.data.adapters.etf import ETF_PROVIDER_KEY, EtfFactsAdapter
from app.backtesting.data.calendar_sql import SqlCalendarAxisDataProvider
from app.backtesting.data.errors import ProviderContractViolationError, UnsupportedCapabilityError
from app.backtesting.data.protocols import ConsistencyTokenStatus, DataConsistencyEvidence
from app.backtesting.data.requests import (
    BarQuery, CHUNK_POLICY, ConsistencyMode, ConsistencyValidation, ContractRef,
    DataCapability, DataChunkQuery, DataPreflightRequest, DataRequest, DateRange,
    InstrumentScopeMode, LookbackWindow, MarketScope, PriceBasis, PreflightStatus,
    QueryBoundary, UniverseQuery as DataUniverseQuery, UniverseQueryPolicy,
)
from app.backtesting.data.reports import DataCoverageReport, DataPreflightReport, PreflightIssue
from app.backtesting.domain import PositionSide, PositionState, PortfolioState
from app.backtesting.registry import (
    ANALYZER_COMPONENT_KIND, DECISION_INTERPRETER_KIND, EXECUTION_MODEL_KIND,
    SLIPPAGE_MODEL_KIND, TIMING_POLICY_KIND, build_default_component_registry,
)
from app.backtesting.result_repository import BacktestResultRepository
from app.backtesting.result_writer import BacktestResultContext, BacktestResultPersistenceService
from app.backtesting.runtime import BacktestViewFactory, DeterministicBacktestRunner, EngineDataView, InstrumentFacts, SessionQuote, run_data_session
from app.backtesting.time_axis import TradingDayAxis
from app.data_ingestion.models.etf_adjustment import EtfAdjustmentFactor
from app.data_ingestion.models.trading_calendar import TradingStatusFact
from app.data_ingestion.repositories.corporate_action import CorporateActionRepository
from app.data_ingestion.repositories.etf_daily import EtfDailyBarRepository
from app.instruments.domain import InstrumentSpec, VersionedReference
from app.instruments.identity_repository import InstrumentIdentityRepository
from app.instruments.repository import InstrumentCodeMappingRepository
from app.instruments.rule_exceptions_repository import RuleExceptionSetsRepository
from app.instruments.rule_facts_repository import RuleFactsRepository
from app.instruments.rules.etf_china import register_china_listed_etf_rules
from app.instruments.rules.registry import RulePackageRegistry
from app.instruments.spec_provider import InstrumentSpecProvider
from app.strategies.models import StrategyRevision

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
        self.adapter = EtfFactsAdapter(code_mappings=self._code_mappings, daily_bars=self._daily_bars, adjustment_factors=self._adjustment_factors, trading_days=self._trading_days, spec_provider=self.spec_provider, corporate_action_repository=self.corporate_action_repository)
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

    def list_rule_facts(self, instrument_id, package_reference, *, start_date, end_date, data_cutoff):
        return self.rule_facts_repository.list_facts(instrument_id, package_reference, start_date=start_date, end_date=end_date, data_cutoff=data_cutoff)

    def resolve_exception_set(self, set_reference, *, data_cutoff):
        return self.exception_repository.load_exception_set(set_reference, data_cutoff=data_cutoff)

    def check_required_trading_status_facts(self, instrument_id, dimensions, *, start_date, end_date, data_cutoff):
        if not dimensions:
            return ()
        mappings = self.mapping_repository.resolve_code_mappings(instrument_id, source="tushare", start_date=start_date, end_date=end_date, data_cutoff=data_cutoff)
        if len(mappings) != 1:
            return tuple(sorted(set(dimensions)))
        rows = self.session.scalars(select(TradingStatusFact).where(TradingStatusFact.ts_code == mappings[0].source_code, TradingStatusFact.trade_date >= start_date, TradingStatusFact.trade_date <= end_date)).all()
        return () if len({row.trade_date for row in rows}) >= (end_date - start_date).days + 1 else tuple(sorted(set(dimensions)))

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
        summary = self.adapter.preflight_summary(instrument_ids=request.fixed_instrument_ids, expected_sessions=expected, bars_by_instrument=bars_by_instrument, mappings_by_instrument=mappings_by_instrument, daily_rows=source_rows, data_cutoff=request.query_boundary.data_cutoff, required_capabilities=request.required_capabilities, strategy_price_bases=request.strategy_price_bases, preflight_profile="formal@1", run_kind="backtest_run")
        return __import__("dataclasses").replace(calendar_report, status=PreflightStatus.BLOCKED if issues else calendar_report.status, static_instrument_ids=request.static_instrument_ids, mandatory_instrument_ids=request.mandatory_instrument_ids, non_zero_initial_position_instrument_ids=request.non_zero_initial_position_instrument_ids, resolved_instruments=request.fixed_instrument_ids, coverage_reports=tuple(coverage_reports), source_revisions=summary.get("source_revisions") if isinstance(summary.get("source_revisions"), Mapping) else {}, non_strict_pit_capabilities=(DataCapability.BARS,), non_strict_pit=True, run_kind="backtest_run", preflight_profile_key="formal", preflight_profile_version=1, session_summary={"production_capabilities": {"status": "complete", "provider_key": self.provider_key}}, issues=tuple((*calendar_report.issues, *issues)))

    @staticmethod
    def _bootstrap_request(request, calendars):
        return DataRequest(provider_key=request.provider_key, requested_window=request.requested_window, frequency=request.frequency, rule_package=request.rule_package, market_scope=request.market_scope, universe_query_policy=request.universe_query_policy, instrument_scope_mode=request.instrument_scope_mode, required_capabilities=request.required_capabilities, strategy_price_bases=request.strategy_price_bases, consistency_mode=request.consistency_mode, consistency_token_contract=request.consistency_token_contract, query_boundary=request.query_boundary, static_instrument_ids=(), resolved_calendar_ids=calendars, resolved_timezone="Asia/Shanghai", admission_calendar_session_signature="0" * 64, admission_preflight_status=PreflightStatus.READY, admission_preflight_hash="0" * 64)

    @staticmethod
    def intent_from_data_request(request):
        return DataPreflightRequest(**{field.name: getattr(request, field.name) for field in fields(DataPreflightRequest)})

    def open_session(self, request):
        return SqlBacktestSession(self, request)


class SqlBacktestSession:
    def __init__(self, provider, request):
        self.provider, self.request = provider, request
        self._state, self._report, self._resolved_sessions, self._warmup_sessions = "created", None, (), ()
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
        self._report = self.provider.preflight(self.provider.intent_from_data_request(self.request)); self._resolved_sessions = self._report.resolved_sessions; self._warmup_sessions = self._report.warmup_sessions; self._state = "ready" if self._report.status is PreflightStatus.READY else "blocked"; return self._report
    def open_chunk(self, query):
        size = self.request.data_chunk_size_sessions; start = query.chunk_index * size; end = min(start + size, len(self._resolved_sessions)); return SqlBacktestChunkSession(self.provider, self, query.chunk_index, tuple(self._resolved_sessions[start:end]))


class SqlBacktestChunkSession:
    def __init__(self, provider, session, index, sessions):
        self.provider, self.session, self.index, self.sessions = provider, session, index, sessions; self.validated = False; self.closed = False; self.current_date = sessions[-1].session_date
        from app.backtesting.data.reports import canonical_hash
        self._digest = canonical_hash({"run": str(session.request.requested_window), "chunk": index})
        self._evidence = DataConsistencyEvidence(chunk_index=index, first_session_id=sessions[0].session_id, last_session_id=sessions[-1].session_id, mode=session.request.consistency_mode, validation_status=ConsistencyValidation.NOT_VALIDATED, fact_types=(DataCapability.BARS, DataCapability.UNIVERSE), coverage_summary={"chunk_session_count": len(sessions)}, token_digest=self._digest, failure_reason="consistency validation has not run yet")
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
        self.validated = True; now = datetime.now(UTC); self._evidence = __import__("dataclasses").replace(self._evidence, validation_status=ConsistencyValidation.VALID, validated_at=now, failure_reason=None); return ConsistencyTokenStatus(status=ConsistencyValidation.VALID, validated_at=now, covered_chunk=self.index, covered_fact_types=self._evidence.fact_types, covered_chunk_start=0, covered_chunk_end=len(self.sessions))
    def _require(self):
        if self.closed or not self.validated: raise ProviderContractViolationError("data chunk must pass consistency validation before reads")
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
        self._require(); day = session_date or self.current_date; result = {}
        for instrument_id in instrument_ids:
            spec = self.provider.spec_provider.resolve_spec(instrument_id, effective_at=datetime.combine(day, time(15), tzinfo=ZoneInfo(self.session.request.resolved_timezone)), data_cutoff=self.session.request.query_boundary.data_cutoff, rule_package_reference=self.session.request.rule_package, exception_set_reference=self.session.request.rule_exception_set)
            if spec is None: raise ProviderContractViolationError(f"instrument spec is unavailable for {instrument_id}")
            result[instrument_id] = InstrumentFacts(instrument_id=instrument_id, price_tick=Decimal(str(spec.price_tick)), calendar_id=spec.calendar_id, suspended=False, buy_allowed=True, sell_allowed=True, board_lot=Decimal(str(spec.lot_size)))
        return result
    def adjusted_series(self, _query): raise UnsupportedCapabilityError("formal v1 does not enable adjusted series")
    def trading_rules(self, _query): return ()
    def trading_status(self, _query): return ()
    def corporate_actions(self, query): return self.provider.adapter.corporate_actions(query)


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


def default_components():
    return {
        TIMING_POLICY_KIND: {"key": "after_close_to_next_open", "version": 1, "parameters": {}},
        EXECUTION_MODEL_KIND: {"key": "bar_market", "version": 1, "parameters": {"commission_rate": "0.0003", "commission_minimum": "5", "slippage_bps": "0", "price_tick": "0.01"}},
        DECISION_INTERPRETER_KIND: {"key": "target_weights", "version": 1, "parameters": {"board_lot": 100}},
        SLIPPAGE_MODEL_KIND: {"key": "none", "version": 1, "parameters": {"price_tick": "0.01"}},
        ANALYZER_COMPONENT_KIND: [],
    }


def binding_from_row(row):
    request = deserialize_data_request(row.data_request)
    spec = SimpleNamespace(start_date=request.requested_window.start_date, end_date=request.requested_window.end_date, initial_cash=Decimal(str(row.initial_cash or "0")), initial_positions=tuple(SimpleNamespace(instrument_id=UUID(str(x["instrument_id"])), side=PositionSide(str(x["side"])), quantity=Decimal(str(x["quantity"])), available_quantity=Decimal(str(x.get("available_quantity", x["quantity"]))), average_price=Decimal(str(x["average_price"])) if x.get("average_price") is not None else None) for x in (row.initial_positions or [])), dynamic_universe=request.instrument_scope_mode in (InstrumentScopeMode.DYNAMIC, InstrumentScopeMode.HYBRID))
    return SimpleNamespace(run_id=row.id, owner_scope=row.tenant_id, spec=spec, run_kind=row.run_kind, profile=row.profile, strategy={"revision_id": str(row.strategy_revision_id), "source_hash": row.strategy_source_hash, "contract_version": row.strategy_contract_version, "parameters": row.parameters or {}, "published": True, "is_draft": False}, components=default_components(), data_request=row.data_request, account=row.fee_schedule_snapshot or {}, metadata={"admission_report_hash": row.data_admission_preflight_hash, "preflight_hash": row.data_preflight_hash, "data_evidence": row.data_evidence or {}, "behavior_versions": row.behavior_versions or {}}, config_hash=row.config_hash, status=row.status)


def build_runtime(binding, *, session, launch_id, strategy_module, worker_id, progress_reporter=None):
    request = deserialize_data_request(binding.data_request); provider = SqlBacktestProvider(session); data_session = provider.open_session(request); report = data_session.preflight(); axis = TradingDayAxis(data_session.resolved_sessions); registry = build_default_component_registry(); components = binding.components or default_components()
    def component(kind, fallback):
        selected = components.get(kind, {"key": fallback, "version": 1, "parameters": {}})
        if isinstance(selected, list): return ()
        entry = registry.resolve(selected["key"], int(selected["version"])); return entry.factory(selected.get("parameters", {}))
    timing, execution, interpreter = component(TIMING_POLICY_KIND, "after_close_to_next_open"), component(EXECUTION_MODEL_KIND, "bar_market"), component(DECISION_INTERPRETER_KIND, "target_weights"); component(SLIPPAGE_MODEL_KIND, "none")
    view = SqlRuntimeViewFactory(provider, data_session, request); writer = BacktestResultPersistenceService(session, BacktestResultContext(run_id=UUID(str(binding.run_id)), run_kind=binding.run_kind, profile=binding.profile, config_hash=binding.config_hash, owner_scope=binding.owner_scope, launch_id=launch_id))
    strategy = __import__("app.strategy_protocol.adapter", fromlist=["FunctionStrategyAdapter"]).FunctionStrategyAdapter(strategy_module, parameters=binding.strategy.get("parameters", {}))
    dates = {cid: tuple(x.session_date for x in data_session.resolved_sessions) for cid in request.resolved_calendar_ids}
    settlement = SimpleNamespace(next_open_session=lambda calendar_id, after_session: next((x for x in dates.get(calendar_id, ()) if x > after_session), None))
    runner = DeterministicBacktestRunner(run_id=str(binding.run_id), axis=axis, timing_policy=timing, view_factory=view, strategy=strategy, interpreter=interpreter, execution_model=execution, accounting=AccountingPolicy(currency=DEFAULT_CURRENCY, settlement_policy=SettlementPolicy.T_PLUS_ONE_BEFORE_OPEN_MATCH), initial_portfolio=_initial_portfolio(binding), settlement_calendar=settlement, fixed_authorized_instrument_ids=request.fixed_instrument_ids, result_sink=writer, progress_sink=progress_reporter)
    return RuntimeBundle(runner, data_session, writer)


def _initial_portfolio(binding):
    positions = {x.instrument_id: PositionState(instrument_id=x.instrument_id, side=x.side, quantity=x.quantity, available_quantity=x.available_quantity, average_price=x.average_price) for x in binding.spec.initial_positions}
    cash = Decimal(str(binding.spec.initial_cash)); return PortfolioState(account=AccountState(cash_balances={DEFAULT_CURRENCY: cash}, available_cash=cash, frozen_cash=0, margin_used=0, margin_available=0, equity=cash), as_of=datetime.combine(binding.spec.start_date, time(9, 30), tzinfo=ZoneInfo("Asia/Shanghai")), positions=positions)


def execute_runtime(binding, *, session, launch_id, strategy_module, worker_id, progress_reporter=None):
    bundle = build_runtime(binding, session=session, launch_id=launch_id, strategy_module=strategy_module, worker_id=worker_id, progress_reporter=progress_reporter)
    with bundle.data_session as data_session:
        result = run_data_session(data_session, bundle.runner, fact_types=(DataCapability.BARS, DataCapability.UNIVERSE), view_factory=bundle.runner._view_factory)
    session.commit(); rows = BacktestResultRepository(session).read_integrity_rows(UUID(str(binding.run_id))); from app.backtesting.runner_integrity import compute_result_integrity
    return {"category": "succeeded", "integrity": compute_result_integrity(rows, config_hash=binding.config_hash).as_dict(), "result": result}


@dataclass(frozen=True, slots=True)
class FormalBindingResult:
    binding: Any
    rule_snapshot_bundle: Any | None = None


def build_formal_binding(*, spec, revision: StrategyRevision, raw_spec, session, degraded=False, confirmed_report_hash=None):
    provider = SqlBacktestProvider(session); ids = tuple(sorted({*(UUID(str(x)) for x in raw_spec.get("instrument_ids", ())), *(x.instrument_id for x in spec.initial_positions)}, key=str)); cutoff = datetime.now(UTC)
    intent = DataPreflightRequest(provider_key=provider.provider_key, requested_window=DateRange(spec.start_date, spec.end_date), frequency="1d", rule_package=DEFAULT_RULE_PACKAGE, market_scope=MarketScope(exchanges=tuple(raw_spec.get("exchanges", DEFAULT_CALENDARS)), asset_classes=("etf",), currencies=(DEFAULT_CURRENCY,)), universe_query_policy=UniverseQueryPolicy(), instrument_scope_mode=InstrumentScopeMode.DYNAMIC if spec.dynamic_universe else InstrumentScopeMode.FIXED, required_capabilities=(DataCapability.BARS,), strategy_price_bases=(PriceBasis.RAW,), consistency_mode=ConsistencyMode.CHUNKED_LOGICAL_TOKEN, consistency_token_contract=DATA_TOKEN_CONTRACT, query_boundary=QueryBoundary(cutoff, include_cutoff_day=True), static_instrument_ids=ids, non_zero_initial_position_instrument_ids=tuple(x.instrument_id for x in spec.initial_positions))
    rule_report = None
    if ids:
        from app.instruments.rule_preflight import FixedInstrumentRulePreflightRequest, FixedInstrumentRulePreflightService
        rule_report = FixedInstrumentRulePreflightService(provider.rule_registry, provider, provider.spec_provider).run(FixedInstrumentRulePreflightRequest(instrument_ids=ids, start_date=spec.start_date, end_date=spec.end_date, data_cutoff=cutoff, rule_package_reference=DEFAULT_RULE_PACKAGE))
    from app.backtesting.data.preflight_service import DataPreflightService, PreflightContext
    outcome = DataPreflightService(provider, profile="formal@1").admit(PreflightContext(request=intent, provider=provider, profile="formal@1", run_kind="backtest_run", rule_preflight_report=rule_report), confirmed_report_hash=confirmed_report_hash if degraded else None)
    if not outcome.allowed:
        error = ValueError("formal backtest admission was blocked"); error.admission_result = outcome; raise error
    frozen = DataRequest.from_admission(intent, outcome.outcome.report, accepted_degraded=degraded, rule_preflight_report=rule_report)
    from app.backtesting.run_binding import RunBindingBuilder
    strategy = RunBindingBuilder().build_strategy({"strategy_id": str(revision.strategy_id), "revision_id": str(revision.id), "revision_number": revision.revision_number, "source_hash": revision.source_hash, "contract_version": (revision.runtime_manifest or {}).get("strategy_contract_version"), "parameter_schema": revision.parameter_schema or {}, "parameters": raw_spec.get("parameters", revision.default_parameters or {}), "published": True, "is_draft": False})
    metadata = {"admission_report_hash": frozen.admission_preflight_hash, "preflight_hash": frozen.admission_preflight_hash, "data_evidence": _json_value(outcome.outcome.as_dict()), "behavior_versions": {"data_contract": "data_contract@1", "timing_policy": "after_close_to_next_open@1", "execution_model": "bar_market@1", "decision_interpreter": "target_weights@1", "slippage_model": "none@1", "accounting_policy": "accounting_policy@1"}}
    account = {"profile_id": "default_formal_account", "version": "1", "fee_schedule_key": "bar_market_flat_commission", "fee_schedule_version": "1", "fee_schedule": {"key": "bar_market_flat_commission", "version": 1, "rate": "0.0003", "minimum": "5"}}
    return FormalBindingResult(RunBindingBuilder().build(spec, run_kind="backtest_run", strategy=strategy, components=default_components(), data_request=serialize_data_request(frozen), account=account, metadata=metadata), getattr(rule_report, "snapshot_bundle", None))


__all__ = ["DATA_TOKEN_CONTRACT", "DEFAULT_RULE_PACKAGE", "FormalBindingResult", "RuntimeBundle", "SqlBacktestProvider", "SqlBacktestSession", "build_formal_binding", "build_runtime", "default_components", "deserialize_data_request", "execute_runtime", "serialize_data_request", "binding_from_row"]
