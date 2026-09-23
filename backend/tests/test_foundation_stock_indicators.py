"""Nested provider indicators preserve nulls and reject ambiguous identities."""
import copy
import pytest
from tests.test_foundation_records import source
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.canonical import FoundationError


def payload():
    return dict(item=[dict(thscode='000001.SZ', report='2026-2', abilities=[
        dict(ability='growth', indicators=[dict(index_id='provider-growth', value='-1.23000001'),
                                          dict(index_id='unknown', value=None)])])])


def converted(raw):
    ref = source('stock_indicators', '000001.SZ')
    return convert(ref, rows_for(ref, raw)[0])['body']


def test_provider_ids_exact_values_and_nulls_are_not_inferred():
    body = converted(payload())
    values = body['reports'][0]['abilities'][0]['indicators']
    assert values == [{'provider_indicator': 'provider-growth', 'reported_value': '-1.23000001'},
                      {'provider_indicator': 'unknown', 'reported_value': None}]
    assert body['calculation_formula'] is None and body['as_filed_history'] is None
    assert converted({'item': []})['reports'] == []


@pytest.mark.parametrize('mutation', ['subject', 'period', 'ability', 'indicator', 'missing_value', 'bad_value'])
def test_malformed_nested_member_invalidates_whole_window(mutation):
    raw = payload(); report = raw['item'][0]; group = report['abilities'][0]
    if mutation == 'subject':report['thscode'] = 'OTHER'
    elif mutation == 'period':raw['item'].append(copy.deepcopy(report))
    elif mutation == 'ability':report['abilities'].append(copy.deepcopy(group))
    elif mutation == 'indicator':group['indicators'].append(copy.deepcopy(group['indicators'][0]))
    elif mutation == 'missing_value':del group['indicators'][0]['value']
    elif mutation == 'bad_value':group['indicators'][0]['value'] = 'Infinity'
    with pytest.raises(FoundationError):converted(raw)
