"""LF-01 precision and bounded metadata primitives (not pipeline acceptance)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_UP
from uuid import UUID

import pytest

from app.data_store.errors import DataStoreError
from app.data_store.values import (checked_ns, control_json, decimal_text,
                                   epoch_ns, exact_decimal)


def code(expected, function, *args, **kwargs):
    with pytest.raises(DataStoreError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == expected


@pytest.mark.parametrize('text,precision,scale,expected', [
    ('1.2300', 4, 2, '1.23'), ('-9.99', 3, 2, '-9.99'),
    ('1000', 4, 0, '1000'), ('0.0001', 4, 4, '0.0001'),
    ('-0E-999999999', 38, 12, '0.000000000000'),
    ('12345678901234567890.123456789012345678', 38, 18,
     '12345678901234567890.123456789012345678'),
])
def test_decimal_is_exact_under_low_precision_context(text, precision, scale, expected):
    with localcontext() as context:
        context.prec, context.rounding = 2, ROUND_UP
        actual = exact_decimal(Decimal(text), precision=precision, scale=scale)
        assert format(actual, 'f') == expected
        assert actual.as_tuple().exponent == -scale


@pytest.mark.parametrize('value,precision,scale', [
    (1.25, 4, 2), (True, 4, 2), ('1.25', 4, 2),
    (Decimal('NaN'), 38, 12), (Decimal('sNaN'), 38, 12),
    (Decimal('Infinity'), 38, 12), (Decimal('1.001'), 3, 2),
    (Decimal('100.0'), 3, 1), (Decimal('1E+999999999'), 38, 0),
    (Decimal('1E-999999999'), 38, 12),
])
def test_decimal_rejects_rounding_non_finite_and_float(value, precision, scale):
    code('INVALID_VALUE', exact_decimal, value, precision=precision, scale=scale)


@pytest.mark.parametrize('precision,scale', [(39, 1), (0, 0), (3, 4), (3, -1), (True, 0), (3, 1.0)])
def test_decimal_contract_rejects_unsupported_widths(precision, scale):
    code('INVALID_CONFIGURATION', exact_decimal, Decimal('1'), precision=precision, scale=scale)


@pytest.mark.parametrize('text,expected', [
    ('-0.000', '0'), ('1.2300', '1.23'), ('100000', '100000'),
    ('-0.0000100', '-0.00001'), ('1E+3', '1000'),
    ('123456789012345678901234567890.123456789', '123456789012345678901234567890.123456789'),
])
def test_decimal_text_never_uses_context_rounding(text, expected):
    with localcontext() as ctx:
        ctx.prec = 2
        assert decimal_text(Decimal(text)) == expected


@pytest.mark.parametrize('text', ['1E+999999999', '1E-999999999'])
def test_extreme_exponents_do_not_allocate_unbounded_strings(text):
    code('CONTROL_BUDGET_EXCEEDED', decimal_text, Decimal(text))


def test_integer_and_time_precision():
    assert exact_decimal(100, precision=5, scale=2) == Decimal('100.00')
    east = timezone(timedelta(hours=8))
    assert epoch_ns(datetime(1970, 1, 1, 8, tzinfo=east)) == 0
    assert epoch_ns(datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)) == -1000
    assert checked_ns(1_790_288_000_123_456_789) == 1_790_288_000_123_456_789
    assert checked_ns(2**63 - 1) == 2**63 - 1
    assert checked_ns(-(2**63)) == -(2**63)


@pytest.mark.parametrize('value', [0.5, True, 2**63, -(2**63)-1, '123'])
def test_nanosecond_rejection(value):
    code('INVALID_VALUE', checked_ns, value)


def test_datetime_requires_aware_and_representable_time():
    code('INVALID_VALUE', epoch_ns, datetime(2026, 1, 1))
    code('INVALID_VALUE', epoch_ns, datetime(1, 1, 1, tzinfo=timezone.utc))


def test_control_encoding_is_stable_and_exact():
    first = dict(z=Decimal('12345678901234567890.123456789'), a=False, empty=None)
    assert control_json(first) == control_json(dict(reversed(list(first.items()))))
    assert '"12345678901234567890.123456789"' in control_json(first)
    assert control_json([UUID(int=0), date(2026, 9, 25)]) == (
        '["00000000-0000-0000-0000-000000000000","2026-09-25"]')
    assert control_json(datetime(1970, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))) == (
        '"1970-01-01T00:00:00+00:00"')


@pytest.mark.parametrize('value', [float('nan'), 0.1, {1: 'bad'}, object(), '\ud800'])
def test_control_refuses_unsafe_types(value):
    code('INVALID_VALUE', control_json, value)


def test_control_utf8_escaped_and_node_budgets():
    assert control_json('汉', max_bytes=5) == '"汉"'
    code('CONTROL_BUDGET_EXCEEDED', control_json, '汉', max_bytes=4)
    code('CONTROL_BUDGET_EXCEEDED', control_json, '\n'*5, max_bytes=10)
    code('CONTROL_BUDGET_EXCEEDED', control_json, [0]*10, max_nodes=10)
    code('CONTROL_BUDGET_EXCEEDED', control_json, {'a': 1}, max_nodes=2)
    code('CONTROL_BUDGET_EXCEEDED', control_json, 1 << 500)
    cycle = []; cycle.append(cycle)
    code('CONTROL_BUDGET_EXCEEDED', control_json, cycle)
    code('CONTROL_BUDGET_EXCEEDED', control_json, [[[1]]], max_depth=2)
    code('INVALID_CONFIGURATION', control_json, {}, max_bytes=True)


def test_unknown_error_messages_cannot_leak_payloads():
    with pytest.raises(ValueError, match='Unknown data-store error code'):
        DataStoreError('private-connection-secret')


def test_explicit_arrow_schema_parquet_round_trip(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    original = Decimal('12345678901234567890.123456789012345678')
    value = exact_decimal(original, precision=38, scale=18)
    stamp = checked_ns(1_790_288_000_123_456_789)
    schema = pa.schema([pa.field('price', pa.decimal128(38, 18), nullable=False),
                        pa.field('event_time_ns', pa.int64(), nullable=False)])
    table = pa.Table.from_pylist([{'price': value, 'event_time_ns': stamp}], schema=schema)
    path = tmp_path / 'precision.parquet'
    pq.write_table(table, path, compression='zstd')
    assert pq.read_table(path).to_pylist() == [{'price': original, 'event_time_ns': stamp}]
