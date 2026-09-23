"""Importer receipts retain operational meaning and reject false settlement."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.data_foundation.record_adapters import convert, rows_for
from app.data_foundation.import_progress import CHANNELS


def payload():
    return dict(item=[], artifact=dict(dump_id='test', sha256='a'*64, request_id='request',
        columns=['thscode', 'date_ms'], rows=100, download_bytes=1000,
        observed_start='2026-01-01', observed_end='2026-01-31', numeric_encoding='provider_decimal_or_float_roundtrip'),
        total_subjects=10, imported_subjects=4, superseded_subjects=1,
        requested_start='2026-01-01', requested_end='2026-01-31',
        failed_requests=[dict(reason='batch_budget', pending=6)])


def source(native):
    return SimpleNamespace(source='tonghuashun', dataset=native, subject='market', representation='ths_observation')


@pytest.mark.parametrize('native', CHANNELS)
def test_pending_import_is_a_typed_receipt_not_an_empty_business_window(native):
    ref=source(native); raw=payload()
    assert rows_for(ref,raw)==[raw]
    value=convert(ref,raw)
    assert value['dataset']=='operations.import_progress'
    assert value['subject_key']==native+':market'
    assert value['body']['pending_requests']==[dict(reason='batch_budget', pending=6)]
    assert value['body']['business_publication_complete'] is None
    assert 'business_publication_complete' in value['field_quality']


@pytest.mark.parametrize('change',[
    {'imported_subjects':11}, {'superseded_subjects':5}, {'failed_requests':[]},
    {'total_subjects':True}, {'requested_end':'2026-02-01'}, {'item':[{'close':1}]},
    {'unknown':'not admitted'}, {'failed_requests':[dict(reason='timeout',pending=6)]},
])
def test_invalid_scope_is_never_silently_settled(change):
    with pytest.raises(ValueError):
        convert(source(CHANNELS[0]),dict(payload(),**change))


def test_fully_imported_receipt_still_does_not_prove_business_publication():
    raw=deepcopy(payload());raw.update(imported_subjects=10, failed_requests=[])
    value=convert(source(CHANNELS[0]),raw)
    assert value['body']['pending_requests']==[]
    assert value['body']['business_publication_complete'] is None
