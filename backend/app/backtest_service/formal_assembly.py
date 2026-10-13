"""Final S3 wiring for code-owned projections of the existing Rust contracts.

This module creates no source semantics or facts. In particular, None action
projection is unknown, whereas an explicitly resolved empty tuple is a proven
complete zero. The default SQL resolver cannot produce the missing projections
until the narrow upstream producer/source scope has been established.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
from uuid import UUID

from .formal_runtime import AcceptedRunPlan
from .runtime_contracts import RuntimeFault, WorkerLimits
from .serialization import encoded


@dataclass(frozen=True)
class ExecutionSessionInput:
    # Stable identity is separately joined to the real PIT repository result,
    # rather than treating a public/source code as a stable economic identity.
    instrument_id: UUID
    payload: dict


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


ACTION_FAMILIES = frozenset({'cash_dividend', 'share_distribution', 'split', 'rights_issue'})


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
        ('resolved_execution_inputs', facts.execution_inputs),
        ('resolved_action_projection', facts.action_projection),
        ('resolved_official_rules', facts.official_rules),
        ('resolved_account_commissions', facts.commissions),
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
    if (not callable(check_facts) or type(facts.execution_inputs) is not tuple
            or not 1 <= len(facts.execution_inputs) <= 20000
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
    sessions = {row['date']: row['session']['key'] for row in calendar}
    required = {(s, day) for day in sessions for s in work.config['universe']}
    identities = {(s, day.isoformat()): value for s, day, value, _ in facts.identities}
    commissions = {(s, day.isoformat()): value for s, day, value in facts.commissions}
    status = {(s, day.isoformat()): value for s, day, value in facts.trading_status}
    # Nonzero source events need source public time, boundary and tax mappings
    # that are still absent. A cash-dividend-only zero is not all-action proof.
    actions = {(s, day.isoformat()): value for s, day, value in facts.corporate_actions}
    if facts.action_projection:
        raise RuntimeFault('CAPABILITY_UNAVAILABLE', '非零公司行动的公开、阶段和税务投影尚未完整')
    for key in required:
        action = actions.get(key)
        if (action is None or action['events'] or not ACTION_FAMILIES <= {
                row['action_type'] for row in action['coverage']
                if row['status'] == 'complete' and row['event_count'] == 0}):
            raise RuntimeFault('CAPABILITY_UNAVAILABLE', '完整零公司行动须逐适用类型覆盖，不能从分红零事件推导')
    bridges = {'identity', 'calendar_sessions', 'instrument_rule_package', 'suspension_facts',
               'corporate_actions', 'account_commission'}
    for check in facts.checks:
        scope = check['scope']
        if (check['binding'] in bridges and (scope.get('security'), scope.get('date')) in required
                and not check['available']):
            raise RuntimeFault('RULE_UNAVAILABLE', '最终装配的既有正式事实依赖未就绪')
    rows = {}
    for item in facts.execution_inputs:
        if not isinstance(item, ExecutionSessionInput) or type(item.payload) is not dict:
            raise RuntimeFault('INVALID_CONTRACT', '执行输入不是受限会话事实')
        payload = deepcopy(item.payload)
        try:
            security, day = payload['instrument']['security'], payload['day']['date']
            key = (security, day)
            identity = identities[key]
            commission = commissions[key]
        except (KeyError, TypeError):
            raise RuntimeFault('RULE_UNAVAILABLE', '执行事实缺少已核身份或捕获佣金') from None
        exchanges = {'SSE': 'shanghai', 'SZSE': 'shenzhen', 'BSE': 'beijing'}
        product = payload['instrument'].get('product', '')
        if (key not in required or key in rows or identity.instrument_id != item.instrument_id
                or identity.currency != 'CNY' or exchanges.get(identity.exchange) != payload['instrument']['exchange']
                or identity.asset_class == 'etf' and not product.endswith('_etf')
                or payload['day']['security'] != security or payload['day']['session'] != sessions[day]):
            raise RuntimeFault('RULE_UNAVAILABLE', '执行事实身份、产品、日期或会话与授权不一致')
        if key not in status or (any(row['status'] == 'suspended' for row in status[key]['facts'])
                and payload['day']['status'] != 'halted'):
            raise RuntimeFault('RULE_UNAVAILABLE', '会话执行状态与已解析停牌事实冲突或缺失')
        # Caller cannot inject computed fees, a rule origin or a model. Rust
        # composes both sides from the original captured account commission.
        if {'buy_fees', 'sell_fees', 'origin', 'buy_commission', 'sell_commission', 'cost_overrides'} & payload.keys():
            raise RuntimeFault('INVALID_CONTRACT', '执行生产者不能覆盖规则目录或受理佣金')
        payload.update(buy_commission=commission[0], sell_commission=commission[1],
                       cost_overrides=work.config.get('cost_overrides', {}))
        rows[key] = native_json(lambda: _native.compose_official_session_fact_json(encoded(payload).decode()))
    if set(rows) != required:
        raise RuntimeFault('RULE_UNAVAILABLE', '执行事实没有覆盖全部已核开放会话和授权标的')
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
    by_session = {session: tuple(rows[s, day] for s in work.config['universe'])
                  for day, session in sessions.items()}
    def session_facts(session, offset, limit):
        if session not in by_session:
            raise RuntimeFault('DATA_RESTRICTED', '会话事实请求超出已核run日历')
        selected = by_session[session]
        if (type(offset) is not int or not 0 <= offset < len(selected)
                or type(limit) is not int or not 1 <= limit <= 256):
            raise RuntimeFault('INVALID_CONTRACT', '会话事实页偏移或信用无效')
        page = dict(session=session, facts=deepcopy(list(selected[offset:offset+limit])),
                    next_offset=offset+limit if offset+limit < len(selected) else None)
        if len(encoded(page)) > 1024 * 1024 - 16384:
            raise RuntimeFault('RESOURCE_LIMIT', '会话事实页超过控制消息预算')
        return page
    return AcceptedRunPlan(facts.market, spec, session_facts=session_facts, check_facts=check_facts,
        research=tuple(research), reference_indices=facts.reference_indices)
