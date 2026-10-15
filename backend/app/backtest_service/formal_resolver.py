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
import math
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
# Only the explicit small, tuple-inspection compatibility path retains a
# Cartesian limit. Default preparation budgets each SQL batch and cache.
LEGACY_FACT_CELLS = 20000
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
        # D01 ErrorDTO scope is map[str, str]. Keep the bounded fact report
        # inside a JSON string rather than widening that shared contract.
        return dict(super().diagnostic(), scope=dict(facts=encoded(self.checks).decode()))


class FormalRangeFault(RuntimeFault):
    def __init__(self, symbols, days, *, legacy=False):
        super().__init__('RESOURCE_LIMIT', '正式小事实日期或标的组合超过预算')
        self.scope = dict(resource='formal_sql_fact_range', requested_symbols=str(symbols),
            requested_natural_days=str(days), requested_cells=str(symbols * days),
            max_natural_days=str(FACT_DAYS))
        if legacy:
            self.scope['max_cells'] = str(LEGACY_FACT_CELLS)

    def diagnostic(self):
        return dict(super().diagnostic(), scope=self.scope.copy())


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


def _fingerprint_payload(item):
    """Retain original JSON evidence; do not reinterpret it as money or units.

    SQL Decimal/date/UUID fields use the existing canonical serializer. Raw
    evidence JSON can legitimately contain finite JSON numbers even though
    the rule/price domain resolvers reject float-valued computational facts.
    """
    from app.instruments.rules.contracts import canonical_payload
    if isinstance(item, float):
        if not math.isfinite(item):
            raise ValueError('non_finite_fact_evidence')
        return item
    if isinstance(item, dict):
        return {key: _fingerprint_payload(value) for key, value in item.items()}
    if isinstance(item, (tuple, list)):
        return [_fingerprint_payload(value) for value in item]
    return canonical_payload(item)


