"""NAV categories and complete adjustment windows remain separate contracts."""
from decimal import Decimal, localcontext
import pytest
from pydantic import ValidationError
from app.data_foundation.canonical import FoundationError
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.record_schemas import validate_body
from tests.test_foundation_records import source


def payload(points):
    return {'item': points, 'coverage': 'provider_rolling_window',
        'observed_start': '2026-09-18', 'observed_end': '2026-09-18'}


def converted(points):
    ref = source('fund_nav', '000001.OF')
    rows = rows_for(ref, payload(points))
    assert len(rows) == 1
    return convert(ref, rows[0])


def test_exact_categories_missing_and_zero_survive_without_invented_units():
    with localcontext() as context:
        context.prec = 3
        result = converted([
            {'nav_date': '2026-09-18', 'unit_nav': 0},
            {'nav_date': '2021-09-15', 'unit_nav': Decimal('1.238000'), 'adj_nav': Decimal('7.64751234567890123456789012345678')}])
    body = result['body']
    assert body['sequence_start'] == '2021-09-15'
    assert body['sequence_end'] == '2026-09-18'
    assert body['points'][0] == {'nav_date': '2021-09-15', 'reported_unit_nav': '1.238',
        'reported_adjusted_nav': '7.64751234567890123456789012345678'}
    assert body['points'][1]['reported_unit_nav'] == '0'
    assert body['points'][1]['reported_adjusted_nav'] is None
    assert result['business_date'] is None
    for field in ('currency', 'share_basis', 'adjustment_formula', 'cumulative_nav'):
        assert body[field] is None and result['field_quality'][field]


@pytest.mark.parametrize('bad', [True, 1.1, Decimal('NaN'), Decimal('Infinity'), Decimal('-1'), '1e-33', '1e32', 'invalid'])
def test_bad_value_quarantines_whole_window(bad):
    with pytest.raises(FoundationError, match='整组隔离'):
        converted([{'nav_date': '2026-09-17', 'unit_nav': 1}, {'nav_date': '2026-09-18', 'adj_nav': bad}])


@pytest.mark.parametrize('points', [[], [None], [{'nav_date': '2026-09-18'}],
    [{'nav_date': 1789660800001, 'unit_nav': 1}],
    [{'nav_date': '2026-09-18', 'unit_nav': 1}, {'nav_date': '2026-09-18', 'unit_nav': 2}]])
def test_no_empty_truncated_or_duplicate_sequence(points):
    with pytest.raises(FoundationError):
        converted(points)


def test_schema_rechecks_sequence_bounds_and_unsupported_money_claims():
    body = converted([{'nav_date': '2026-09-18', 'unit_nav': 1}])['body']
    for changes in ({'sequence_start': '2026-09-17'}, {'currency': 'CNY'},
                    {'points': body['points'] * 2}, {'cumulative_nav': '1'}):
        with pytest.raises(ValidationError):
            validate_body('fund.nav_snapshot', body | changes)
    with pytest.raises(FoundationError):
        ref = source('fund_nav', 'F')
        convert(ref, rows_for(ref, {'item': [{'nav_date': '2026-09-18', 'unit_nav': 1}]})[0])
