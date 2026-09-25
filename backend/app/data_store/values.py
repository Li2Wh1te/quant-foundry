"""Exact values and bounded current-control encoding; no legacy dependencies.

Decimal conversion rejects rounding and widths unsupported by DuckDB DECIMAL.
Nanosecond identity stays in integer space. Control JSON is for SMALL current
metadata, not for storing successful business rows or per-row process ledgers.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from .errors import DataStoreError

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


def exact_decimal(value: Decimal | int, *, precision: int, scale: int) -> Decimal:
    """Return an exact fixed-scale Decimal, independently of decimal.Context.

    Parsing source text is an adapter responsibility. In particular float and
    bool inputs are rejected rather than silently converted to monetary data.
    """
    if (type(precision) is not int or type(scale) is not int
            or not 1 <= precision <= 38 or not 0 <= scale <= precision):
        raise DataStoreError('INVALID_CONFIGURATION')
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DataStoreError('INVALID_VALUE')
    sign, digits, exponent = value.as_tuple()
    if not value:
        return Decimal((0, (0,), -scale))
    shift = exponent + scale
    if shift < 0:
        remove = -shift
        if remove >= len(digits) or any(digits[-remove:]):
            raise DataStoreError('INVALID_VALUE')
        digits = digits[:-remove]
    else:
        # Check before allocating zeros: hostile exponents remain bounded.
        if len(digits) + shift > precision:
            raise DataStoreError('INVALID_VALUE')
        digits += (0,) * shift
    if len(digits) > precision:
        raise DataStoreError('INVALID_VALUE')
    return Decimal((sign, digits, -scale))


def decimal_text(value: Decimal, *, max_chars: int = 256) -> str:
    """Canonical plain decimal text without context rounding or float."""
    if type(max_chars) is not int or max_chars < 1:
        raise DataStoreError('INVALID_CONFIGURATION')
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DataStoreError('INVALID_VALUE')
    if not value:
        return '0'
    sign, digits, exponent = value.as_tuple()
    # Remove trailing coefficient zeros without normalize(), which can round.
    end = len(digits)
    while end > 1 and digits[end - 1] == 0:
        end -= 1
        exponent += 1
    digits = digits[:end]
    size = sign + (len(digits) + exponent if exponent >= 0 else
                   max(len(digits) + 1, 2 - exponent))
    if size > max_chars:
        raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
    text = format(Decimal((sign, digits, exponent)), 'f')
    return text


def epoch_ns(value: datetime) -> int:
    """Convert an aware datetime exactly (its precision is microseconds)."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DataStoreError('INVALID_VALUE')
    delta = value.astimezone(timezone.utc) - EPOCH
    return checked_ns(((delta.days * 86400 + delta.seconds) * 1_000_000
                       + delta.microseconds) * 1000)


def checked_ns(value: int) -> int:
    """Validate an already-nanosecond timestamp; never round through seconds."""
    if type(value) is not int or not INT64_MIN <= value <= INT64_MAX:
        raise DataStoreError('INVALID_VALUE')
    return value


def control_json(value: object, *, max_bytes: int = 65_536,
                 max_nodes: int = 4096, max_depth: int = 16) -> str:
    """Encode bounded current metadata with exact Decimal strings.

    Limits apply to the escaped UTF-8 result, nesting and all values/keys. The
    same encoding is independent of mapping insertion order. No custom object
    serializer is invoked. Repeated/cyclic containers exhaust the same budget.
    """
    if any(type(v) is not int or v < 1 for v in (max_bytes, max_nodes, max_depth)):
        raise DataStoreError('INVALID_CONFIGURATION')
    nodes = 0

    def visit(item: object, depth: int):
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
        if item is None or type(item) is bool:
            return item
        if type(item) is int:
            if item.bit_length() > 420:
                raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
            return item
        if isinstance(item, Decimal):
            item = decimal_text(item, max_chars=min(256, max_bytes))
        elif isinstance(item, datetime):
            if item.utcoffset() is None:
                raise DataStoreError('INVALID_VALUE')
            item = item.astimezone(timezone.utc).isoformat()
        elif isinstance(item, (date, UUID)):
            item = str(item)
        if type(item) is str:
            try:
                if len(item) > max_bytes or len(item.encode('utf-8')) > max_bytes:
                    raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
            except UnicodeError:
                raise DataStoreError('INVALID_VALUE') from None
            return item
        if type(item) is dict:
            if len(item) > max_nodes - nodes:
                raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
            if any(type(key) is not str for key in item):
                raise DataStoreError('INVALID_VALUE')
            return {visit(key, depth + 1): visit(item[key], depth + 1)
                    for key in sorted(item)}
        if type(item) in (list, tuple):
            if len(item) > max_nodes - nodes:
                raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
            return [visit(child, depth + 1) for child in item]
        raise DataStoreError('INVALID_VALUE')

    normalized = visit(value, 0)
    chunks, size = [], 0
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                               separators=(',', ':'), allow_nan=False)
    for chunk in encoder.iterencode(normalized):
        size += len(chunk.encode('utf-8'))
        if size > max_bytes:
            raise DataStoreError('CONTROL_BUDGET_EXCEEDED')
        chunks.append(chunk)
    return ''.join(chunks)