class FactReadBudget:
    """Budget every ORM SELECT, including reads performed by shared repositories.

    FrozenResult preserves the normal ORM scalar/row interface. The extra row
    detects exhaustion; it is never returned as a silently truncated fact set.
    """
    def __init__(self, connection=None):
        self.connection = connection
        self.ceiling_ms = FACT_SECONDS * 1000
        if connection is not None and connection.dialect.name == 'postgresql':
            configured = self._setting_ms()
            if configured:
                self.ceiling_ms = min(self.ceiling_ms, configured)
        self.written_ms = None
        self.reset()

    def reset(self):
        """Start one bounded SQL batch, never widen a caller's final timeout."""
        if hasattr(self, 'deadline'):
            self.check()
        self.rows, self.bytes = 0, 0
        self.deadline = clock.monotonic() + min(FACT_SECONDS, self.ceiling_ms / 1000)
        self.starting = True

    def _setting_ms(self):
        value = self.connection.scalar(text("SELECT current_setting('statement_timeout')"))
        for unit, factor in (('ms', 1), ('min', 60000), ('s', 1000), ('h', 3600000), ('d', 86400000)):
            if value.endswith(unit):
                return int(value[:-len(unit)]) * factor
        return int(value)

    def check(self):
        remaining = int((self.deadline - clock.monotonic()) * 1000)
        if remaining <= 0:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实读取超过时间预算')
        return remaining

    def timeout(self):
        remaining = self.check()
        if self.connection is not None and self.connection.dialect.name == 'postgresql':
            configured = self._setting_ms()
            # Only restore a timeout written by this budget at a new batch.
            # External callers may lower the setting even after session entry.
            if not (self.starting and self.written_ms is not None and configured == self.written_ms):
                remaining = min(remaining, configured or remaining)
            bounded = max(1, remaining // 10 * 10)
            if configured != bounded:
                self.connection.execute(text('SET LOCAL statement_timeout = ' + str(bounded)))
                self.written_ms = bounded
            self.starting = False

    def read(self, call):
        self.check()
        if not isinstance(call.statement, Select):
            raise RuntimeFault('INVALID_CONTRACT', '正式事实适配器只允许SQL读取')
        limit = FACT_ROWS - self.rows + 1
        if call.statement._limit_clause is not None:
            limit = min(limit, call.statement._limit_clause.value)
        try:
            self.timeout()
            result = call.invoke_statement(call.statement.limit(limit)).freeze()
        except DBAPIError as error:
            if getattr(error.orig, 'sqlstate', None) == '57014':
                raise RuntimeFault('RESOURCE_LIMIT', '正式小事实SQL读取超过时间预算') from None
            if getattr(error.orig, 'sqlstate', None) == '42P01':
                raise FormalResolutionFault([state('formal_sql_schema', 'fact_missing',
                    missing=('required_existing_fact_tables',))]) from None
            raise
        self.check()
        self.rows += len(result.data)
        if self.rows > FACT_ROWS:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实读取超过行预算')
        # Canonical conversion also covers UUIDs, dates, Decimal and PG ranges.
        payload = [_row_payload(item) for item in result.data]
        payload = _fingerprint_payload(payload)
        size = len(encoded(payload))
        self.bytes += size
        if self.bytes > FACT_BYTES:
            raise RuntimeFault('RESOURCE_LIMIT', '正式小事实读取超过字节预算')
        self.check()
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
    # Code-owned projections of the existing S3 contracts. No REST/config or
    # arbitrary JSON metadata may supply these missing producer outputs.
    market: object = None
    execution_inputs: object = None
    action_projection: object = None
    research: tuple = ()
    reference_indices: tuple = ()
    fact_fingerprint: str | None = None


class SqlFormalPlanResolver:
    def __init__(self, settings, *, engine=None):
        self.settings, self.engine = settings, engine

    @contextmanager
    def _session(self, connection=None):
        from app.db.session import get_engine
        if connection is not None:
            budget = FactReadBudget(connection)
            budget.timeout()
            # A final result transaction holds these existing small-fact
            # tables stable until it commits. No new snapshot/table is made.
            if connection.dialect.name == 'postgresql':
                connection.execute(text('LOCK TABLE instrument_code_mappings, instrument_identity_facts, '
                    'instrument_rule_facts, calendar_registry, calendar_definitions, calendar_session_facts, '
                    'calendar_source_priorities, trading_status_facts, trading_status_coverage_facts, '
                    'corporate_action_facts, corporate_action_coverage_facts IN SHARE MODE'))
            with Session(bind=connection) as session:
                event.listen(session, 'do_orm_execute', budget.read, retval=True)
                yield session, budget
                budget.check()
            return
        engine = self.engine or get_engine()
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                if connection.dialect.name == 'postgresql':
                    connection.execute(text('SET TRANSACTION READ ONLY'))
                    connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                with Session(bind=connection) as session:
                    budget = FactReadBudget(connection)
                    event.listen(session, 'do_orm_execute', budget.read, retval=True)
                    yield session, budget
                    budget.check()

    def __call__(self, work):
        facts = self.resolve_facts(work, paged=True)
        from .formal_assembly import assemble_run_plan, missing_plan_dependencies
        if missing_plan_dependencies(facts):
            raise FormalResolutionFault(list(facts.checks))
        return assemble_run_plan(work, facts,
            check_facts=lambda current, *, connection=None: self.check_facts(
                current, facts, connection=connection))

    def check_facts(self, work, accepted, *, connection=None):
        from .formal_fact_guard import fact_fingerprint
        with self._session(connection) as (session, _budget):
            current = fact_fingerprint(session, work, zone=ZONE)
        token = canonical_hash(dict(config=work.config, facts=current, official_rules=self._official_rules()))
        if token != accepted.token:
            raise RuntimeFault('DATA_CHANGED', '已解析的正式小事实、版本或质量发生变化')

    def resolve_facts(self, work, *, connection=None, paged=True):
        """Default to bounded SQL pages; retain small tuple inspection explicitly.

        Tuple inspection keeps its old small-scope bound. This compatibility
        choice is absent from RunConfig/REST and supplies no source semantics.
        """
        self._check_range(work.config, paged=paged)
        with self._session(connection) as (session, budget):
            facts = self._resolve(session, budget, work, paged=paged)
        if connection is None:
            # A correction during the coherent admission snapshot must not
            # escape as an accepted dependency set, even before execution.
            self.check_facts(work, facts)
        return facts

    @staticmethod
    def _check_range(config, *, paged=True):
        start, end = (date.fromisoformat(config[name]) for name in ('start', 'end'))
        days, symbols = (end - start).days + 1, len(config['universe'])
        if not 1 <= days <= FACT_DAYS or not paged and symbols * days > LEGACY_FACT_CELLS:
            raise FormalRangeFault(symbols, days, legacy=not paged)

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

    def _read_dependency_page(self, session, work, symbols, day, *, inventory_asof=None, summarized=False):
        from .formal_sql_pages import ScopedChecks, SqlDependencyPage
        from .session_fact_pages import PAGE_ROWS
        if not 1 <= len(symbols) <= PAGE_ROWS:
            raise RuntimeFault('RESOURCE_LIMIT', 'SQL依赖读取超过单页标的信用')
        checks = ScopedChecks() if summarized else []
        identities, calendars, rules, trading_status, actions, commissions = [], [], [], [], [], []
        candidates = session.info.get('s3_candidate_page')
        if candidates is not None:
            candidates.load_day(day, inventory_asof or datetime.combine(day, time(), ZONE))
        from app.instruments.spec_provider import DEFAULT_MAPPING_SOURCE, DEFAULT_RULE_PACKAGE_REFERENCE
        from app.instruments.models import InstrumentCodeMappingRecord
        from app.instruments.repository import InstrumentCodeMappingRepository
        from app.instruments.identity_repository import InstrumentIdentityFactRepository
        from app.instruments.rule_facts_repository import RuleFactsRepository
        from app.instruments.rules.registry import RulePackageRegistry
        from app.instruments.rules.resolver import RulePackageResolver
        from app.instruments.rules.etf_china import register_china_listed_etf_rules
        registry = session.info.get('s3_rule_registry')
        if registry is None:
            registry = RulePackageRegistry()
            register_china_listed_etf_rules(registry)
            session.info['s3_rule_registry'] = registry
        for symbol in symbols:
            at = datetime.combine(day, time(), ZONE)
            cutoff = inventory_asof or at  # facts known before this simulated day
            scope = dict(security=symbol, date=day.isoformat())
            def identity():
                if candidates is not None:
                    return candidates.identity(symbol, day, cutoff, ZONE)
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
                                             'suspension_facts', 'corporate_actions',
                                             *(('account_commission',) if summarized else ())))
                continue
            fact, mappings = pair
            identities.append((symbol, day, fact, mappings))
            calendar = self._attempt('calendar_sessions',
                lambda: self._calendar(session, fact.calendar_id, day, cutoff), checks, scope=scope)
            if calendar is not None:
                calendars.append((symbol, day, calendar))
            def rule():
                if candidates is not None:
                    return candidates.rule(fact, day, cutoff, registry)
                rule_facts = RuleFactsRepository(session).list_facts(fact.instrument_id,
                    DEFAULT_RULE_PACKAGE_REFERENCE, start_date=day, end_date=day, data_cutoff=cutoff)
                if not rule_facts:
                    return None
                resolved = RulePackageResolver(registry).resolve(DEFAULT_RULE_PACKAGE_REFERENCE,
                    instrument_id=fact.instrument_id, asset_class=fact.asset_class,
                    effective_date=day, data_cutoff=cutoff, facts=rule_facts)
                from app.instruments.rules.contracts import ResolutionStatus
                if resolved.status != ResolutionStatus.READY:
                    raise ValueError(','.join(issue.code.value for issue in resolved.issues))
                return resolved
            resolved = self._attempt('instrument_rule_package', rule, checks, scope=scope)
            if resolved is not None:
                rules.append((symbol, day, resolved))
            status = self._attempt('suspension_facts', lambda: self._status(session,
                fact.instrument_id, symbol, day, cutoff, candidates=candidates), checks, scope=scope)
            if status is not None:
                trading_status.append((symbol, day, status))
            action = self._attempt('corporate_actions', lambda: self._actions(session,
                fact.instrument_id, day, cutoff, candidates=candidates), checks, scope=scope)
            if action is not None:
                actions.append((symbol, day, action))
            if getattr(work, 'specification', None):
                def commission():
                    from .account_config import commission_from_account_fee_schedule
                    key = (fact.currency, fact.asset_class, fact.exchange)
                    if candidates is not None and key in candidates.commissions:
                        candidates.commissions.move_to_end(key)
                        return candidates.commissions[key]
                    schedule = work.specification['account_snapshot']['fee_schedule']
                    value = tuple(commission_from_account_fee_schedule(schedule, side=side,
                        currency=fact.currency, applicability_context=dict(asset_class=fact.asset_class,
                            exchange=fact.exchange)) for side in ('buy', 'sell'))
                    if candidates is not None:
                        candidates._remember(candidates.commissions, key, value)
                    return value
                value = self._attempt('account_commission', commission, checks, scope=scope)
                if value is not None:
                    commissions.append((symbol, day, value))
            elif summarized:
                checks.append(state('account_commission', 'fact_missing',
                    missing=('captured_account_snapshot',), scope=scope))
            if len(encoded(checks)) > 256 * 1024:
                raise RuntimeFault('RESOURCE_LIMIT', '正式事实诊断超过控制消息预算')
        return SqlDependencyPage(tuple(identities), tuple(calendars), tuple(rules),
            tuple(trading_status), tuple(actions), tuple(commissions), tuple(checks))

    def _resolve(self, session, budget, work, *, inventory_asof=None, paged=False):
        from .formal_fact_guard import fact_fingerprint
        from .formal_sql_pages import ScopedChecks, SessionFailurePositions
        from .session_fact_pages import PAGE_ROWS, BRIDGES
        from .formal_assembly import missing_plan_dependencies, zero_action_projection
        from .research_gateway import current_research_bindings
        config = work.config
        start, end = date.fromisoformat(config['start']), date.fromisoformat(config['end'])
        days, symbols = (end - start).days + 1, tuple(config['universe'])
        self._check_range(config, paged=paged)
        fingerprint = fact_fingerprint(session, work, zone=ZONE, inventory_asof=inventory_asof)
        checks = ScopedChecks(symbols) if paged else []
        identities, calendars, rules, trading_status, actions, commissions = [], [], [], [], [], []
        calendar_seeds = {}
        failures = SessionFailurePositions(symbols, start, end,
            (*BRIDGES, 'execution_corporate_actions')) if paged else None
        def batches():
            if not paged:
                for offset in range(days):
                    for first in range(0, len(symbols), PAGE_ROWS):
                        yield symbols[first:first + PAGE_ROWS], start + timedelta(days=offset)
                return
            from .formal_sql_candidates import SqlCandidatePage
            cutoff = inventory_asof or datetime.combine(end, time(), ZONE)
            try:
                for first in range(0, len(symbols), PAGE_ROWS):
                    selected = symbols[first:first + PAGE_ROWS]
                    # One bounded candidate cache, then one bounded daily
                    # batch. No cache or output grows with symbols * days.
                    session.info.pop('s3_candidate_page', None)
                    budget.reset()
                    session.info['s3_candidate_page'] = SqlCandidatePage(session, selected, start, end, cutoff)
                    budget.check()
                    for offset in range(days):
                        budget.reset()
                        yield selected, start + timedelta(days=offset)
                        budget.check()
            finally:
                session.info.pop('s3_candidate_page', None)
        iterator = batches()
        try:
            for selected, day in iterator:
                page = self._read_dependency_page(session, work, selected, day,
                    inventory_asof=inventory_asof, summarized=paged)
                if paged:
                    checks.merge(page.checks)
                    for check in page.checks:
                        failures.record(check)
                    for row in page.calendars:
                        calendar_id = row[2]['fact'].calendar_id
                        # Existing assembly rejects multiple references; two
                        # seeds suffice to diagnose it without a growing map.
                        if calendar_id in calendar_seeds or len(calendar_seeds) < 2:
                            calendar_seeds.setdefault(calendar_id, row)
                    _, failure = zero_action_projection(page.corporate_actions,
                        dict(calendar=[dict(date=day.isoformat())]), selected)
                    failures.record(failure)
                    if failures.control_bytes(checks) > 256 * 1024:
                        raise RuntimeFault('RESOURCE_LIMIT', '正式事实诊断超过控制消息预算')
                else:
                    checks.extend(page.checks)
                    for target, rows in ((identities, page.identities), (calendars, page.calendars),
                            (rules, page.rule_packages), (trading_status, page.trading_status),
                            (actions, page.corporate_actions), (commissions, page.commissions)):
                        target.extend(rows)
                    if len(encoded(checks)) > 256 * 1024:
                        raise RuntimeFault('RESOURCE_LIMIT', '正式事实诊断超过控制消息预算')
                del page
        finally:
            iterator.close()
        if paged:
            budget.reset()
        checks = list(checks)
        execution_calendar = self._attempt('execution_calendar_assembly', lambda:
            self._assemble_calendar(session, tuple(calendar_seeds.values()) if paged else calendars,
                start, end, inventory_asof), checks, scope=dict(start=start.isoformat(), end=end.isoformat()))
        diagnostic_key, diagnostic_values = None, None
        def reread_failure(symbol, day):
            nonlocal diagnostic_key, diagnostic_values
            if diagnostic_key != (symbol, day):
                budget.reset()
                page = self._read_dependency_page(session, work, (symbol,), date.fromisoformat(day),
                    inventory_asof=inventory_asof, summarized=True)
                _, action = zero_action_projection(page.corporate_actions,
                    dict(calendar=[dict(date=day)]), (symbol,))
                diagnostic_values = {row['binding']: row for row in (*page.checks, action)}
                diagnostic_key = symbol, day
                del page
                if failures.control_bytes(checks) + len(encoded(diagnostic_values)) > 256 * 1024:
                    raise RuntimeFault('RESOURCE_LIMIT', '正式事实诊断超过控制消息预算')
            return diagnostic_values
        if paged and execution_calendar is not None and execution_calendar['calendar']:
            # The legacy assembler requires bridge facts on open sessions.
            # Retaining only the first natural-day failure can otherwise hide
            # a later open-session failure behind an irrelevant weekend gap.
            open_days = [row['date'] for row in execution_calendar['calendar']]
            for index, check in enumerate(checks):
                if check['binding'] not in BRIDGES:
                    continue
                failure = failures.select(check['binding'], open_days, reread_failure)
                if failure is not None:
                    failure['scope']['checked_cells'] = check['scope']['checked_cells']
                    checks[index] = failure
                elif not check['available']:
                    checks[index] = state(check['binding'], 'available', scope=dict(
                        kind='checked_open_session_dependencies', checked_cells=check['scope']['checked_cells']))
        official = self._attempt('official_market_rules', self._official_rules, checks,
            scope=dict(kind='offline_catalog_only'))
        research = current_research_bindings()
        checks.extend(self._execution_gaps(config['frequency'], research=research))
        if paged and execution_calendar is not None:
            action_check = failures.select('execution_corporate_actions',
                [row['date'] for row in execution_calendar['calendar']], reread_failure)
            action_check = action_check or state('execution_corporate_actions', 'available',
                scope=dict(kind='complete_zero_action_projection'))
            action_projection = () if action_check['available'] else None
        else:
            action_projection, action_check = zero_action_projection(actions, execution_calendar, symbols)
        checks = [action_check if check['binding'] == 'execution_corporate_actions' else check for check in checks]
        token = canonical_hash(dict(config=config, facts=fingerprint, official_rules=official))
        inputs = self._sql_session_inputs(work, execution_calendar, token) if paged else None
        facts = ResolvedFormalFacts(tuple(checks), tuple(identities), tuple(calendars),
            tuple(rules), tuple(trading_status), tuple(actions), tuple(commissions),
            tuple(official or ()), execution_calendar, token, execution_inputs=inputs,
            action_projection=action_projection, fact_fingerprint=fingerprint, research=research)
        checks[-1]['missing'] = list(missing_plan_dependencies(facts))
        return facts

    def _sql_session_inputs(self, work, calendar, token):
        from .formal_sql_pages import SqlSessionInputs
        from .formal_fact_guard import fact_fingerprint
        from .session_fact_pages import PAGE_ROWS
        captured = deepcopy(work)
        symbols = tuple(captured.config['universe'])
        sessions = {row['session']['key']: date.fromisoformat(row['date'])
            for row in calendar['calendar']} if calendar is not None else {}
        accepted = SimpleNamespace(token=token)
        def read(session_key, offset, limit):
            if type(session_key) is not str or session_key not in sessions:
                raise RuntimeFault('DATA_RESTRICTED', 'SQL依赖页超出已核开放会话')
            if (type(offset) is not int or not 0 <= offset < len(symbols)
                    or type(limit) is not int or not 1 <= limit <= PAGE_ROWS):
                raise RuntimeFault('INVALID_CONTRACT', 'SQL依赖页偏移或信用无效')
            with self._session() as (session, _budget):
                current = fact_fingerprint(session, captured, zone=ZONE)
                actual = canonical_hash(dict(config=captured.config, facts=current, official_rules=self._official_rules()))
                if actual != token:
                    raise RuntimeFault('DATA_CHANGED', '已解析的正式小事实、版本或质量发生变化')
                from .formal_sql_candidates import SqlCandidatePage
                day = sessions[session_key]
                selected = symbols[offset:offset + limit]
                _budget.reset()
                session.info['s3_candidate_page'] = SqlCandidatePage(session, selected, day, day,
                    datetime.combine(day, time(), ZONE))
                _budget.check()
                _budget.reset()
                try:
                    page = self._read_dependency_page(session, captured, selected,
                        day, summarized=True).session_inputs()
                finally:
                    session.info.pop('s3_candidate_page', None)
            # A new transaction catches a correction committed while the page
            # was read under its repeatable-read snapshot. Final publication
            # still calls the original same-result-transaction check/locks.
            self.check_facts(captured, accepted)
            return page
        return SqlSessionInputs(read)

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
        from .formal_calendar_reader import CalendarReader
        reader = session.info.get('s3_calendar_reader')
        if reader is None:
            reader = CalendarReader(session)
            session.info['s3_calendar_reader'] = reader
        return reader.resolve(calendar_id, day, cutoff)

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
        following.sort()
        # The worker needs the FIRST session after the run as a CalendarSession,
        # not the key of the second one. Its week/month ordinals also require
        # explicit complete natural-day coverage; never count a partial month.
        next_day = following[0]
        next_month = (next_day.replace(day=28) + timedelta(days=4)).replace(day=1)
        next_lower = min(next_day.replace(day=1), next_day - timedelta(days=next_day.weekday()))
        next_upper = max(next_month - timedelta(days=1), next_day + timedelta(days=6-next_day.weekday()))
        day = next_lower
        while day <= next_upper:
            if day not in selected:
                selected[day] = cls._calendar(session, calendar_id, day, cutoff)
            day += timedelta(days=1)
        open_days = sorted(day for day, value in selected.items() if value['fact'].is_open)
        def key(day):
            return calendar_id + ':' + day.isoformat()
        def ns(instant):
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            delta = instant.astimezone(UTC) - epoch
            return delta.days * 86400 * 1000000000 + delta.seconds * 1000000000 + delta.microseconds * 1000
        rows = []
        for day in open_days:
            if not start <= day <= end and day != next_day:
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
        if not any(start <= date.fromisoformat(row['date']) <= end for row in rows):
            raise ValueError('open_run_sessions_missing')
        from quantfoundry import _native
        rows = json.loads(_native.validate_formal_calendar_json(encoded(rows).decode()))
        return dict(calendar=rows[:-1], next_session=key(next_day), following_session=rows[-1],
            subsequent_session=key(following[1]),
            reference=calendar_id + ':' + canonical_hash([value['fact'].content_hash for _, value in sorted(selected.items())]))

    @staticmethod
    def _status(session, iid, symbol, day, cutoff, *, candidates=None):
        from app.data_ingestion.models.trading_calendar import TradingStatusFact, TradingStatusCoverageFact
        if candidates is not None:
            facts, coverage = candidates.status_inputs(iid, symbol)
        else:
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
    def _actions(session, iid, day, cutoff, *, candidates=None):
        from app.data_ingestion.repositories.corporate_action import CorporateActionRepository
        from app.data_ingestion.models.corporate_action import CorporateActionCoverageFact
        repository = CorporateActionRepository(session)
        if candidates is None:
            coverage = repository.coverage((iid,), day, day, cutoff=cutoff, knowledge_as_of=cutoff)
            if not coverage:
                return None
            candidates = tuple(session.scalars(select(CorporateActionCoverageFact).where(
                CorporateActionCoverageFact.instrument_id == iid,
                CorporateActionCoverageFact.start_date <= day, CorporateActionCoverageFact.end_date >= day,
                CorporateActionCoverageFact.known_at <= cutoff, CorporateActionCoverageFact.observed_at <= cutoff)))
            get_events = lambda: repository.list_facts((iid,), day, day, cutoff=cutoff, knowledge_as_of=cutoff)
        else:
            candidate_page = candidates
            get_events = lambda: candidate_page.action_events(iid)
            coverage, candidates = candidate_page.action_coverage_inputs(iid)
        if not coverage:
            return None
        # The shared repository picks a latest assertion per action family.
        # A tied latest rank is ambiguous even when one row happens to be zero;
        # do not let physical SQL order manufacture complete-zero acceptance.
        for row in coverage:
            tied = [candidate for candidate in candidates if candidate.action_type == row.action_type
                    and (candidate.known_at, candidate.observed_at) == (row.known_at, row.observed_at)]
            if len(tied) != 1:
                raise ValueError('corporate_action_coverage_ambiguous')
            if (row.status != 'complete' or row.event_count is None or not row.source_revision
                    or not row.known_at or not row.observed_at or not row.evidence or not row.validation_rule):
                raise ValueError('corporate_action_coverage_incomplete')
        events = get_events()
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
    def _execution_gaps(frequency, *, research=None):
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
        result.append(state('execution_corporate_actions', 'fact_missing', missing=(
            'all_required_action_family_coverage',)))
        result.append(state('research_publication', 'field_semantics_missing', missing=(
            'historical_publication_time', 'historical_effective_sets', 'adjustment_public_time_and_anchor')))
        from .research_gateway import current_research_bindings
        for binding in current_research_bindings() if research is None else research:
            result.append(state(binding.name, 'field_semantics_missing', missing=(binding.unavailable_reason,),
                scope=dict(research_kind=binding.kind, requirement='optional_per_capability')))
        result.append(state('execution_plan_assembly', 'fact_missing', missing=(
            'resolved_market_projection', 'resolved_execution_inputs',
            'resolved_action_projection'), scope=dict(adapter='implemented',
                acceptance='requires_all_execution_dependencies', research='optional_per_capability')))
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
