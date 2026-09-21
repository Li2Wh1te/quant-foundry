"""Whole-report boundaries, using synthetic inputs rather than private samples."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from uuid import UUID
import pytest
from app.data_foundation.holdings import (normalize_report, ResolvedIdentity,
    ratio, business_date, target_key)
from app.data_foundation.canonical import FoundationError


def fixture():
    report = {'start_date_ms': 1735660800000, 'end_date_ms': 1743350400000,
              'report_type': 'quarter'}
    member = {'thscode': '123456', 'asset_type': 'stock', 'end_date_ms': 1743350400000,
              'report_type': '季度', 'hold_ratio': '4.310', 'period_increase_pct': '-0.26',
              'rank': None, 'market_value': '113882000.00'}
    return {'report_directory': {'item': [report]}, 'item': [
        {'report_key': '2025-03-31:quarter', 'report': report,
         'data': {'item': [member, deepcopy(member)]}}]}


def resolver(code, kind, start, end):
    return ResolvedIdentity(UUID(int=1 if kind == 'fund_share' else 2), UUID(int=3),
                            date(2025, 1, 1), date(2026, 1, 1), datetime.now(timezone.utc))


def receipts(subject='TEST.OF'):
    return [{'interface':'fund.portfolio.stock-history','request_id':'isolated-successful-request',
        'parameters':{'thscode':subject,'end_date':'2025-03-31','report_type':'quarter'}}]


def run(data=None, identity=resolver):
    return normalize_report(subject='TEST.OF', selected_key='2025-03-31:quarter',
                            container=fixture() if data is None else data, resolve_identity=identity, receipt_requests=receipts())


def test_whole_object_preserves_duplicates_nulls_negative_change_and_units():
    r = run()
    assert r['readiness'] == 'ready'
    assert len(r['members']) == 2 and [m['member_ordinal'] for m in r['members']] == [0, 1]
    assert r['members'][0]['hold_ratio'] == Decimal('0.04310')
    assert r['members'][0]['period_change_ratio'] == Decimal('-0.0026')
    assert r['members'][0]['market_value'] is None and r['members'][0]['rank'] is None
    assert r['portfolio_complete'] is None and r['public_at'] is None


def test_missing_identity_quarantines_whole_report_not_only_member():
    r = run(identity=lambda *args: None)
    assert r['readiness'] == 'quarantined'
    assert r['reasons'] == ['FUND_IDENTITY_UNRESOLVED', 'MEMBER_IDENTITY_UNRESOLVED']
    assert len(r['members']) == 2


def test_current_binding_is_not_a_historical_binding():
    def current(*args):
        return ResolvedIdentity(UUID(int=1), UUID(int=2), date(2026, 1, 1),
                                date(2027, 1, 1), datetime.now(timezone.utc))
    assert run(identity=current)['readiness'] == 'quarantined'


@pytest.mark.parametrize('case,reason', [
    ('no_item', 'REPORT_RECEIPT_UNKNOWN'), ('no_directory', 'REPORT_DIRECTORY_MISSING'),
    ('duplicate', 'REPORT_DUPLICATE'), ('pages', 'REPORT_PAGES_MISSING'),
    ('bad_period', 'REPORT_MEMBER_PERIOD_MISMATCH'), ('bad_core', 'REPORT_VALUE_INVALID'),
    ('unknown_failure', 'REPORT_RECEIPT_UNKNOWN')])
def test_incomplete_or_invalid_objects_never_ready(case, reason):
    d = fixture();w = d['item'][0]
    if case == 'no_item': w['data'] = {}
    elif case == 'no_directory': d.pop('report_directory')
    elif case == 'duplicate': d['item'].append(deepcopy(w))
    elif case == 'pages': w['data']['next_cursor'] = 'second-page'
    elif case == 'bad_period': w['data']['item'][0]['end_date_ms'] = 1735574400000
    elif case == 'bad_core': w['data']['item'][0]['hold_ratio'] = '-1'
    elif case == 'unknown_failure': d['failed_requests'] = [{'reason': 'batch_budget'}]
    result = run(d)
    assert result['readiness'] == 'quarantined' and reason in result['reasons']


def test_other_report_failure_does_not_invalidate_selected_object():
    d = fixture();d['failed_requests'] = [{'parameters': {'end_date': '2024-12-31', 'report_type': 'quarter'}}]
    assert run(d)['readiness'] == 'ready'
    d['failed_requests'][0]['parameters']['end_date'] = '2025-03-31'
    assert run(d)['reasons'] == ['REPORT_REQUEST_FAILED']


def test_empty_report_requires_matching_directory_and_successful_wrapper():
    d = fixture();d['item'][0]['data']['item'] = []
    r = run(d)
    assert r['readiness'] == 'ready' and r['header']['member_count'] == 0
    d['report_directory']['item'] = []
    assert run(d)['readiness'] == 'quarantined'


def test_optional_invalid_fields_do_not_remove_core():
    d = fixture();m = d['item'][0]['data']['item'][0]
    m.update(rank=True, period_increase_pct='NaN')
    r = run(d)
    assert r['readiness'] == 'ready'
    assert r['members'][0]['field_quality']['rank'] == 'REPORT_RANK_INVALID'
    assert r['members'][0]['period_change_ratio'] is None


@pytest.mark.parametrize('value', [True, 1.2, 'NaN', 'Infinity', '1e999', '0.000000001'])
def test_precision_and_finiteness_never_round(value):
    with pytest.raises(FoundationError): ratio(value, core=True)


def test_ratio_conversion_ignores_caller_decimal_precision():
    with localcontext() as c:
        c.prec = 2
        assert ratio('4.31234567', core=True) == Decimal('0.0431234567')


def test_date_uses_shanghai_and_rejects_non_date_time():
    assert business_date(1743350400000) == date(2025, 3, 31)
    with pytest.raises(FoundationError): business_date(1743350400001)


def test_business_key_keeps_share_and_report_scope_distinct():
    args = (date(2025, 1, 1), date(2025, 3, 31), 'quarter')
    a = target_key(UUID(int=1), *args)
    assert a != target_key(UUID(int=2), *args)
    assert a != target_key(UUID(int=1), *args, scope='full_portfolio')


def test_item_and_directory_alone_do_not_prove_complete_receipt():
    result = normalize_report(subject='TEST.OF', selected_key='2025-03-31:quarter',
        container=fixture(), resolve_identity=resolver)
    assert result['readiness'] == 'quarantined' and result['reasons'] == ['REPORT_RECEIPT_UNKNOWN']
