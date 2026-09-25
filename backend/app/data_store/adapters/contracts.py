"""Small input/normalization protocol. Native evidence, never legacy work IDs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from decimal import Decimal
import hashlib
import json
from typing import Mapping
from uuid import UUID

from .canonical import NativeInputError


def native_json(value) -> str:
    """Match the collector's exact number encoding; reject floats at this boundary."""
    if isinstance(value, float):
        raise NativeInputError('SOURCE_PRECISION_INVALID', '本地输入包含二进制浮点值。')
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise NativeInputError('SOURCE_PRECISION_INVALID', '本地输入包含非有限数值。')
        return '-0.0' if str(value) == '-0' else str(value)
    if isinstance(value, dict):
        if any(type(k) is not str for k in value):
            raise NativeInputError('SOURCE_SCHEMA_INVALID', '本地输入字段名无效。')
        return '{' + ','.join(json.dumps(k, ensure_ascii=False)+':'+native_json(v)
                              for k,v in sorted(value.items())) + '}'
    if isinstance(value, (tuple, list)):
        return '[' + ','.join(native_json(v) for v in value) + ']'
    if isinstance(value, (datetime, date, UUID)):
        value = value.isoformat() if not isinstance(value, UUID) else str(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value) -> str:
    return hashlib.sha256(native_json(value).encode()).hexdigest()


def loads(value: str):
    def invalid(_):
        raise NativeInputError('SOURCE_PRECISION_INVALID', '本地输入包含非有限数值。')
    return json.loads(value, parse_float=Decimal, parse_constant=invalid)


def instant_ns(value: datetime | str) -> int:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise NativeInputError('SOURCE_ORDER_UNPROVEN', '来源缺少带时区的观察依据。')
    d = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    result = (d.days * 86400 + d.seconds) * 1_000_000_000 + d.microseconds * 1000
    if not -(2**63) <= result < 2**63:
        raise NativeInputError('SOURCE_ORDER_UNPROVEN', '来源时间超出纳秒范围。')
    return result


@dataclass(frozen=True)
class LocalInput:
    source: str
    dataset: str
    subject: str
    variant: str
    observed_at: datetime
    content: dict | list
    token: str
    order_kind: str = 'observation'
    # For source reconstructions, individual inherited rows retain their actual
    # confirmation. This is ephemeral input evidence, not a persisted row ledger.
    row_basis: Mapping[str, tuple[int, str]] = field(default_factory=dict)
    basis_field: str | None = None
    failure: str | None = None
    limitations: tuple[str, ...] = ()
    representation: str = 'ths_observation'
    # Adapter callers must prove explicit withdrawal; a missing response or
    # archive-delta removal is NOT such proof. Keys use the declared business codec.
    withdrawals: tuple[str, ...] = ()
    unconfirmed_keys: tuple[str, ...] = ()

    @property
    def order_ns(self) -> int:
        return instant_ns(self.observed_at)

    @property
    def group(self) -> str:
        return digest([self.source, self.dataset, self.subject, self.variant, self.order_kind])

    @property
    def representation_key(self) -> str:
        # Variant is a source-owned semantic scope. Never collapse different
        # adjustment/frequency/request semantics because their field names match.
        return self.source + ':' + digest([self.dataset, self.variant])[:24]

    def __post_init__(self):
        if (self.source not in ('tonghuashun', 'tushare', 'synthetic') or
                any(type(v) is not str or not v or len(v.encode()) > n for v,n in
                    ((self.dataset,80),(self.subject,256),(self.variant,128))) or
                self.order_kind not in ('observation','current_table_snapshot','source_revision') or
                len(self.token) != 64 or any(c not in '0123456789abcdef' for c in self.token)):
            raise NativeInputError('SOURCE_SCHEMA_INVALID', '本地输入身份无效。')
        self.order_ns
        if (not isinstance(self.withdrawals, tuple) or len(self.withdrawals)>10000 or
                any(type(k) is not str or not k or len(k.encode())>240 for k in self.withdrawals)):
            raise NativeInputError('SOURCE_SCHEMA_INVALID', '撤回必须具有有界的明确业务键。')


@dataclass(frozen=True)
class Unit:
    """One true point/report/current object, not one processing success record."""
    subject: str
    representation: str
    object_key: str
    group: str
    order: int
    token: str
    rows: tuple[dict, ...]
    complete: bool = True
    failure: str | None = None
    limitations: tuple[str, ...] = ()
    withdrawn: bool = False

    @property
    def key(self):
        return (self.representation, self.subject, self.object_key)
