"""Final S3 wiring for code-owned projections of the existing Rust contracts.

This module creates no source semantics or facts. In particular, None action
projection is unknown, whereas an explicitly resolved empty tuple is a proven
complete zero. The default SQL resolver cannot produce the missing projections
until the narrow upstream producer/source scope has been established.
"""
from copy import deepcopy
import json

from .formal_runtime import AcceptedRunPlan
from .runtime_contracts import RuntimeFault, WorkerLimits
from .serialization import encoded
from .session_fact_pages import (ExecutionSessionInput, PagedSessionInputs,
    LegacySessionInputs, ACTION_FAMILIES, BRIDGES, PAGE_ROWS, PAGE_OUTPUT_BYTES, compose_page)


class FormalAssemblyFault(RuntimeFault):
    def __init__(self, error):
        super().__init__(error.code, error.message)
        self.operation, self.scope = error.operation, dict(error.scope)

    def diagnostic(self):
        return dict(super().diagnostic(), operation=self.operation, scope=self.scope)


def native_json(read):
    from quantfoundry import _native
    try:
        return json.loads(read())
    except _native.ContractError as error:
        raise FormalAssemblyFault(error) from None


def zero_action_projection(actions, calendar, symbols):
    """Resolve only existing explicit zero proofs; nonzero events need more semantics."""
    from .formal_resolver import state
    if calendar is None:
        return None, state('execution_corporate_actions', 'fact_missing',
            missing=('resolved_execution_calendar',))
    resolved = {(s, day.isoformat()): value for s, day, value in actions}
    for row in calendar['calendar']:
        for security in symbols:
            scope = dict(security=security, date=row['date'])
            action = resolved.get((security, row['date']))
            if action is None:
                return None, state('execution_corporate_actions', 'fact_missing',
                    missing=('complete_corporate_action_dependency',), scope=scope)
            if action['events']:
                return None, state('execution_corporate_actions', 'field_semantics_missing', missing=(
                    'dated_action_boundaries_and_sequence',
                    'action_public_time_raw_mark_and_investor_tax_scope'), scope=scope)
            covered = {r['action_type'] for r in action['coverage']
                       if r['status'] == 'complete' and r['event_count'] == 0}
            if not ACTION_FAMILIES <= covered:
                return None, state('execution_corporate_actions', 'fact_missing',
                    missing=tuple(sorted(ACTION_FAMILIES-covered)), scope=scope)
    return (), state('execution_corporate_actions', 'available', scope=dict(kind='complete_zero_action_projection'))


def missing_plan_dependencies(facts):
    return tuple(name for name, value in (
        ('resolved_market_projection', facts.market),
        ('resolved_execution_calendar', facts.execution_calendar),
        ('resolved_execution_inputs', None if getattr(facts.execution_inputs, 'unavailable_reason', None)
            else facts.execution_inputs),
        ('resolved_action_projection', facts.action_projection),
        ('resolved_official_rules', facts.official_rules),
        # A paged reader supplies the original captured commissions in every
        # page; full admission traversal still rejects any missing slice.
        ('resolved_account_commissions', facts.execution_inputs
            if isinstance(facts.execution_inputs, PagedSessionInputs) else facts.commissions),
    ) if value is None or name != 'resolved_action_projection' and not value)


