"""Atomic index set semantics, independently of publication storage."""
import pytest
from pydantic import ValidationError
from tests.test_foundation_records import source
from app.data_foundation.canonical import FoundationError
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.record_schemas import validate_body


@pytest.mark.parametrize('dataset,subject', [('index_catalog', 'industry'), ('index_constituents', '000001.SH')])
def test_index_set_is_atomic_sorted_and_has_no_inferred_effective_date(dataset, subject):
    ref = source(dataset, subject)
    rows = rows_for(ref, {'item': [{'thscode': 'B.SH'}, {'thscode': 'A.SH', 'name': 'Source name'}], 'timestamp': 1})
    assert len(rows) == 1
    value = convert(ref, rows[0])
    assert value['business_date'] is None
    assert value['subject_key'] == subject
    assert [m['source_code'] for m in value['body']['members']] == ['A.SH', 'B.SH']
    assert value['body']['members'][0]['ticker'] is None
    assert 'timestamp' not in value['body']
    assert convert(ref, rows_for(ref, {'item': []})[0])['body']['members'] == []
    with pytest.raises(FoundationError):
        rows_for(ref, {})


@pytest.mark.parametrize('members', [[{'thscode': 'A.SH'}, {}], [{'thscode': 'A.SH'}, {'thscode': 'A.SH'}],
    [None], [{'thscode': True}], [{'thscode': 'A.SH', 'ticker': 1}], [{'thscode': 'A.SH', 'name': ''}]])
def test_bad_member_rejects_whole_set(members):
    ref = source('index_constituents', 'I.SH')
    with pytest.raises((FoundationError, ValidationError)):
        convert(ref, rows_for(ref, {'item': members})[0])


def test_category_identity_and_canonical_kind_are_verified():
    with pytest.raises(FoundationError):
        convert(source('index_catalog', 'invented'), {'members': []})
    body = convert(source('index_catalog', 'industry'), {'members': []})['body']
    with pytest.raises(ValidationError):
        validate_body('index.constituent_snapshot', body)
