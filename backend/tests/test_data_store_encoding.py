"""Existing source/canonical bytes remain stable under encoder reuse."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum
from uuid import UUID

import pytest

from app.data_store.adapters.canonical import NativeInputError, encode
from app.data_store.adapters.contracts import native_json


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
