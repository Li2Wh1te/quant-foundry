"""Bounded pages of the existing execution DTO and resolved dependencies.

This is an in-process read port, not a new stored fact schema or producer.
The default resolver still has to obtain every original input independently.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from .runtime_contracts import RuntimeFault
from .serialization import encoded

PAGE_ROWS = 256
PAGE_INPUT_BYTES = 4 * 1024 * 1024
PAGE_OUTPUT_BYTES = 1024 * 1024 - 16384
LEGACY_INPUT_ROWS = 20000
ACTION_FAMILIES = frozenset({'cash_dividend', 'share_distribution', 'split', 'rights_issue'})
BRIDGES = frozenset({'identity', 'calendar_sessions', 'instrument_rule_package',
    'suspension_facts', 'corporate_actions', 'account_commission'})


@dataclass(frozen=True)
class ExecutionSessionInput:
    instrument_id: UUID
    payload: dict


@dataclass(frozen=True)
class SessionInputPage:
    """Existing resolver outputs restricted to one session/security slice."""
    execution_inputs: tuple
    identities: tuple
    commissions: tuple
    trading_status: tuple
    corporate_actions: tuple
    checks: tuple = ()

    def __post_init__(self):
        for name in ('execution_inputs', 'identities', 'commissions', 'trading_status', 'corporate_actions'):
            value = getattr(self, name)
            if type(value) is not tuple or len(value) > PAGE_ROWS:
                raise RuntimeFault('RESOURCE_LIMIT', '会话事实输入页超过行信用或不是有限元组')
        if type(self.checks) is not tuple or len(self.checks) > PAGE_ROWS * len(BRIDGES):
            raise RuntimeFault('RESOURCE_LIMIT', '会话事实诊断页超过行信用')


@dataclass(frozen=True)
class PagedSessionInputs:
    """A trusted reader returns SessionInputPage for (session, offset, limit).

    It may query existing repositories. Neither REST nor account metadata may
    select this reader, and no successful plan or computed fees are accepted.
    """
    read: object

    def __post_init__(self):
        if not callable(self.read):
            raise RuntimeFault('INVALID_CONTRACT', '会话事实分页读取器无效')


@dataclass(frozen=True)
class _Identity:
    instrument_id: UUID
    currency: str
    exchange: str
    asset_class: str


def _day(value):
    if type(value) is not date:
        raise RuntimeFault('INVALID_CONTRACT', '会话事实日期不是已解析业务日期')
    return value.isoformat()


def reject_checks(checks, securities, day):
    for check in checks:
        if (check['binding'] in BRIDGES and check['scope'].get('security') in securities
                and check['scope'].get('date') == day and not check['available']):
            raise RuntimeFault('RULE_UNAVAILABLE', '会话事实的既有正式依赖未就绪')


def check_input_bytes(page):
    evidence = dict(inputs=[item.payload for item in page.execution_inputs],
        identities=[dict(instrument_id=value.instrument_id, currency=value.currency,
            exchange=value.exchange, asset_class=value.asset_class) for _, _, value, _ in page.identities],
        commissions=[row[2] for row in page.commissions], status=[row[2] for row in page.trading_status],
        actions=[row[2] for row in page.corporate_actions], checks=page.checks)
    if len(encoded(evidence)) > PAGE_INPUT_BYTES:
        raise RuntimeFault('RESOURCE_LIMIT', '会话事实输入页超过字节预算')


class LegacySessionInputs:
    """Bounded compatibility for old small tuple callers, never large runs.

    Capture raw inputs once, without materializing the full Cartesian product
    or retaining Rust-composed rows. New large readers use PagedSessionInputs.
    """
    def __init__(self, facts, securities, sessions):
        inputs = facts.execution_inputs
        if type(inputs) is not tuple or not 1 <= len(inputs) <= LEGACY_INPUT_ROWS:
            raise RuntimeFault('INVALID_CONTRACT', '旧小范围执行输入须为有界元组')
        self.securities, self.sessions = securities, sessions
        allowed = frozenset(securities)
        dates = frozenset(sessions.values())
        self.inputs = {}
        for item in inputs:
            if not isinstance(item, ExecutionSessionInput) or type(item.payload) is not dict:
                raise RuntimeFault('INVALID_CONTRACT', '执行输入不是受限会话事实')
            payload = item.payload
            try:
                key = (payload['instrument']['security'], payload['day']['date'])
            except (KeyError, TypeError):
                raise RuntimeFault('RULE_UNAVAILABLE', '执行输入缺少标的或会话日期') from None
            if key[0] not in allowed or key[1] not in dates or key in self.inputs:
                raise RuntimeFault('RULE_UNAVAILABLE', '执行输入重复或超出已核会话/标的')
            self.inputs[key] = ExecutionSessionInput(item.instrument_id, deepcopy(payload))
        if len(self.inputs) != len(securities) * len(sessions):
            raise RuntimeFault('RULE_UNAVAILABLE', '执行输入没有覆盖已核开放会话和授权标的')
        def index(rows, *, identity=False):
            result = {}
            for row in rows:
                key = (row[0], _day(row[1]))
                if key[0] not in allowed or key[1] not in dates:
                    continue
                if key in result:
                    raise RuntimeFault('RULE_UNAVAILABLE', '会话事实依赖存在重复范围')
                value = row[2]
                result[key] = (_Identity(value.instrument_id, value.currency, value.exchange, value.asset_class)
                               if identity else deepcopy(value))
            return result
        self.identities = index(facts.identities, identity=True)
        self.commissions = index(facts.commissions)
        self.status = index(facts.trading_status)
        self.actions = index(facts.corporate_actions)
        for check in facts.checks:
            if (check['binding'] in BRIDGES and check['scope'].get('security') in allowed
                    and check['scope'].get('date') in dates and not check['available']):
                raise RuntimeFault('RULE_UNAVAILABLE', '最终装配的既有正式事实依赖未就绪')

    def read(self, session, offset, limit):
        day = self.sessions[session]
        selected = self.securities[offset:offset + limit]
        stamp = date.fromisoformat(day)
        def rows(mapping, *, identity=False):
            try:
                return tuple((security, stamp, mapping[security, day], ()) if identity else
                    (security, stamp, mapping[security, day]) for security in selected)
            except KeyError:
                raise RuntimeFault('RULE_UNAVAILABLE', '会话事实缺少已核身份、佣金、状态或行动覆盖') from None
        return SessionInputPage(tuple(self.inputs[security, day] for security in selected),
            rows(self.identities, identity=True), rows(self.commissions), rows(self.status), rows(self.actions))


def compose_page(raw, securities, day, session, work, compose):
    """Validate one exact slice and invoke only the original Rust composer."""
    if not isinstance(raw, SessionInputPage):
        raise RuntimeFault('INVALID_CONTRACT', '会话读取器未返回原有依赖的受限事实页')
    expected = frozenset(securities)
    for rows in (raw.execution_inputs, raw.identities, raw.commissions, raw.trading_status, raw.corporate_actions):
        if len(rows) > len(securities):
            raise RuntimeFault('RESOURCE_LIMIT', '会话读取器超过已给本页的行信用')
    def index(rows, *, identity=False):
        result = {}
        for row in rows:
            if (type(row) is not tuple or len(row) != (4 if identity else 3)
                    or row[0] not in expected or _day(row[1]) != day or row[0] in result):
                raise RuntimeFault('RULE_UNAVAILABLE', '会话依赖身份、日期、重复或授权范围无效')
            result[row[0]] = row[2]
        if frozenset(result) != expected:
            raise RuntimeFault('RULE_UNAVAILABLE', '会话依赖未覆盖本页全部授权标的')
        return result
    identities, commissions = index(raw.identities, identity=True), index(raw.commissions)
    status, actions = index(raw.trading_status), index(raw.corporate_actions)
    reject_checks(raw.checks, expected, day)
    payloads = {}
    for item in raw.execution_inputs:
        if not isinstance(item, ExecutionSessionInput) or type(item.payload) is not dict:
            raise RuntimeFault('INVALID_CONTRACT', '执行输入不是原有受限会话 DTO')
        try:
            if (type(item.payload.get('instrument')) is not dict or type(item.payload.get('day')) is not dict
                    or not {'security', 'exchange', 'product'} <= item.payload['instrument'].keys()
                    or not {'security', 'date', 'session'} <= item.payload['day'].keys()):
                raise RuntimeFault('RULE_UNAVAILABLE', '会话输入缺少原执行身份或日期字段')
            security = item.payload['instrument']['security']
        except (KeyError, TypeError):
            raise RuntimeFault('RULE_UNAVAILABLE', '会话输入缺少执行标的') from None
        if type(security) is not str or security not in expected or security in payloads:
            raise RuntimeFault('RULE_UNAVAILABLE', '执行输入重复或超出本页授权标的')
        payloads[security] = item
    if frozenset(payloads) != expected:
        raise RuntimeFault('RULE_UNAVAILABLE', '执行输入没有覆盖本页全部授权标的')
    for security, identity in identities.items():
        if (not isinstance(getattr(identity, 'instrument_id', None), UUID)
                or not isinstance(payloads[security].instrument_id, UUID)
                or any(not hasattr(identity, name) for name in ('currency', 'exchange', 'asset_class'))):
            raise RuntimeFault('RULE_UNAVAILABLE', '会话输入缺少原稳定身份依赖')
    check_input_bytes(raw)
    output = []
    for security in securities:
        item, identity = payloads[security], identities[security]
        action = actions[security]
        if (action.get('events') != () or type(action.get('coverage')) is not tuple
                or not ACTION_FAMILIES <= {row['action_type'] for row in action['coverage']
                    if row['status'] == 'complete' and type(row['event_count']) is int and row['event_count'] == 0}):
            raise RuntimeFault('CAPABILITY_UNAVAILABLE', '会话缺少完整零行动证明或包含尚未接入的非零行动')
        payload = deepcopy(item.payload)
        exchanges = {'SSE': 'shanghai', 'SZSE': 'shenzhen', 'BSE': 'beijing'}
        product = payload['instrument'].get('product', '')
        if (identity.instrument_id != item.instrument_id or identity.currency != 'CNY'
                or exchanges.get(identity.exchange) != payload['instrument']['exchange']
                or identity.asset_class == 'etf' and not product.endswith('_etf')
                or payload['day']['security'] != security or payload['day']['date'] != day
                or payload['day']['session'] != session):
            raise RuntimeFault('RULE_UNAVAILABLE', '执行事实身份、产品、日期或会话与授权不一致')
        if (any(row['status'] == 'suspended' for row in status[security]['facts'])
                and payload['day']['status'] != 'halted'):
            raise RuntimeFault('RULE_UNAVAILABLE', '会话执行状态与已解析停牌事实冲突')
        if {'buy_fees', 'sell_fees', 'origin', 'buy_commission', 'sell_commission', 'cost_overrides'} & payload.keys():
            raise RuntimeFault('INVALID_CONTRACT', '执行输入不能覆盖原规则目录或捕获佣金')
        commission = commissions[security]
        payload.update(buy_commission=commission[0], sell_commission=commission[1],
                       cost_overrides=work.config.get('cost_overrides', {}))
        output.append(compose(payload))
    return output
