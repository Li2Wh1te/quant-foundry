"""Formal daily node consumption, given separately accepted small facts.

No writer, provider lookup, source-table fallback, new fact registry or market
copy exists here. The default source contracts still lack execution facts;
only a trusted code-owned resolver may supply them. REST cannot supply a plan.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType

import pyarrow as pa

from app.data_store.adapters.registry import BY_ID
from .data_gateway import GatewayError, MARKET_SCHEMA, MarketBinding, RunDataGateway
from .runtime_contracts import RuntimeFault, WorkerLimits


REQUIRED_EXECUTION_FACTS = frozenset({
    'CNY_prices', 'raw_execution_prices', 'exact_integer_share_conversion',
    'stable_identity_validity', 'accepted_daily_sessions',
})


def daily_node_binding(entry_id, *, representation, identities, calendar,
                       shares_per_quantity, accepted_facts, evidence):
    """Project E50/E70 node dates into completed bars without guessing units.

    identities maps a public run key to (native subject, stable identity).
    calendar consists of already accepted D03 CalendarSession DTOs. Evidence
    and acceptance come from a trusted resolver, never observation timestamps,
    account metadata or user run parameters. A synthetic resolver is allowed
    only in an explicitly named isolated model, as enforced by the provider.
    """
    if entry_id not in ('E50', 'E70'):
        raise GatewayError('CAPABILITY_UNAVAILABLE', '该正式价格口径不可用于股票/ETF raw 成交')
    if not REQUIRED_EXECUTION_FACTS <= frozenset(accepted_facts) or not evidence:
        raise GatewayError('CAPABILITY_UNAVAILABLE', '正式价格货币、raw基础、份额转换、身份或会话依据缺失')
    if (type(representation) is not str or not representation.startswith(BY_ID[entry_id].source + ':')
            or len(representation.encode()) > 128 or not 1 <= len(identities) <= 10000):
        raise GatewayError('INVALID_CONTRACT', '正式节点来源或身份集合无效')
    identities = dict(identities)
    if any(type(k) is not str or not k or type(v) is not tuple or len(v) != 2
           or any(type(x) is not str or not x for x in v) for k, v in identities.items()):
        raise GatewayError('CAPABILITY_UNAVAILABLE', '正式节点稳定身份和来源标识映射缺失')
    if len({v[0] for v in identities.values()}) != len(identities):
        raise GatewayError('CAPABILITY_UNAVAILABLE', '正式节点身份映射存在歧义')
    try:
        if type(shares_per_quantity) is not str:
            raise ValueError()
        multiplier = Decimal(shares_per_quantity)
        if (not multiplier.is_finite() or multiplier <= 0 or len(shares_per_quantity)>128
                or len(multiplier.as_tuple().digits)>38
                or not -38 <= multiplier.as_tuple().exponent <= 0):
            raise ValueError()
    except (ValueError, InvalidOperation):
        raise GatewayError('CAPABILITY_UNAVAILABLE', '原数量到份额的已接受精确换算缺失') from None
    sessions = {}
    for row in calendar:
        day = date.fromisoformat(row['date'])
        if (day in sessions or row['session']['exchange_timezone'] != 'Asia/Shanghai'
                or not row['bar_windows']):
            raise GatewayError('RULE_UNAVAILABLE', '正式日线会话重复或缺少交易区间')
        start = int(row['bar_windows'][0]['start_ns'])
        end = int(row['bar_windows'][-1]['end_ns'])
        if not -(2**63) <= start < end < 2**63 or end != int(row['session']['close_ns']):
            raise GatewayError('RULE_UNAVAILABLE', '正式日线完成边界无效')
        sessions[day] = (start, end, row['session']['key'])
    if not 1 <= len(sessions) <= 10000:
        raise GatewayError('RESOURCE_LIMIT', '正式会话集合超出预算')
    sessions = MappingProxyType(sessions)
    reverse = {native: public for public, (native, _) in identities.items()}
    entry = BY_ID[entry_id]
    model = entry.projection[-2]
    column = lambda field: entry.layout.columns[model, field][0]
    day_column = column('trade_date')
    quantity_column = column('volume_lots' if entry_id == 'E70' else 'reported_volume')
    fields = {f: column(f if entry_id == 'E70' else 'reported_' + f)
              for f in ('open', 'high', 'low', 'close')}
    fields.update(security='subject', time_ns='_end', interval_start_ns='_start',
                  interval_end_ns='_end', session='_session', source_session='_session',
                  quantity='_shares', stable_input_sequence='_identity')

    def time_bounds(start, end):
        visible = [day for day, (_, close, _) in sessions.items() if start <= close <= end]
        # The actual gate/read still runs for an empty visible range.
        return (min(visible), max(visible)) if visible else (date.max, date.min)

    def bounds(security, start, end):
        if security not in identities:
            raise GatewayError('DATA_RESTRICTED', '正式来源身份超出授权集合')
        lower, upper = time_bounds(start, end)
        subject = identities[security][0]
        if lower > upper:
            lower = upper = date.min
        return ((representation, subject, lower.isoformat()),
                (representation, subject, upper.isoformat() + '\0'))

    def project(table):
        days = table[day_column].to_pylist()
        subjects = table['subject'].to_pylist()
        values = table[quantity_column].to_pylist()
        starts, ends, keys, quantities, securities = [], [], [], [], []
        for day, subject, raw in zip(days, subjects, values):
            if day not in sessions or subject not in reverse:
                raise GatewayError('CAPABILITY_UNAVAILABLE', '行情日期或身份没有已接受的会话映射')
            if raw is None:
                raise GatewayError('CAPABILITY_UNAVAILABLE', '成交数量缺失，不能假定无限流动性')
            try:
                with localcontext() as ctx:
                    # Both source DECIMAL and the accepted multiplier have at
                    # most 38 coefficient digits: 100 retains their complete
                    # product before any integer/range check (never rounding
                    # a near-integer into a tradable share quantity).
                    ctx.prec = 100
                    quantity = Decimal(str(raw)) * multiplier
                if (not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value()
                        or quantity >= 2**63):
                    raise ValueError()
            except (ValueError, InvalidOperation):
                raise GatewayError('NUMERIC_RANGE_UNSUPPORTED', '原数量不能无损转换为整数份额') from None
            start, end, key = sessions[day]
            starts.append(start); ends.append(end); keys.append(key)
            quantities.append(int(quantity)); securities.append(reverse[subject])
        table = table.set_column(table.schema.get_field_index('subject'), 'subject', pa.array(securities))
        for name, values, kind in (('_start', starts, pa.int64()), ('_end', ends, pa.int64()),
                                  ('_session', keys, pa.string()), ('_shares', quantities, pa.int64()),
                                  ('_identity', [x + 2**63 for x in ends], pa.uint64())):
            table = table.append_column(name, pa.array(values, type=kind))
        return table

    return MarketBinding(entry_id, entry.spec, '1d', fields,
        dict(kind='bar', channel=representation, sequence=1), bounds, day_column,
        transform=project, extra_columns=(quantity_column,),
        derived_columns=('_start', '_end', '_session', '_shares', '_identity'),
        query_time_bounds=time_bounds,
        storage_securities=lambda securities: tuple(identities[s][0] for s in securities),
        count_order=(day_column,),
        limitations=tuple(entry.describe_capability()['limitations']) +
                     ('accepted_execution_projection_not_historical_snapshot',))


class RunStoreGateway(RunDataGateway):
    """Original gateway gates plus separately accepted small-fact consistency."""
    def __init__(self, *args, **kwargs):
        self.owned_store = kwargs.pop('owned_store', False)
        self.check_facts = kwargs.pop('check_facts', None)
        super().__init__(*args, **kwargs)

    def check(self, context):
        unchanged = super().check(context)
        if self.check_facts is not None:
            self.check_facts(None)
        return unchanged

    def finalize(self, context, commit):
        # The accepted-fact producer must check/lock its existing small facts
        # using this SAME bounded final transaction. It may not perform remote
        # I/O or create a snapshot. A stale producer token must raise DATA_CHANGED.
        def checked_commit(connection):
            if self.check_facts is not None:
                self.check_facts(connection)
            return commit(connection)
        return super().finalize(context, checked_commit)

    def close(self, context):
        try:
            super().close(context)
        finally:
            if self.owned_store:
                self.store.close()
                self.owned_store = False


class FormalDailyGateway(RunStoreGateway):
    """One bounded completed bar, globally ordered by the public run key.

    Native subject order need not match stable public identity order. Gather
    at most one row per authorized symbol at ONE daily boundary, then sort and
    slice to IPC credit. Never retain/sort full history or copy source files.
    """
    def __init__(self, *args, calendar, **kwargs):
        super().__init__(*args, **kwargs)
        self.closes = tuple(sorted(int(row['session']['close_ns']) for row in calendar))

    def _point(self, name, request, context, page_rows):
        parts=[];rows=bytes_used=0
        for batch in super().read(name,request,context):
            rows+=batch.table.num_rows;bytes_used+=batch.table.nbytes
            if rows>len(request['securities']) or bytes_used+16384>self.batch_bytes:
                raise GatewayError('RESOURCE_LIMIT','单个正式日线边界超过唯一标的或批次预算')
            parts.append(batch.table)
        table=pa.concat_tables(parts) if parts else pa.Table.from_batches([],schema=MARKET_SCHEMA)
        ordered=self._batch(table,self.bindings[name]).table
        credit=min(page_rows or self.batch_rows,self.batch_rows)
        if not rows:
            self.check(context)
            yield self._batch(ordered,self.bindings[name])
        for offset in range(0,rows,credit):
            self.check(context)
            yield self._batch(ordered.slice(offset,credit),self.bindings[name])
        self.check(context)

    def read(self, name, request, context, *, page_rows=None):
        _, start, end, count = self._request(name, request, context)
        if count is not None or not request['securities']:
            yield from super().read(name, request, context, page_rows=page_rows)
            return
        if start == end:
            yield from self._point(name,request,context,page_rows)
            return
        emitted = False
        for close in self.closes:
            if start <= close <= end:
                point = dict(request, start_ns=str(close), end_ns=str(close))
                yield from self._point(name,point,context,page_rows)
                emitted = True
        if not emitted:
            yield from self._point(name,dict(request,start_ns=str(end)),context,page_rows)


@dataclass(frozen=True)
class AcceptedRunPlan:
    """Ephemeral trusted D03/D04/D02 DTOs; no persisted market/fact snapshot."""
    market: MarketBinding
    spec: dict
    session_facts: object = None
    check_facts: object = None
    research: tuple = ()
    reference_indices: tuple[str, ...] = ()

    def validate(self, work):
        from .resampling import can_resample
        from .research_gateway import ResearchBinding
        if (type(self.research) is not tuple or len(self.research) > 32
                or any(not isinstance(b, ResearchBinding) for b in self.research)
                or len({b.name for b in (self.market, *self.research)}) != 1 + len(self.research)
                or len({b.kind for b in self.research}) != len(self.research)
                or type(self.reference_indices) is not tuple or len(self.reference_indices) > 128
                or any(type(s) is not str or not s or len(s.encode()) > 128 for s in self.reference_indices)
                or len(set(self.reference_indices)) != len(self.reference_indices)):
            raise RuntimeFault('INVALID_CONTRACT', '正式研究映射或参考指数集合无效')
        if (self.market.unavailable_reason or (self.market.frequency != work.config['frequency']
                and not can_resample(self.market.frequency, work.config['frequency']))
                or work.config['frequency'] not in ('1d','1m','5m','15m','30m','60m','tick') or not self.spec.get('calendar')
                or not self.spec.get('rules') or not self.spec.get('calendar_reference')
                or self.spec.get('source', {}).get('kind') != 'ipc'):
            raise RuntimeFault('CAPABILITY_UNAVAILABLE', '正式运行频率、会话、规则或来源契约未就绪')
        limits = getattr(work, 'limits', None) or WorkerLimits()
        data_bytes = self.spec.get('data_budget_bytes')
        if (not isinstance(limits, WorkerLimits) or type(data_bytes) is not int
                or not 0 < data_bytes <= limits.memory_bytes//2):
            raise RuntimeFault('RESOURCE_LIMIT', '正式数据信用超过已批准worker内存的一半，拒绝受理')
        dates = [row['date'] for row in self.spec['calendar']]
        if dates != sorted(set(dates)) or dates[0] < work.config['start'] or dates[-1] > work.config['end']:
            raise RuntimeFault('RULE_UNAVAILABLE', '正式运行会话与请求日期不一致')
        if not callable(self.check_facts):
            raise RuntimeFault('RULE_UNAVAILABLE', '缺少日期适用的身份、状态、费用和公司行动检查')
        self.check_facts(work, connection=None)
        required = (('price','quantity') if work.config['execution_model']=='trade_tick_v1' else
            ('bid','ask','bid_quantity','ask_quantity') if work.config['frequency']=='tick' else
            ('open','high','low','close','quantity'))
        if not set(required)<=set(self.market.fields):
            raise RuntimeFault('CAPABILITY_UNAVAILABLE','已接受输入缺少该执行模型的价格或数量字段')
        return self.spec['calendar_reference']

    def packet(self, work, run_id):
        result = deepcopy(self.spec)
        result.update(run_id=run_id, config=deepcopy(work.config))
        fields = ([f for f in ('price','quantity','bid','ask','bid_quantity','ask_quantity') if f in self.market.fields]
                  if work.config['frequency']=='tick' else ['open','high','low','close','quantity'])
        result['source'] = dict(kind='ipc', binding=self.market.name,
            socket_path='/run/qf/ipc/data.sock', request=dict(
                securities=list(work.config['universe']), fields=fields,
                frequency=work.config['frequency'], start_ns=result['calendar'][0]['before_open_ns'],
                end_ns=result['calendar'][-1]['after_close_ns'], count_per_security=None, adjustment='none'))
        return result
