"""Offering filters remain observed sets, with intraday instants intact."""
from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.record_schemas import validate_body
from app.data_foundation.canonical import FoundationError
from tests.test_foundation_records import source


def converted(members, category='active'):
    ref = source('fund_offerings', category)
    rows = rows_for(ref, {'item': members, 'collection_scope': {'subscribe': category}})
    assert len(rows) == 1
    return convert(ref, rows[0])


def test_source_closing_time_is_not_truncated_to_a_date_or_live_eligibility():
    result = converted([{'thscode': '028311.OF', 'ticker': '028311',
        'subscription_start_ms': Decimal('1786896000000'), 'subscription_end_ms': 1794812400000}])
    member = result['body']['members'][0]
    assert member['reported_subscription_start'] == '2026-08-16T16:00:00Z'
    assert member['reported_subscription_end'] == '2026-11-16T07:00:00Z'
    assert result['body']['current_subscription_eligibility'] is None
    assert result['field_quality']['current_subscription_eligibility'] == 'CURRENT_ELIGIBILITY_UNVERIFIED'
    assert result['business_date'] is None


@pytest.mark.parametrize('value', [True, 1794812400000.0, Decimal('NaN'), Decimal('Infinity'), Decimal('1786896000000.1'), '1794812400000', Decimal('1e100')])
def test_bad_member_timestamp_never_turns_into_an_absent_member(value):
    with pytest.raises(FoundationError):
        converted([{'thscode': 'GOOD.OF'}, {'thscode': 'BAD.OF', 'subscription_end_ms': value}])


@pytest.mark.parametrize('members', [[{}], [None], [{'thscode': 'A.OF', 'ticker': 123}],
    [{'thscode': 'A.OF'}, {'thscode': 'A.OF'}],
    [{'thscode': 'A.OF', 'subscription_start_ms': 1794812400000, 'subscription_end_ms': 1786896000000}]])
def test_invalid_or_conflicting_members_reject_whole_set(members):
    with pytest.raises((FoundationError, ValidationError)):
        converted(members)


def test_explicit_empty_and_missing_boundaries_have_different_meanings():
    assert converted([], 'upcoming')['body']['members'] == []
    member = converted([{'thscode': 'A.OF'}])['body']['members'][0]
    assert member['reported_subscription_start'] is None and member['reported_subscription_end'] is None
    with pytest.raises(FoundationError):
        rows_for(source('fund_offerings', 'active'), {})
    with pytest.raises(FoundationError):
        rows_for(source('fund_offerings', 'active'), {'item': [], 'collection_scope': {'subscribe': 'upcoming'}})
    with pytest.raises(FoundationError):
        converted([], 'invented')


def test_canonical_schema_rejects_implicit_epoch_or_naive_timestamp():
    body = converted([{'thscode': 'A.OF'}])['body']
    for value in [1794812400000, '2026-11-16T15:00:00', '2026-11-16T15:00:00+08:00']:
        changed = body | {'members': [body['members'][0] | {'reported_subscription_end': value}]}
        with pytest.raises(ValidationError):
            validate_body('fund.offering_snapshot', changed)
