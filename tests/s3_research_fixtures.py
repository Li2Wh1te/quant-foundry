"""Small synthetic accepted research facts for isolated composition tests only.

The real CurrentStore, gateway, IPC and Rust reader consume these fixtures.
This module is never imported by a production resolver or strategy worker.
"""
from uuid import uuid4
import pyarrow as pa

from app.backtest_service.research_gateway import ResearchBinding, SCHEMA
from app.data_store.catalog import SourceUpdate
from app.data_store.schema import DatasetSpec, fingerprint


def research_nodes(store, calendar, *, security='A.SH', reference='REF'):
    at = int(calendar[0]['before_open_ns']) - 1
    values = {
        'fundamentals': [('net_profit', '120', 'decimal', 'CNY')],
        'valuation': [('pe_ratio', '12', 'decimal', 'ratio')],
        'industry': [('industry_code', 'I1', 'text', 'isolated_taxonomy'),
                     ('industry_name', 'Fixture', 'text', 'isolated_taxonomy'),
                     ('taxonomy', 'isolated', 'text', 'isolated_taxonomy')],
        'instruments': [(f, v, 'text', 'identity_attribute') for f, v in
                        [('name', 'Fixture A'), ('exchange', 'SH'), ('asset_type', 'stock'),
                         ('currency', 'CNY'), ('listing_date', '2000-01-01'), ('end_date', None)]],
        'index_stocks': [('member', security, 'text', 'security_key')],
        'price': [('close', '3000.125', 'decimal', 'index_points')],
        'adjustment': [('factor', '2', 'decimal', 'verified_cumulative_factor')],
    }
    bindings = []
    prefix = 's3_review_' + uuid4().hex
    for kind, fields in values.items():
        subject = reference if kind in ('index_stocks', 'price') else security
        schema = pa.schema([pa.field(f.name, f.type, nullable=f.name in
                    ('value', 'report_period', 'effective_until_ns'),
                    metadata={b'max_utf8_bytes': b'128'} if pa.types.is_string(f.type) else
                             {b'unit': b'epoch_ns'}) for f in SCHEMA], metadata=SCHEMA.metadata)
        dataset = DatasetSpec(prefix + '_' + kind, schema, ('security', 'record_id', 'field'),
                              'synthetic-s3-review-v1', {'knowledge_basis': 'synthetic_verified_public_time'})
        store.register(dataset)
        rows = [dict(security=subject, instrument_id='isolated-stable-' + subject, record_id='fixture',
                     field=f, value=v, value_kind=t, unit=u, report_period='2025-12-31',
                     time_ns=at, effective_from_ns=at, effective_until_ns=None) for f, v, t, u in fields]
        rows.sort(key=lambda r: dataset.key_bytes(tuple(r[k] for k in dataset.key)))
        store.upsert(dataset, 'default', [pa.RecordBatch.from_pylist(rows, schema=schema)],
                     SourceUpdate('s3-review', fingerprint(prefix), fingerprint('synthetic'), 0),
                     lower=(subject,), upper=(subject + '\0',), source_check=lambda _: True)
        bindings.append(ResearchBinding(kind, dataset, kind, {n: n for n in SCHEMA.names}, {},
            lambda s, *_: ((s,), (s + '\0',)), 'time_ns', {f: (t, u) for f, _, t, u in fields},
            ('verified_public_time', 'stable_identity', 'verified_effective_dates',
             'verified_cumulative_anchor', 'verified_complete_sets'),
            limitations=('synthetic_business_oracle_not_production_data',),
            factor_anchor=dict(factor='1', public_ns=str(at - 1), effective_ns=str(at - 1))
                          if kind == 'adjustment' else None))
    return tuple(bindings)
