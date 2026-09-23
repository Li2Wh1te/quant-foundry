"""Career observations preserve text and reject false quantitative certainty."""
import pytest
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.canonical import FoundationError
from tests.test_foundation_records import source


def test_assignment_text_and_unknown_performance_remain_distinct():
    ref = source('fund_manager_experience', 'M1')
    result = convert(ref, {'investment_history': {
        'B': {'code': 'B', 'start': '2020-01-01', 'end': '至今', 'hs_rate': '1.23'},
        'A': {'code': 'A', 'name': 'Source name'}}, 'heavy_assets': {'stock': []}})
    assert result['subject_key'] == 'M1' and result['business_date'] is None
    assert [entry['fund_code'] for entry in result['body']['assignments']] == ['A', 'B']
    assert result['body']['assignments'][1]['end_text'] == '至今'
    assert result['body']['performance'] is None
    assert result['field_quality'] == {'performance': 'PERFORMANCE_UNITS_UNVERIFIED',
        'awards': 'MISSING', 'heavy_assets': 'SEMANTICS_UNVERIFIED'}
    assert 'hs_rate' not in str(result['body'])


@pytest.mark.parametrize('history', [None, [], {'F': {'code': 'other'}}, {'F': None}, {'F': {'code': 'F', 'start': True}}])
def test_malformed_assignment_cannot_silently_remove_a_member(history):
    with pytest.raises(FoundationError):
        convert(source('fund_manager_experience', 'M'), {'investment_history': history})


def test_empty_history_requires_explicit_container():
    ref = source('fund_manager_experience', 'M')
    assert convert(ref, {'investment_history': {}})['body']['assignments'] == []
    for content in ({}, {'item': []}, {'item': [{}, {}]}):
        with pytest.raises(FoundationError):
            rows_for(ref, content)
