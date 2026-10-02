"""Existing source/canonical bytes remain stable under encoder reuse."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum
import hashlib
from uuid import UUID

import pytest

from app.data_store.adapters.canonical import NativeInputError, encode, normalized
from app.data_store.adapters.contracts import LocalInput, native_json, _input_group, _representation
from app.data_store.merge import value_hash


class Count(IntEnum):
    ONE = 1


def examples():
    instant = datetime(2026, 1, 2, tzinfo=timezone(timedelta(hours=8)))
    return {
        'z': [True, False, None, Count.ONE, -7, 123456789012345678901234567890],
        'a': Decimal('12345678901234567890.12345678901234567890'),
        'zero': Decimal('-0'), 'scale': Decimal('1.2300'),
        '日': date(2026, 1, 2), '时': instant,
        'id': UUID('12345678-1234-5678-1234-567812345678'),
        'text': '中文\n"\\/\t',
    }


NATIVE = ('{"a":12345678901234567890.12345678901234567890,'
          '"id":"12345678-1234-5678-1234-567812345678","scale":1.2300,'
          '"text":"中文\\n\\"\\\\/\\t",'
          '"z":[true,false,null,1,-7,123456789012345678901234567890],'
          '"zero":-0.0,"日":"2026-01-02","时":"2026-01-02T00:00:00+08:00"}')
CANONICAL = ('{"a":"12345678901234567890.1234567890123456789",'
             '"id":"12345678-1234-5678-1234-567812345678","scale":"1.23",'
             '"text":"中文\\n\\"\\\\/\\t",'
             '"z":[true,false,null,1,-7,123456789012345678901234567890],'
             '"zero":"0","日":"2026-01-02","时":"2026-01-01T16:00:00+00:00"}')


@pytest.mark.parametrize('encoder,expected', [(native_json, NATIVE), (encode, CANONICAL)])
def test_source_and_canonical_v1_exact_byte_contract(encoder, expected):
    assert encoder(examples()).encode() == expected.encode()
    # The reusable encoder must not retain a previous call or share mutable
    # iteration state between ordinary concurrent ingestion workers.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: encoder(examples()), range(32)))
    assert results == [expected] * 32


@pytest.mark.parametrize('value', [1.5, float('nan'), float('inf'), Decimal('NaN'), Decimal('Infinity')])
@pytest.mark.parametrize('encoder', [native_json, encode])
def test_reuse_cannot_admit_inexact_or_nonfinite_source_values(encoder, value):
    with pytest.raises((NativeInputError, ValueError)):
        encoder({'nested': [value]})


@pytest.mark.parametrize('encoder', [native_json, encode])
def test_reuse_cannot_coerce_nonstring_source_keys(encoder):
    with pytest.raises((NativeInputError, ValueError)):
        encoder({1: 'not a source field'})


def test_flat_native_c_encoder_preserves_sorted_unicode_and_large_integer_bytes():
    value = {'z': 123456789012345678901234567890, 'a': '中文\n"\\', 'n': None, 'b': True}
    assert native_json(value) == '{"a":"中文\\n\\"\\\\","b":true,"n":null,"z":123456789012345678901234567890}'
    assert native_json(('中文', -7, False, None)) == '["中文",-7,false,null]'
    assert list(normalized({'z': {'b': 2, 'a': 1}, 'a': 0})) == ['a', 'z']
    assert list(normalized({'z': {'b': 2, 'a': 1}})['z']) == ['a', 'b']


def test_flat_decimal_runs_preserve_literal_scale_exponent_zero_and_key_positions():
    value = {'a': True, 'b': Decimal('0.000000000001'), 'c': '中文',
             'd': Decimal('1.2300'), 'e': None, 'f': Decimal('-0')}
    assert native_json(value) == '{"a":true,"b":1E-12,"c":"中文","d":1.2300,"e":null,"f":-0.0}'
    assert native_json({'z': Decimal('2.000'), 'a': Decimal('1E+9')}) == '{"a":1E+9,"z":2.000}'


def test_projected_fingerprint_matches_existing_canonical_v1_golden_bytes():
    rows = [{'member_key':'root','row_kind':0,'f0':Decimal('1234.5000'),
             'f1':date(2026,1,2),'f2':{'nested':[True,1,None]},
             'f3':None,'quality_json':'{"anchor_date":"UNVERIFIED"}',
             'basis_ns':17,'basis_token':'excluded'}]
    expected = ('[{"f0":"1234.5","f1":"2026-01-02","f2":{"nested":[true,1,null]},'
                '"member_key":"root","quality_json":"{\\"anchor_date\\":\\"UNVERIFIED\\"}","row_kind":0}]')
    assert value_hash(rows) == hashlib.sha256(expected.encode()).hexdigest()
    rows[0]['basis_ns'] = 18
    assert value_hash(rows) == hashlib.sha256(expected.encode()).hexdigest()
    for bad in (1.5, Decimal('NaN')):
        rows[0]['f2'] = {'nested':[bad]}
        with pytest.raises((ValueError, NativeInputError)):
            value_hash(rows)


def test_semantic_cache_uses_all_identity_dimensions_and_not_values_or_observation():
    value = LocalInput('tushare', 'etf_adjustment_factors', '000001.SH', 'default',
                       datetime(2026, 1, 1, tzinfo=timezone.utc), {}, '0'*64,
                       order_kind='current_table_snapshot', representation='native_table')
    expected = hashlib.sha256(b'["tushare","etf_adjustment_factors","000001.SH","default","current_table_snapshot"]').hexdigest()
    assert value.group == expected
    later = replace(value, observed_at=value.observed_at+timedelta(microseconds=1),
                    content={'changed': True}, token='1'*64)
    assert later.group == value.group and later.order_ns == value.order_ns+1000
    for field, changed in [('source', 'tonghuashun'), ('dataset', 'etf_daily'),
                           ('subject', '000002.SH'), ('variant', 'other'),
                           ('order_kind', 'source_revision')]:
        assert replace(value, **{field: changed}).group != value.group
    assert replace(value, subject='000002.SH').representation_key == value.representation_key
    assert replace(value, variant='other').representation_key != value.representation_key


def test_semantic_cache_memory_is_bounded_and_invalid_tokens_stay_rejected():
    for index in range(4200):
        _input_group('tushare', 'etf_adjustment_factors', str(index), 'default', 'current_table_snapshot')
    for index in range(600):
        _representation('tushare', 'etf_adjustment_factors', str(index))
    assert _input_group.cache_info().currsize <= 4096
    assert _representation.cache_info().currsize <= 512
    for token in ('A'*64, '0'*63, '0'*65, '0'*63+'\n', ['0']*64, None):
        with pytest.raises(NativeInputError):
            LocalInput('tushare', 'etf_adjustment_factors', '000001.SH', 'default',
                       datetime(2026, 1, 1, tzinfo=timezone.utc), {}, token)
