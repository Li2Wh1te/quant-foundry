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


def register_holdings_catalog(session):
    """Declare stock holdings with explicit scope and observed-only time."""
    from app.data_foundation.holdings import DATASET, SERIES
    register_definition(session, kind='series', name=SERIES, version='1', definition={
        'schema_version': 1, 'object_kind': 'holdings-report', 'scope_kind': 'provider_reported', 'asset_scope': 'stock',
        'weight_unit': 'ratio', 'public_time_status': 'unverified', 'portfolio_completeness': 'unknown'})
    register_definition(session, kind='support', name=DATASET, version='m5', definition={
        'schema_version': 1, 'read_status': 'implemented', 'update_status': 'bounded_manual',
        'replay_status': 'verify_execution_dependencies', 'notice_days': 30, 'approval_required': True,
        'supported_series': [SERIES], 'time_modes': ['observed'], 'max_report_objects': 100,
        'pagination': True, 'market_value_unit': 'unverified'})
    contract = register_definition(session, kind='contract', name=DATASET, version='1.0', definition={
        'schema_version': 1, 'subject_kind': 'fund_share',
        'key_fields': ['fund_share_id', 'period_start', 'period_end', 'report_type', 'scope_kind', 'series'],
        'core_fields': {'hold_ratio': {'type': 'decimal', 'precision': 20, 'scale': 10, 'unit': 'ratio', 'required': True}},
        'optional_fields': {'market_value': {'type': 'decimal', 'precision': 32, 'scale': 8, 'unit': 'CNY'},
            'period_change_ratio': {'type': 'decimal', 'precision': 20, 'scale': 10, 'unit': 'ratio'},
            'rank': {'type': 'integer', 'unit': 'rank'}},
        'time_semantics': {'business': 'report_period', 'observation': 'observed_at', 'public_at': 'unknown-unless-proven'},
        'atomic_unit': 'whole-report', 'supported_filters': ['fund_share_id', 'report_period', 'report_type', 'scope_kind']})
    policy = register_definition(session, kind='policy', name='holdings-single-source', version='1', definition={
        'schema_version': 1, 'dataset': DATASET, 'major': 1, 'profile': 'default', 'series': SERIES,
        'input_admission': 'resolved-identity-and-complete-object', 'core_fields': ['hold_ratio'],
        'source_order': ['tonghuashun'], 'comparison': {'enabled': False}, 'fallback': {'enabled': False},
        'atomicity': 'whole-report'})
    return contract, policy
