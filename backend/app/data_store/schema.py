"""Small static dataset contracts, exact keys and the internal JSON boundary.

Adapters own units, source order and completeness. The store never guesses
those from column names. A report's key prefix must identify its whole object;
all its members live in one declared partition (possibly several shards).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import base64
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping, Callable

import pyarrow as pa
import pyarrow.compute as pc

from .errors import DataStoreError
from .values import control_json, decimal_text, exact_decimal

_NAME = re.compile(r'[a-z][a-z0-9_]{0,62}\Z')
_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z')
_EPOCH_DATE = date(1970, 1, 1)


def identifier(value: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise DataStoreError('INVALID_VALUE')
    return value


def fingerprint(value: object) -> str:
    return hashlib.sha256(control_json(value).encode()).hexdigest()


def supported_type(t: pa.DataType) -> bool:
    return (t in (pa.string(), pa.bool_(), pa.int32(), pa.int64(), pa.uint64(), pa.date32())
            or (pa.types.is_decimal128(t) and 0 <= t.scale <= t.precision <= 38)
            or (pa.types.is_timestamp(t) and t.unit == 'us' and t.tz == 'UTC'))


@dataclass(frozen=True)
class DatasetSpec:
    """Code-owned contract, not an end-user schema or transformation DSL.

    Nanosecond event identity MUST use int64 with `unit=epoch_ns` field metadata:
    DuckDB TIMESTAMPTZ uses microseconds. Decimal256, floats, unknown units and
    implicit timestamp conversion are not silently accepted. report_prefix is
    a count of leading business-key fields, not a separate report ledger.
    """
    name: str
    schema: pa.Schema
    key: tuple[str, ...]
    rule: str
    semantics: Mapping[str, str] = field(default_factory=dict)
    report_prefix: int = 0
    partitioning: str = 'single'
    partitioner: Callable[[tuple], str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        identifier(self.name)
        if not self.name[0].islower():
            raise DataStoreError('INVALID_CONFIGURATION')
        identifier(self.rule)
        identifier(self.partitioning)
        if (not isinstance(self.schema, pa.Schema) or not 1 <= len(self.schema) <= 128
                or len(set(self.schema.names)) != len(self.schema)
                or not self.key or len(set(self.key)) != len(self.key)
                or any(not _NAME.fullmatch(n) for n in self.schema.names)
                or any(n not in self.schema.names for n in self.key)
                or type(self.report_prefix) is not int
                or not 0 <= self.report_prefix < len(self.key)):
            raise DataStoreError('INVALID_CONFIGURATION')
        for f in self.schema:
            if pa.types.is_string(f.type):
                self.string_limit(f)
        if any(not supported_type(f.type) for f in self.schema):
            raise DataStoreError('SCHEMA_UNSUPPORTED')
        if any(self.schema.field(k).nullable for k in self.key):
            raise DataStoreError('INVALID_CONFIGURATION')
        if any(type(k) is not str or type(v) is not str for k, v in self.semantics.items()):
            raise DataStoreError('INVALID_CONFIGURATION')
        object.__setattr__(self, 'key', tuple(self.key))
        object.__setattr__(self, 'semantics', MappingProxyType(dict(self.semantics)))
        control_json(self.descriptor())

    def descriptor(self) -> dict:
        return {'name': self.name, 'arrow': base64.b64encode(self.schema.serialize()).decode(),
                'key': list(self.key), 'rule': self.rule, 'semantics': dict(self.semantics),
                'report_prefix': self.report_prefix, 'partitioning': self.partitioning}

    @classmethod
    def from_descriptor(cls, descriptor: dict) -> DatasetSpec:
        try:
            control_json(descriptor)
            return cls(descriptor['name'], pa.ipc.read_schema(pa.BufferReader(
                base64.b64decode(descriptor['arrow'], validate=True))),
                tuple(descriptor['key']), descriptor['rule'], descriptor['semantics'],
                descriptor['report_prefix'], descriptor.get('partitioning', 'single'))
        except (KeyError, TypeError, ValueError, pa.ArrowException):
            raise DataStoreError('REBUILD_REQUIRED') from None

    @property
    def schema_id(self) -> str:
        d = self.descriptor()
        del d['rule']
        return fingerprint(d)

    def accepts_schema(self, actual: pa.Schema) -> bool:
        """Only nullable additions are compatible; units/metadata never change."""
        if actual.metadata != self.schema.metadata:
            return False
        for f in actual:
            if f.name not in self.schema.names or not f.equals(self.schema.field(f.name),
                                                               check_metadata=True):
                return False
        return all(f.name in actual.names or (f.nullable and f.name not in self.key)
                   for f in self.schema)

    def accepts(self, old: DatasetSpec) -> bool:
        return (self.name == old.name and self.key == old.key and self.semantics == old.semantics
                and self.report_prefix == old.report_prefix and self.partitioning == old.partitioning
                and self.rule == old.rule and self.accepts_schema(old.schema))

    def check_partition(self, batch: pa.RecordBatch, partition: str):
        # A code-owned deterministic router prevents the same logical key being
        # inserted into two current partitions. Single-partition is the safe default.
        if self.partitioning == 'single':
            if partition != 'default':
                raise DataStoreError('INVALID_VALUE')
            return
        if self.partitioner is None:
            raise DataStoreError('INVALID_CONFIGURATION')
        columns = [batch.column(batch.schema.get_field_index(k)).to_pylist() for k in self.key]
        if any(self.partitioner(tuple(k)) != partition for k in zip(*columns)):
            raise DataStoreError('KEY_ORDER_INVALID')

    @staticmethod
    def string_limit(field: pa.Field) -> int:
        raw = (field.metadata or {}).get(b'max_utf8_bytes', b'65536')
        try:
            value = int(raw)
            if not 1 <= value <= 65536 or str(value).encode('ascii') != raw:
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise DataStoreError('INVALID_CONFIGURATION') from None

    def bounded_rows(self, requested: int, byte_limit: int) -> int:
        """Bound Arrow materialization BEFORE decoding variable-width columns.

        Native DuckDB memory limits do not cover caller-owned Arrow buffers.
        String bounds are explicit contract metadata, never inferred from a
        sample/average. Adapters can declare small symbol/code widths; an
        undeclared string is conservatively allowed at most 64 KiB per value.
        """
        per_row = 8 * len(self.schema)  # offsets/null bitmap/alignment allowance
        for f in self.schema:
            per_row += self.string_limit(f) if pa.types.is_string(f.type) else 16
        return max(1, min(requested, byte_limit // max(1, per_row)))

    def validate_batch(self, batch: pa.RecordBatch, *, max_rows: int, max_bytes: int) -> list[bytes]:
        if (not isinstance(batch, pa.RecordBatch)
                or not batch.schema.equals(self.schema, check_metadata=True)):
            raise DataStoreError('REBUILD_REQUIRED')
        if batch.num_rows > max_rows or batch.nbytes > max_bytes:
            raise DataStoreError('BATCH_BUDGET_EXCEEDED')
        try:
            batch.validate(full=True)
            for f, column in zip(self.schema, batch.columns):
                if not f.nullable and column.null_count:
                    raise DataStoreError('INVALID_VALUE')
                if pa.types.is_string(f.type) and batch.num_rows:
                    longest = pc.max(pc.binary_length(column)).as_py()
                    if longest is not None and longest > self.string_limit(f):
                        raise DataStoreError('BATCH_BUDGET_EXCEEDED')
            columns = [batch.column(batch.schema.get_field_index(k)).to_pylist() for k in self.key]
            keys = [self.key_bytes(v) for v in zip(*columns)]
            if any(a >= b for a, b in zip(keys, keys[1:])):
                raise DataStoreError('KEY_ORDER_INVALID')
            return keys
        except (pa.ArrowException, OverflowError, UnicodeError):
            raise DataStoreError('INVALID_VALUE') from None

    def key_bytes(self, values: tuple) -> bytes:
        if len(values) > len(self.key) or not values:
            raise DataStoreError('INVALID_VALUE')
        result = b''.join(_ordered(v, self.schema.field(k).type)
                          for k, v in zip(self.key, values))
        if len(result) > 2048:
            raise DataStoreError('INVALID_VALUE')
        return result


def _ordered(value, t: pa.DataType) -> bytes:
    """Order preserving, prefix-free encoding; used by the PG range index."""
    if value is None:
        raise DataStoreError('INVALID_VALUE')
    if pa.types.is_string(t):
        if type(value) is not str:
            raise DataStoreError('INVALID_VALUE')
        return value.encode('utf-8').replace(b'\x00', b'\x00\xff') + b'\x00\x00'
    if pa.types.is_boolean(t):
        if type(value) is not bool:
            raise DataStoreError('INVALID_VALUE')
        return bytes([int(value)])
    if pa.types.is_date32(t):
        if type(value) is not date:
            raise DataStoreError('INVALID_VALUE')
        value = (value - _EPOCH_DATE).days
    elif pa.types.is_timestamp(t):
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise DataStoreError('INVALID_VALUE')
        delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        value = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    elif pa.types.is_decimal(t):
        value = exact_decimal(value, precision=t.precision, scale=t.scale)
        sign, digits, _ = value.as_tuple()
        coefficient = int(''.join(map(str, digits))) * (-1 if sign else 1)
        return (coefficient + 10**38).to_bytes(17, 'big')
    if type(value) is not int:
        raise DataStoreError('INVALID_VALUE')
    try:
        return (value + (0 if t == pa.uint64() else 2**63)).to_bytes(8, 'big')
    except OverflowError:
        raise DataStoreError('INVALID_VALUE') from None


def prefix_end(prefix: bytes) -> bytes:
    for i in range(len(prefix) - 1, -1, -1):
        if prefix[i] < 255:
            return prefix[:i] + bytes([prefix[i] + 1])
    raise DataStoreError('INVALID_VALUE')


def api_value(value):
    """Exact transport values for D04; no public route is registered by D01."""
    if isinstance(value, Decimal):
        return decimal_text(value)
    if type(value) is int and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise DataStoreError('INVALID_VALUE')
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None or type(value) in (int, str, bool):
        return value
    raise DataStoreError('INVALID_VALUE')
