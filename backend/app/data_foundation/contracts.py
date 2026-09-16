"""M1 frozen metadata. Registration does not advertise implemented queries."""
from app.data_foundation.catalog import register_definition


def register_bar_contract(session):
    return register_definition(session, kind='contract', name='market.bar.daily', version='1.0', definition={
        'schema_version': 1, 'subject_kind': 'instrument', 'key_fields': ['instrument_id', 'trade_date', 'series'],
        'core_fields': {key: {'type': 'decimal', 'precision': 28, 'scale': 10, 'unit': 'CNY', 'required': True} for key in ('open', 'high', 'low', 'close')},
        'optional_fields': {'volume': {'type': 'decimal', 'precision': 32, 'scale': 8, 'unit': '份'},
                            'turnover': {'type': 'decimal', 'precision': 32, 'scale': 8, 'unit': 'CNY'}},
        'time_semantics': {'business': 'trade_date', 'observation': 'observed_at', 'public_at': 'unknown-unless-proven'},
        'atomic_unit': 'instrument-trading-day', 'supported_filters': ['instrument_id', 'start', 'end'],
    })


def register_initial_catalog(session):
    contract = register_bar_contract(session)
    series = register_definition(session, kind='series', name='cn-etf-cny-1d-unadjusted', version='1', definition={
        'schema_version': 1, 'currency': 'CNY', 'interval': '1d', 'session_scope': 'exchange',
        'price_basis': 'unadjusted', 'method_status': 'not_applicable', 'anchor_status': 'not_applicable'})
    policy = register_definition(session, kind='policy', name='s1-single-source', version='1', definition={
        'schema_version': 1, 'dataset': 'market.bar.daily', 'major': 1, 'profile': 'default',
        'series': series.name, 'input_admission': 'resolved-identity-and-fixed-dependencies',
        'core_fields': ['open', 'high', 'low', 'close'], 'source_order': ['tushare'],
        'comparison': {'enabled': False}, 'fallback': {'enabled': False}, 'atomicity': 'whole-core-unit'})
    support = register_definition(session, kind='support', name='market.bar.daily', version='m2', definition={
        'schema_version': 1, 'read_status': 'not_implemented', 'update_status': 'not_implemented',
        'replay_status': 'dependency_missing', 'notice_days': 30, 'approval_required': True})
    return [contract, series, policy, support]