def assemble_run_plan(work, facts, *, check_facts):
    """Compose narrow existing DTOs; neither a full plan nor acceptance flags.

    Research mappings are independently optional. Each enabled mapping must
    satisfy the original D05 public-time/identity/unit/anchor/complete-set gate.
    Nothing reads a source table, imports a strategy or enqueues work here.
    """
    from quantfoundry import _native
    if missing_plan_dependencies(facts):
        raise RuntimeFault('CAPABILITY_UNAVAILABLE', '最终装配缺少明确的执行生产者投影')
    if (not callable(check_facts) or not isinstance(facts.execution_inputs, (tuple, PagedSessionInputs))
            or type(facts.action_projection) is not tuple):
        raise RuntimeFault('INVALID_CONTRACT', '最终装配的受限事实输入或一致性检查无效')
    projection = facts.execution_calendar
    rules = native_json(_native.official_market_rules_json)
    if rules != list(facts.official_rules):
        raise RuntimeFault('DATA_CHANGED', '装配规则目录与已解析的原Rust目录发生变化')
    calendar = deepcopy(projection['calendar'])
    following = deepcopy(projection['following_session'])
    if (not calendar or type(following) is not dict
            or following['session']['key'] != projection['next_session']):
        raise RuntimeFault('RULE_UNAVAILABLE', '后续账户会话与已核日历不一致')
    native_json(lambda: _native.validate_formal_calendar_json(encoded([*calendar, following]).decode()))
    if work.config.get('reference_calendar', projection['reference']) != projection['reference']:
        raise RuntimeFault('RULE_UNAVAILABLE', '请求参考日历与已核日历不一致')
    sessions = {row['session']['key']: row['date'] for row in calendar}
    securities = tuple(work.config['universe'])
    if (not 1 <= len(securities) <= 10000 or len(set(securities)) != len(securities)
            or any(type(security) is not str or not security for security in securities)):
        raise RuntimeFault('INVALID_CONTRACT', '会话事实的授权标的集合无效')
    if facts.action_projection:
        raise RuntimeFault('CAPABILITY_UNAVAILABLE', '非零公司行动的公开、阶段和税务投影尚未完整')
    if isinstance(facts.execution_inputs, PagedSessionInputs):
        if any((facts.identities, facts.commissions, facts.trading_status, facts.corporate_actions, facts.rule_packages)):
            raise RuntimeFault('INVALID_CONTRACT', '分页装配的依赖须由单页读取，不能同时保留全期事实')
        source = facts.execution_inputs
        allowed, dates = frozenset(securities), frozenset(sessions.values())
        for check in facts.checks:
            if (check['binding'] in BRIDGES and check['scope'].get('security') in allowed
                    and check['scope'].get('date') in dates and not check['available']):
                raise RuntimeFault('RULE_UNAVAILABLE', '最终装配的既有正式事实依赖未就绪')
    else:
        source = LegacySessionInputs(facts, securities, sessions)
    def compose(payload):
        return native_json(lambda: _native.compose_official_session_fact_json(encoded(payload).decode()))
    def read_page(session, offset, limit):
        if type(session) is not str or session not in sessions:
            raise RuntimeFault('DATA_RESTRICTED', '会话事实请求超出已核run日历')
        if (type(offset) is not int or not 0 <= offset < len(securities)
                or type(limit) is not int or not 1 <= limit <= PAGE_ROWS):
            raise RuntimeFault('INVALID_CONTRACT', '会话事实页偏移或信用无效')
        selected = securities[offset:offset + limit]
        raw = source.read(session, offset, len(selected))
        rows = compose_page(raw, selected, sessions[session], session, work, compose)
        following_offset = offset + len(rows)
        page = dict(session=session, facts=rows,
            next_offset=following_offset if following_offset < len(securities) else None)
        if len(encoded(page)) > PAGE_OUTPUT_BYTES:
            raise RuntimeFault('RESOURCE_LIMIT', '会话事实页超过控制消息预算')
        return page
    from .data_gateway import GatewayError
    research = []
    for binding in facts.research:
        if binding.unavailable_reason:
            continue
        try:
            binding.validate_capability()
        except GatewayError as error:
            if error.code != 'CAPABILITY_UNAVAILABLE':
                raise
            continue
        research.append(binding)
    limits = getattr(work, 'limits', None) or WorkerLimits()
    if not isinstance(limits, WorkerLimits):
        raise RuntimeFault('RESOURCE_LIMIT', '受理worker预算无效')
    spec = dict(calendar=calendar, calendar_reference=projection['reference'],
        next_session=projection['next_session'], following_session=following,
        rules=rules, facts=[], stream_session_facts=True,
        corporate_actions=deepcopy(list(facts.action_projection)), initial_marks={}, source=dict(kind='ipc'),
        result_budget_bytes=limits.result_bytes, data_budget_bytes=min(64 * 1024**2, limits.memory_bytes // 2),
        max_active_orders=1000, max_commands_per_boundary=1000, chunk_events=32,
        wall_time_seconds=limits.wall_seconds)
    if len(encoded(spec)) > 1024 * 1024 - 256 * 1024:
        raise RuntimeFault('RESOURCE_LIMIT', '最终装配超过有界工作进程控制消息预算')
    # Reject control-budget failures before reading facts, then reject gaps
    # and invalid Rust rules before admission, visiting one page at a time.
    # No composed full-run rows or Cartesian coverage set is retained.
    check_facts(work, connection=None)
    for session in sessions:
        for offset in range(0, len(securities), PAGE_ROWS):
            read_page(session, offset, PAGE_ROWS)
    check_facts(work, connection=None)
    def session_facts(session, offset, limit):
        check_facts(work, connection=None)
        page = read_page(session, offset, limit)
        check_facts(work, connection=None)
        return page
    return AcceptedRunPlan(facts.market, spec, session_facts=session_facts, check_facts=check_facts,
        research=tuple(research), reference_indices=facts.reference_indices)
