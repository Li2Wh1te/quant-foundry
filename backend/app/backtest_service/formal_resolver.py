"""Bounded, read-only composition of the existing formal SQL fact contracts.

Identity validity, calendar knowledge, a legacy ETF rule package, and action
coverage are different facts. Resolving one never supplies the missing fields
of another. In particular, this adapter cannot manufacture a ListingWindow,
ETF Product, raw prices, share conversion, or research publication time.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import json
import time as clock
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from .runtime_contracts import RuntimeFault
from .serialization import canonical_hash, encoded

ZONE = ZoneInfo('Asia/Shanghai')
FACT_ROWS = 10000
FACT_BYTES = 4 * 1024 * 1024
FACT_DAYS = 3660
FACT_CELLS = 20000
FACT_SECONDS = 5


def state(binding, status, *, missing=(), scope=None, code='CAPABILITY_UNAVAILABLE'):
    return dict(binding=binding, frequency=None, available=status == 'available',
                status=status, code=None if status == 'available' else code,
                missing=list(missing), scope=scope or {})


class FormalResolutionFault(RuntimeFault):
    def __init__(self, checks):
        super().__init__('CAPABILITY_UNAVAILABLE', '默认正式解析器已读取小事实；执行所需事实或字段语义不完整')
        self.checks = checks

    def diagnostic(self):
        return dict(super().diagnostic(), scope=dict(facts=self.checks))


def _row_payload(item):
    from sqlalchemy.dialects.postgresql import Range
    if isinstance(item, Range):
        return dict(lower=_row_payload(item.lower), upper=_row_payload(item.upper),
                    bounds=item.bounds, empty=item.empty)
    if hasattr(item, '__table__'):
        return {column.name: _row_payload(getattr(item, column.name)) for column in inspect(item).mapper.columns}
    if isinstance(item, (tuple, list)):
        return [_row_payload(value) for value in item]
    if hasattr(item, '_mapping'):
        return [_row_payload(value) for value in item]
    return item


class FactReadBudget:
    """Budget every ORM SELECT, including reads performed by shared repositories.

    FrozenResult preserves the normal ORM scalar/row interface. The extra row
    detects exhaustion; it is never returned as a silently truncated fact set.
    """
    def __init__(self):
        self.rows, self.bytes, self.parts = 0, 0, []
        self.deadline = clock.monotonic() + FACT_SECONDS

    def read(self, call):
        if clock.monotonic() >= self.deadline:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实读取超过时间预算')
        if not isinstance(call.statement, Select):
            raise RuntimeFault('INVALID_CONTRACT', '正式事实适配器只允许SQL读取')
        limit = FACT_ROWS - self.rows + 1
        if call.statement._limit_clause is not None:
            limit = min(limit, call.statement._limit_clause.value)
        try:
            result = call.invoke_statement(call.statement.limit(limit)).freeze()
        except DBAPIError as error:
            if getattr(error.orig, 'sqlstate', None) == '57014':
                raise RuntimeFault('RESOURCE_LIMIT', '正式小事实SQL读取超过时间预算') from None
            if getattr(error.orig, 'sqlstate', None) == '42P01':
                raise FormalResolutionFault([state('formal_sql_schema', 'fact_missing',
                    missing=('required_existing_fact_tables',))]) from None
            raise
        if clock.monotonic() >= self.deadline:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实读取超过时间预算')
        self.rows += len(result.data)
        if self.rows > FACT_ROWS:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实读取超过行预算')
        # Canonical conversion also covers UUIDs, dates, Decimal and PG ranges.
        payload = [_row_payload(item) for item in result.data]
        from app.instruments.rules.contracts import canonical_payload
        payload = canonical_payload(payload)
        size = len(encoded(payload))
        self.bytes += size
        if self.bytes > FACT_BYTES:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实读取超过字节预算')
        self.parts.append(canonical_hash(sorted(payload, key=lambda item: encoded(item))))
        return result()


@dataclass(frozen=True)
class ResolvedFormalFacts:
    checks: tuple
    identities: tuple
    calendars: tuple
    rule_packages: tuple
    trading_status: tuple
    corporate_actions: tuple
    commissions: tuple
    official_rules: tuple
    execution_calendar: object
    token: str


class SqlFormalPlanResolver:
    def __init__(self, settings, *, engine=None):
        self.settings, self.engine = settings, engine

    @contextmanager
    def _session(self, connection=None):
        from app.db.session import get_engine
        if connection is not None:
            # A final result transaction holds these existing small-fact
            # tables stable until it commits. No new snapshot/table is made.
            if connection.dialect.name == 'postgresql':
                connection.execute(text('LOCK TABLE instrument_code_mappings, instrument_identity_facts, '
                    'instrument_rule_facts, calendar_registry, calendar_definitions, calendar_session_facts, '
                    'calendar_source_priorities, trading_status_facts, trading_status_coverage_facts, '
                    'corporate_action_facts, corporate_action_coverage_facts IN SHARE MODE'))
            with Session(bind=connection) as session:
                budget = FactReadBudget()
                event.listen(session, 'do_orm_execute', budget.read, retval=True)
                yield session, budget
            return
        engine = self.engine or get_engine()
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                if connection.dialect.name == 'postgresql':
                    connection.execute(text('SET TRANSACTION READ ONLY'))
                    connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                with Session(bind=connection) as session:
                    budget = FactReadBudget()
                    event.listen(session, 'do_orm_execute', budget.read, retval=True)
                    yield session, budget

    def __call__(self, work):
        facts = self.resolve_facts(work)
        # There is no producer for these explicitly named execution fields in
        # the current registry. The SQL bridge remains useful independently;
        # its successes must not be advertised as an executable run plan.
        raise FormalResolutionFault(list(facts.checks))

    def check_facts(self, work, accepted, *, connection=None):
        current = self.resolve_facts(work, connection=connection)
        if current.token != accepted.token:
            raise RuntimeFault('DATA_CHANGED', '已解析的正式小事实、版本或质量发生变化')

    def resolve_facts(self, work, *, connection=None):
        with self._session(connection) as (session, budget):
            return self._resolve(session, budget, work)

    @staticmethod
    def _attempt(name, read, checks, *, scope):
        if len(encoded(checks)) > 256 * 1024:
            raise RuntimeFault('RESOURCE_LIMIT', '正式事实诊断超过控制消息预算')
        try:
            value = read()
        except RuntimeFault:
            raise
        except ValueError as error:
            code = str(getattr(error, 'code', str(error) if type(error) is ValueError else type(error).__name__))[:256]
            status = 'fact_missing' if 'missing' in code.lower() or 'gap' in code.lower() else 'fact_invalid'
            details = getattr(error, 'details', None) or getattr(error, 'scope', None)
            if details:
                from app.instruments.rules.contracts import canonical_payload
                details = canonical_payload(details)
                if len(encoded(details)) <= 2048:
                    scope = dict(scope, missing_scope=details)
            checks.append(state(name, status, missing=(code,), scope=scope))
            return None
        if value is None:
            checks.append(state(name, 'fact_missing', missing=(name,), scope=scope))
        else:
            checks.append(state(name, 'available', scope=scope))
        return value

    def _resolve(self, session, budget, work, *, inventory_asof=None):
        config = work.config
        start, end = date.fromisoformat(config['start']), date.fromisoformat(config['end'])
        days = (end - start).days + 1
        symbols = tuple(config['universe'])
        if not 1 <= days <= FACT_DAYS or len(symbols) * days > FACT_CELLS:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实日期或标的组合超过预算')
        checks, identities, calendars, rules, trading_status, actions, commissions = [], [], [], [], [], [], []
        from app.instruments.spec_provider import DEFAULT_MAPPING_SOURCE, DEFAULT_RULE_PACKAGE_REFERENCE
        from app.instruments.models import InstrumentCodeMappingRecord
        from app.instruments.repository import InstrumentCodeMappingRepository
        from app.instruments.identity_repository import InstrumentIdentityFactRepository
        from app.instruments.rule_facts_repository import RuleFactsRepository
        from app.instruments.rules.registry import RulePackageRegistry
        from app.instruments.rules.resolver import RulePackageResolver
        from app.instruments.rules.etf_china import register_china_listed_etf_rules
        registry = RulePackageRegistry()
        register_china_listed_etf_rules(registry)
        for symbol in symbols:
            for offset in range(days):
                day = start + timedelta(days=offset)
                at = datetime.combine(day, time(), ZONE)
                cutoff = inventory_asof or at  # facts known before this simulated day
                scope = dict(security=symbol, date=day.isoformat())
                def identity():
                    ids = set(session.scalars(select(InstrumentCodeMappingRecord.instrument_id).where(
                        func.lower(func.trim(InstrumentCodeMappingRecord.source)) == DEFAULT_MAPPING_SOURCE,
                        InstrumentCodeMappingRecord.source_code == symbol,
                        InstrumentCodeMappingRecord.known_at <= cutoff)))
                    found = []
                    for iid in sorted(ids, key=str):
                        mappings = InstrumentCodeMappingRepository(session).resolve_code_mappings(iid,
                            source=DEFAULT_MAPPING_SOURCE, start_date=day, end_date=day, data_cutoff=cutoff)
                        if not any(mapping.source_code == symbol for mapping in mappings):
                            continue
                        fact = InstrumentIdentityFactRepository(session).resolve_identity_at(iid,
                            effective_at=at, data_cutoff=cutoff)
                        if fact is not None:
                            found.append((fact, mappings))
                    if len(found) > 1:
                        raise ValueError('identity_mapping_ambiguous')
                    return found[0] if found else None
                pair = self._attempt('identity', identity, checks, scope=scope)
                if pair is None:
                    checks.extend(state(name, 'fact_missing', missing=('resolved_identity_dependency',),
                        scope=scope) for name in ('calendar_sessions', 'instrument_rule_package',
                                                 'suspension_facts', 'corporate_actions'))
                    continue
                fact, mappings = pair
                identities.append((symbol, day, fact, mappings))
                calendar = self._attempt('calendar_sessions',
                    lambda: self._calendar(session, fact.calendar_id, day, cutoff), checks, scope=scope)
                if calendar is not None:
                    calendars.append((symbol, day, calendar))
                def rule():
                    candidates = RuleFactsRepository(session).list_facts(fact.instrument_id,
                        DEFAULT_RULE_PACKAGE_REFERENCE, start_date=day, end_date=day, data_cutoff=cutoff)
                    if not candidates:
                        return None
                    resolved = RulePackageResolver(registry).resolve(DEFAULT_RULE_PACKAGE_REFERENCE,
                        instrument_id=fact.instrument_id, asset_class=fact.asset_class,
                        effective_date=day, data_cutoff=cutoff, facts=candidates)
                    from app.instruments.rules.contracts import ResolutionStatus
                    if resolved.status != ResolutionStatus.READY:
                        raise ValueError(','.join(issue.code.value for issue in resolved.issues))
                    return resolved
                resolved = self._attempt('instrument_rule_package', rule, checks, scope=scope)
                if resolved is not None:
                    rules.append((symbol, day, resolved))
                status = self._attempt('suspension_facts', lambda: self._status(session,
                    fact.instrument_id, symbol, day, cutoff), checks, scope=scope)
                if status is not None:
                    trading_status.append((symbol, day, status))
                action = self._attempt('corporate_actions', lambda: self._actions(session,
                    fact.instrument_id, day, cutoff), checks, scope=scope)
                if action is not None:
                    actions.append((symbol, day, action))
                if getattr(work, 'specification', None):
                    def commission():
                        from .account_config import commission_from_account_fee_schedule
                        schedule = work.specification['account_snapshot']['fee_schedule']
                        return tuple(commission_from_account_fee_schedule(schedule, side=side,
                            currency=fact.currency, applicability_context=dict(asset_class=fact.asset_class,
                                exchange=fact.exchange)) for side in ('buy', 'sell'))
                    value = self._attempt('account_commission', commission, checks, scope=scope)
                    if value is not None:
                        commissions.append((symbol, day, value))
                if len(encoded(checks)) > 256 * 1024:
                    raise RuntimeFault('RESOURCE_LIMIT', '正式事实诊断超过控制消息预算')
        # No mapping/row quantity, UUID date, current report or metadata flag
        # supplies these semantics. Failures distinguish code from producers.
        execution_calendar = self._attempt('execution_calendar_assembly', lambda:
            self._assemble_calendar(session, calendars, start, end, inventory_asof), checks,
            scope=dict(start=start.isoformat(), end=end.isoformat()))
        official = self._attempt('official_market_rules', self._official_rules, checks,
            scope=dict(kind='offline_catalog_only'))
        checks.extend(self._execution_gaps(config['frequency']))
        token = canonical_hash(dict(config=config, rows=budget.parts, checks=checks, official_rules=official))
        return ResolvedFormalFacts(tuple(checks), tuple(identities), tuple(calendars),
            tuple(rules), tuple(trading_status), tuple(actions), tuple(commissions),
            tuple(official or ()), execution_calendar, token)

    @staticmethod
    def _official_rules():
        try:
            from quantfoundry import _native
        except ImportError:
            return None
        if not hasattr(_native, 'official_market_rules_json'):
            return None
        payload = _native.official_market_rules_json()
        if len(payload.encode()) > 256 * 1024:
            raise RuntimeFault('RESOURCE_LIMIT', '正式规则目录超过预算')
        result = json.loads(payload)
        if type(result) is not list or len(result) > 1024:
            raise RuntimeFault('INVALID_CONTRACT', '正式规则目录格式无效')
        return result

    @staticmethod
    def _calendar(session, calendar_id, day, cutoff):
        from app.calendars.repository import CalendarFactRepository
        from app.calendars.facts import CalendarPITContext, select_pit_candidate
        from app.calendars.contracts import QueryBoundary
        repository = CalendarFactRepository(session)
        context = CalendarPITContext.from_query_boundary(QueryBoundary(
            # Calendar planning may read a pre-announced future session. Its
            # knowledge time remains the simulated instant, not that future day.
            data_cutoff=max(cutoff, datetime.combine(day, time(), ZONE)),
            knowledge_as_of=cutoff, include_cutoff_day=True))
        priorities = repository.list_source_priorities()
        def choose(rows):
            selected = select_pit_candidate(rows, effective_day=day, pit_context=context,
                source_priorities=priorities)
            selected.strict_validate()
            return selected
        registry = choose(repository.list_registries((calendar_id,)))
        definition = choose(repository.list_definitions((calendar_id,)))
        fact = choose(repository.list_session_facts((calendar_id,), day, day + timedelta(days=1)))
        if (definition.registry_fact_id != registry.fact_id or definition.registry_version != registry.registry_version
                or fact.registry_fact_id != registry.fact_id or fact.registry_version != registry.registry_version
                or fact.definition_fact_id != definition.fact_id or fact.definition_version != definition.definition_version):
            raise ValueError('calendar_reference_mismatch')
        timezone, windows = fact.effective_timezone_and_sessions(definition)
        # A narrow exact local-window projection, with no invented auction or
        # before/after callback phase. The D03 execution phase producer is separate.
        intervals = tuple((datetime.combine(day, window.start, ZoneInfo(timezone)),
                           datetime.combine(day, window.end, ZoneInfo(timezone))) for window in windows)
        return dict(registry=registry, definition=definition, fact=fact, timezone=timezone, intervals=intervals)

    @classmethod
    def _assemble_calendar(cls, session, resolved, start, end, inventory_asof):
        ids = {row[2]['fact'].calendar_id for row in resolved}
        if not ids:
            return None
        if len(ids) != 1:
            raise ValueError('single_reference_calendar_required')
        calendar_id = next(iter(ids))
        cutoff = inventory_asof or datetime.combine(start, time(), ZONE)
        month_start = start.replace(day=1)
        month_next = (end.replace(day=28) + timedelta(days=4)).replace(day=1)
        lower = min(month_start, start - timedelta(days=start.weekday()))
        upper = max(month_next - timedelta(days=1), end + timedelta(days=6-end.weekday()))
        selected = {}
        day = lower
        while day <= upper:
            selected[day] = cls._calendar(session, calendar_id, day, cutoff)
            day += timedelta(days=1)
        following = [day for day, value in selected.items() if day > end and value['fact'].is_open]
        day = upper + timedelta(days=1)
        while len(following) < 2 and day <= upper + timedelta(days=31):
            selected[day] = cls._calendar(session, calendar_id, day, cutoff)
            if selected[day]['fact'].is_open:
                following.append(day)
            day += timedelta(days=1)
        if len(following) < 2:
            raise ValueError('next_open_session_coverage_missing')
        open_days = sorted(day for day, value in selected.items() if value['fact'].is_open)
        def key(day):
            return calendar_id + ':' + day.isoformat()
        def ns(instant):
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            delta = instant.astimezone(UTC) - epoch
            return delta.days * 86400 * 1000000000 + delta.seconds * 1000000000 + delta.microseconds * 1000
        rows = []
        for day in open_days:
            if not start <= day <= end:
                continue
            value = selected[day]
            intervals = value['intervals']
            if not intervals:
                raise ValueError('open_session_windows_missing')
            week_start = day - timedelta(days=day.weekday())
            week_days = [at for at in open_days if week_start <= at < week_start + timedelta(days=7)]
            month_days = [at for at in open_days if (at.year, at.month) == (day.year, day.month)]
            windows = [dict(start_ns=str(ns(a)), end_ns=str(ns(b))) for a, b in intervals]
            rows.append(dict(date=day.isoformat(),
                session=dict(key=key(day), exchange_timezone=value['timezone'],
                    open_ns=windows[0]['start_ns'], close_ns=windows[-1]['end_ns']),
                local_midnight_ns=str(ns(datetime.combine(day, time(), ZoneInfo(value['timezone'])))),
                # Callback boundaries are logical instants adjacent to the
                # explicit source windows; no exchange auction is fabricated.
                before_open_ns=str(int(windows[0]['start_ns']) - 1),
                after_close_ns=str(int(windows[-1]['end_ns']) + 1),
                market_windows=windows, bar_windows=deepcopy(windows),
                week=dict(id=week_start.isoformat(), trading_day=week_days.index(day)+1,
                          total_trading_days=len(week_days)),
                month=dict(id=day.isoformat()[:7], trading_day=month_days.index(day)+1,
                           total_trading_days=len(month_days))))
        if not rows:
            raise ValueError('open_run_sessions_missing')
        from quantfoundry import _native
        rows = json.loads(_native.validate_formal_calendar_json(encoded(rows).decode()))
        return dict(calendar=rows, next_session=key(following[0]), following_session=key(following[1]),
            reference=calendar_id + ':' + canonical_hash([value['fact'].content_hash for _, value in sorted(selected.items())]))

    @staticmethod
    def _status(session, iid, symbol, day, cutoff):
        from app.data_ingestion.models.trading_calendar import TradingStatusFact, TradingStatusCoverageFact
        facts = tuple(session.scalars(select(TradingStatusFact).where(
            TradingStatusFact.instrument_id == iid, TradingStatusFact.ts_code == symbol,
            TradingStatusFact.trade_date == day, TradingStatusFact.dimension == 'suspension',
            TradingStatusFact.known_at <= cutoff, TradingStatusFact.observed_at <= cutoff)))
        coverage = tuple(session.scalars(select(TradingStatusCoverageFact).where(
            TradingStatusCoverageFact.instrument_id == iid, TradingStatusCoverageFact.dimension == 'suspension',
            TradingStatusCoverageFact.start_date <= day, TradingStatusCoverageFact.end_date >= day,
            TradingStatusCoverageFact.known_at <= cutoff, TradingStatusCoverageFact.observed_at <= cutoff).order_by(
                TradingStatusCoverageFact.known_at.desc(), TradingStatusCoverageFact.observed_at.desc())))
        if not facts and not coverage:
            return None
        if coverage:
            rank = (coverage[0].known_at, coverage[0].observed_at)
            latest = tuple(row for row in coverage if (row.known_at, row.observed_at) == rank)
            if len(latest) != 1:
                raise ValueError('suspension_coverage_ambiguous')
            coverage = latest
        for row in facts:
            if (row.quality_status != 'complete' or row.status not in {'suspended', 'tradable'}
                    or row.valid_from is None or row.valid_from > day
                    or row.valid_to is not None and day >= row.valid_to or not row.source_revision):
                raise ValueError('suspension_fact_incomplete')
        for row in coverage:
            if (row.status != 'complete' or not row.source_revision or not row.evidence or not row.validation_rule):
                raise ValueError('suspension_coverage_incomplete')
        # A zero-event proof is retained as coverage. It does not become the
        # missing opening/risk/price-limit or intraday execution state.
        return dict(facts=tuple(_row_payload(row) for row in facts),
                    coverage=tuple(_row_payload(row) for row in coverage))

    @staticmethod
    def _actions(session, iid, day, cutoff):
        from app.data_ingestion.repositories.corporate_action import CorporateActionRepository
        repository = CorporateActionRepository(session)
        coverage = repository.coverage((iid,), day, day, cutoff=cutoff, knowledge_as_of=cutoff)
        if not coverage:
            return None
        for row in coverage:
            if (row.status != 'complete' or row.event_count is None or not row.source_revision
                    or not row.known_at or not row.observed_at or not row.evidence or not row.validation_rule):
                raise ValueError('corporate_action_coverage_incomplete')
        events = repository.list_facts((iid,), day, day, cutoff=cutoff, knowledge_as_of=cutoff)
        for row in events:
            if (row.quality != 'complete' or not row.source_revision or not row.known_at or not row.observed_at
                    or not row.evidence or row.valid_from is None or row.record_date is None or row.ex_date is None
                    or not row.currency or not row.entitlement_rule or not row.timing_rule):
                raise ValueError('corporate_action_fields_incomplete')
            if row.cash_amount_per_unit is not None and (
                    row.cash_effective_date is None or not row.cash_effective_phase or not row.cash_date_rule):
                raise ValueError('corporate_action_cash_timing_incomplete')
        # Preserve whole approved rows, including quantity/cash date, phase,
        # currency, evidence and revision. Do not guess clock instants or taxes.
        return dict(coverage=tuple(_row_payload(row) for row in coverage),
                    events=tuple(_row_payload(row) for row in events))

    @staticmethod
    def _execution_gaps(frequency):
        from .market_projection import current_market_bindings
        result = []
        for binding in current_market_bindings():
            if frequency == '1d':
                result.append(dict(state(binding.name, 'field_semantics_missing',
                    missing=(binding.unavailable_reason,)), frequency=binding.frequency,
                    reason=binding.unavailable_reason, limitations=list(binding.limitations)))
        if frequency != '1d':
            result.append(state('market_source', 'adapter_missing', missing=('formal_' + frequency + '_source_contract',)))
        result.append(state('execution_trade_days', 'field_semantics_missing', missing=(
            'product_subtype', 'listing_window_and_session_ordinal', 'intraday_status_and_risk',
            'actual_price_limits', 'fund_price_band_and_underlying_session', 'delisting_session_ordinal')))
        result.append(state('execution_fee_scope', 'field_semantics_missing', missing=(
            'product_and_investor_kind', 'accepted_trading_permissions',
            'sale_cash_and_dividend_tax_timing')))
        result.append(state('research_publication', 'field_semantics_missing', missing=(
            'historical_publication_time', 'historical_effective_sets', 'adjustment_public_time_and_anchor')))
        result.append(state('execution_plan_assembly', 'adapter_missing', missing=(
            'source_and_trade_day_producers_to_AcceptedRunPlan',
            'formal_research_source_mapping_to_D05_bindings')))
        return result

    def capabilities(self):
        # Inventory is deliberately bounded and scoped. A validated one-day
        # sample proves that a bridge works, not that any requested run is ready.
        checks = self._execution_gaps('1d')
        with self._session() as (session, budget):
            from app.instruments.models import InstrumentCodeMappingRecord
            from app.instruments.spec_provider import DEFAULT_MAPPING_SOURCE
            now = datetime.now(UTC)
            row = session.scalars(select(InstrumentCodeMappingRecord).where(
                func.lower(func.trim(InstrumentCodeMappingRecord.source)) == DEFAULT_MAPPING_SOURCE,
                InstrumentCodeMappingRecord.known_at <= now).order_by(
                    InstrumentCodeMappingRecord.known_at.desc(), InstrumentCodeMappingRecord.id).limit(1)).first()
            if row is not None:
                day = min(now.astimezone(ZONE).date(), row.valid_to - timedelta(days=1)) if row.valid_to else now.astimezone(ZONE).date()
                day = max(day, row.valid_from)
                # Prefer an explicit session in this identity's mapping window
                # when the catalogue contains historical rather than today's facts.
                from app.calendars.models import CalendarSessionFactRecord
                from app.instruments.models import InstrumentIdentityFactRecord
                sample_day = session.scalar(select(CalendarSessionFactRecord.session_date).join(
                    InstrumentIdentityFactRecord,
                    InstrumentIdentityFactRecord.calendar_id == CalendarSessionFactRecord.calendar_id).where(
                        InstrumentIdentityFactRecord.instrument_id == row.instrument_id,
                        CalendarSessionFactRecord.session_date >= row.valid_from,
                        CalendarSessionFactRecord.session_date <= day,
                        CalendarSessionFactRecord.known_at <= now).order_by(CalendarSessionFactRecord.session_date.desc()).limit(1))
                day = sample_day or day
                work = SimpleNamespace(config=dict(universe=[row.source_code], frequency='1d',
                    start=day.isoformat(), end=day.isoformat()), specification={})
                facts = self._resolve(session, budget, work, inventory_asof=now)
                checks = list(facts.checks)
                for check in checks:
                    if check['scope']:
                        check['scope']['knowledge_profile'] = 'bounded_inventory_current_knowledge'
                        check['scope']['known_at_cutoff'] = now.isoformat()
            else:
                checks.extend(state(name, 'fact_missing', missing=(name,), scope=dict(
                    knowledge_profile='bounded_inventory_current_knowledge')) for name in (
                        'identity', 'calendar_sessions', 'instrument_rule_package', 'suspension_facts', 'corporate_actions'))
                self._attempt('official_market_rules', self._official_rules, checks,
                    scope=dict(kind='offline_catalog_only'))
        return checks
