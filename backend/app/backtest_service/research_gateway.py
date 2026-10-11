"""D05 research projections on D04's gate/context/read/IPC, never source tables.

Bindings are accepted, code-owned facts about an existing CurrentStore layout.
Current formal observations lack required PIT facts and remain explicit denials.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable, Mapping

import pyarrow as pa
import pyarrow.compute as pc

from .data_gateway import RunDataGateway, GatewayError, _ns
from app.data_store.adapters.registry import BY_ID

TEXT = ('security', 'instrument_id', 'record_id', 'field', 'value_kind', 'value', 'unit', 'report_period')
INTS = ('time_ns', 'effective_from_ns', 'effective_until_ns')
SCHEMA = pa.schema([pa.field(f, pa.string()) for f in TEXT] + [pa.field(f, pa.int64()) for f in INTS],
    metadata={b'schema_id': b'qf.research.v1', b'time_unit': b'utc_nanoseconds',
              b'knowledge_basis': b'verified_public_time'})
FIELDS = {
    'fundamentals': ('revenue', 'net_profit', 'total_assets', 'total_liabilities', 'eps', 'roe'),
    'valuation': ('pe_ratio', 'pb_ratio', 'market_cap', 'circulating_market_cap'),
    'industry': ('industry_code', 'industry_name', 'taxonomy'),
    'instruments': ('name', 'exchange', 'asset_type', 'currency', 'listing_date', 'end_date'),
    'index_stocks': ('member',), 'adjustment': ('factor',), 'status': ('halted',),
    'price': ('open', 'high', 'low', 'close', 'quantity', 'turnover'),
}


@dataclass(frozen=True)
class ResearchBinding:
    name: str
    spec: object
    kind: str
    fields: Mapping[str, str]
    constants: Mapping[str, str | int]
    bounds: Callable
    time_column: str
    field_contract: Mapping[str, tuple[str, str]]
    accepted_facts: tuple[str, ...]
    unavailable_reason: str | None = None
    limitations: tuple[str, ...] = ('current_revision_not_historical_snapshot', 'observed_rows_not_coverage')
    transform: Callable | None = None
    extra_columns: tuple[str, ...] = ()
    derived_columns: tuple[str, ...] = ()
    selectors: tuple = ()
    guards: tuple = ()
    time_offset_ns: int = 0
    factor_anchor: Mapping[str, str] | None = None
    identity_fields: frozenset = field(default=frozenset(SCHEMA.names), init=False)

    def __post_init__(self):
        if (self.kind not in FIELDS or not set(self.field_contract) <= set(FIELDS[self.kind])
                or not set(self.fields) <= set(SCHEMA.names) or not set(self.constants) <= set(SCHEMA.names)
                or any(v not in self.spec.schema.names and v not in self.derived_columns for v in self.fields.values())
                or self.time_column not in self.spec.schema.names or len(self.selectors) > 12):
            raise GatewayError('INVALID_CONTRACT', '研究投影字段/口径契约无效')
        for name in ('fields', 'constants', 'field_contract'):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        if self.factor_anchor is not None:
            object.__setattr__(self, 'factor_anchor', MappingProxyType(dict(self.factor_anchor)))

    def validate_capability(self):
        if not {'verified_public_time', 'stable_identity', 'verified_effective_dates'} <= set(self.accepted_facts):
            raise GatewayError('CAPABILITY_UNAVAILABLE', '研究公开/生效时间或稳定身份依据缺失')
        if self.kind == 'adjustment' and 'verified_cumulative_anchor' not in self.accepted_facts:
            raise GatewayError('CAPABILITY_UNAVAILABLE', '研究复权因子公式和锚点尚未验证')
        if self.kind == 'adjustment':
            anchor = self.factor_anchor
            try:
                if anchor is None or set(anchor) != {'factor', 'effective_ns', 'public_ns'}:
                    raise ValueError()
                value = Decimal(anchor['factor'])
                if (type(anchor['factor']) is not str or len(anchor['factor']) > 128
                        or not value.is_finite() or value <= 0):
                    raise ValueError()
                _ns(anchor['effective_ns']); _ns(anchor['public_ns'])
            except (ValueError, TypeError, InvalidOperation):
                raise GatewayError('CAPABILITY_UNAVAILABLE', '复权原始锚值及真实公开/生效边界尚未明确') from None
        if self.kind == 'index_stocks' and 'verified_complete_sets' not in self.accepted_facts:
            raise GatewayError('CAPABILITY_UNAVAILABLE', '历史成员集合完整性尚未验证')

    def project(self, table, wanted):
        self.validate_capability()
        for column, expected in self.guards:
            if table[column].null_count or table.num_rows and not pc.all(pc.equal(table[column], expected)).as_py():
                raise GatewayError('CAPABILITY_UNAVAILABLE', '研究字段依据未满足已接受口径')
        if self.transform:
            table = self.transform(table)
        arrays = []
        for f in SCHEMA:
            if f.name in self.fields:
                arrays.append(pc.cast(table[self.fields[f.name]], f.type, safe=True))
            elif f.name in self.constants:
                arrays.append(pa.array([self.constants[f.name]]*table.num_rows, type=f.type))
            else:
                arrays.append(pa.nulls(table.num_rows, f.type))
        projected = pa.Table.from_arrays(arrays, schema=SCHEMA)
        # Validate kind/unit per public field; unselected fields cannot silently
        # acquire a value/unit by a client request or a metadata assertion.
        for name, (kind, unit) in self.field_contract.items():
            selection = pc.equal(projected['field'], name)
            matching = projected.filter(pc.fill_null(selection, False))
            if matching.num_rows and (matching['value_kind'].null_count or matching['unit'].null_count
                    or not pc.all(pc.equal(matching['value_kind'], kind)).as_py()
                    or not pc.all(pc.equal(matching['unit'], unit)).as_py()):
                raise GatewayError('CAPABILITY_UNAVAILABLE', '研究原值类型或单位与正式口径不一致')
        if projected.num_rows and (projected['field'].null_count or not pc.all(pc.is_in(projected['field'],
                value_set=pa.array(tuple(self.field_contract), type=pa.string()))).as_py()):
            raise GatewayError('CAPABILITY_UNAVAILABLE', '研究字段未纳入已接受口径')
        return projected

    def batch_metadata(self, table):
        first, last = (pc.min(table['time_ns']).as_py(), pc.max(table['time_ns']).as_py()) if table.num_rows else (None, None)
        metadata = dict(schema_id='qf.research.v1', time_unit='utc_nanoseconds', rows=table.num_rows,
            actual_scope=dict(start_ns=None if first is None else str(first), end_ns=None if last is None else str(last),
                              securities=pc.unique(table['security']).to_pylist()),
            limitations=list(self.limitations), fields={k: list(v) for k, v in self.field_contract.items()})
        if self.kind == 'adjustment':
            metadata['factor_anchor'] = dict(self.factor_anchor)
        return metadata


class ResearchDataGateway(RunDataGateway):
    """Same dependency context and one maintained D04 serve/_transport format."""
    def __init__(self, *args, reference_indices=(), **kwargs):
        super().__init__(*args, **kwargs)
        if len(reference_indices) > 128 or any(type(i) is not str or not i or len(i.encode()) > 128 for i in reference_indices):
            raise GatewayError('RESOURCE_LIMIT', '参考指数范围超限')
        self.reference_indices = frozenset(reference_indices)

    def _request(self, name, request, context):
        binding = self.bindings.get(name) if type(name) is str else None
        if not isinstance(binding, ResearchBinding):
            return super()._request(name, request, context)
        self.declare(name, context)
        # Reject unknown knowledge/factor capabilities before any materialized
        # read, including empty inputs and a smaller-than-row memory budget.
        binding.validate_capability()
        if (type(request) is not dict or set(request) != {'kind', 'securities', 'fields', 'as_of_ns'}
                or request['kind'] != binding.kind or type(request['securities']) is not list
                or type(request['fields']) is not list or len(request['securities']) > 10000
                or not request['fields'] or len(request['fields']) > 128
                or any(type(s) is not str for s in request['securities'])
                or any(type(f) is not str for f in request['fields'])
                or len(set(request['securities'])) != len(request['securities'])
                or len(set(request['fields'])) != len(request['fields'])):
            raise GatewayError('INVALID_CONTRACT', '研究请求契约无效')
        allowed = self.reference_indices if binding.kind == 'index_stocks' else frozenset(self.grant.universe)
        if not set(request['securities']) <= allowed:
            raise GatewayError('DATA_RESTRICTED', '研究标的超出本运行授权/参考范围')
        if not set(request['fields']) <= set(binding.field_contract):
            raise GatewayError('CAPABILITY_UNAVAILABLE', '此入口尚未支持所需研究字段')
        at = _ns(request['as_of_ns'])
        if not self.grant.start_ns <= at <= self.grant.end_ns:
            raise GatewayError('DATA_RESTRICTED', '研究时点超出运行授权窗口')
        # Publication may precede the market lookback. Authorization binds the
        # as_of instant and identities; CurrentStore still bounds scans/rows.
        return binding, -(2**63), at, None

    def read(self, name, request, context, *, page_rows=None):
        binding = self.bindings.get(name) if type(name) is str else None
        if not isinstance(binding, ResearchBinding):
            yield from super().read(name, request, context, page_rows=page_rows)
            return
        binding, start, end, _ = self._request(name, request, context)
        if not request['securities']:
            yield self._batch(pa.Table.from_batches([], schema=SCHEMA), binding)
        else:
            securities = tuple(sorted(request['securities']))
            yield from self._pages(binding, securities, start, end, context, tuple(SCHEMA.names), page_rows=page_rows)
        self.check(context)


FORMAL_RESEARCH_GAPS = {
    'E14': ('fundamentals', 'report_end_not_verified_public_time'),
    'E15': ('fundamentals', 'report_end_not_verified_public_time'),
    'E39': ('valuation', 'observed_snapshot_not_historical_public_valuation'),
    'E40': ('instruments', 'source_local_identity_and_historical_name_validity_unverified'),
    'E56': ('industry', 'classification_effective_dates_and_taxonomy_unverified'),
    'E57': ('index_stocks', 'current_membership_not_historical_membership'),
    'E67': ('instruments', 'source_local_identity_and_historical_name_validity_unverified'),
    'E69': ('adjustment', 'factor_anchor_and_public_time_unverified'),
    'E52': ('price', 'index_points_unit_and_close_visibility_unverified'),
    'E70': ('price', 'provider_reported_basis_and_close_visibility_unverified'),
}


def current_research_bindings():
    """Denial descriptors only; removing the reason cannot enable a stub."""
    def unavailable_bounds(*_):
        raise GatewayError('CAPABILITY_UNAVAILABLE', '正式节点的稳定身份和公开/生效时间投影尚未具备依据')
    result = []
    for entry_id, (kind, reason) in FORMAL_RESEARCH_GAPS.items():
        spec = BY_ID[entry_id].spec
        # A code-owned unusable descriptor, not an invented publication mapping.
        column = next(f.name for f in spec.schema)
        result.append(ResearchBinding('research_'+entry_id, spec, kind, {}, {}, unavailable_bounds,
            column, {}, (), unavailable_reason=reason,
            limitations=tuple(BY_ID[entry_id].describe_capability()['limitations'])))
    return tuple(result)
