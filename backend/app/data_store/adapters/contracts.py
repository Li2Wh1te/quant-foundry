"""Small input/normalization protocol. Native evidence, never legacy work IDs."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from datetime import datetime, timezone, date
from decimal import Decimal
import hashlib
import json
import re
from typing import Mapping, TYPE_CHECKING
from uuid import UUID

from .canonical import NativeInputError

if TYPE_CHECKING:
    from ..catalog_membership import CatalogMemberships

# The encoder holds only immutable formatting options, never a source payload.
# Reusing it avoids constructing a JSONEncoder for every scalar/key while
# preserving the collector's exact UTF-8 quoting and primitive representation.
_NATIVE_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
_NATIVE_SORTED_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                         sort_keys=True, separators=(',', ':'))
_JSON_SCALARS = frozenset((str, int, bool, type(None)))
_FLAT_SCALARS = _JSON_SCALARS | {Decimal}
_TOKEN = re.compile(r'[0-9a-f]{64}\Z')


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
        # The C encoder is byte-equivalent for a flat JSON scalar object. Never
        # send Decimal, float or nested values through this shortcut: their
        # exact-number validation still uses the ordinary recursive contract.
        if all(type(v) in _JSON_SCALARS for v in value.values()):
            return _NATIVE_SORTED_ENCODER.encode(value)
        if all(type(v) in _FLAT_SCALARS for v in value.values()):
            # Native NUMERIC must keep its exact literal (including exponent,
            # scale and negative zero). Encode contiguous primitive fields in
            # C, inserting Decimal literals with the SAME recursive validator;
            # no placeholder replacement or floating-point conversion is used.
            parts=[];plain={}
            for key,item in sorted(value.items()):
                if type(item) is Decimal:
                    if plain:
                        parts.append(_NATIVE_ENCODER.encode(plain)[1:-1]);plain={}
                    parts.append(_NATIVE_ENCODER.encode(key)+':'+native_json(item))
                else:plain[key]=item
            if plain:parts.append(_NATIVE_ENCODER.encode(plain)[1:-1])
            return '{'+','.join(parts)+'}'
        return '{' + ','.join(_NATIVE_ENCODER.encode(k)+':'+native_json(v)
                              for k,v in sorted(value.items())) + '}'
    if isinstance(value, (tuple, list)):
        if all(type(v) in _JSON_SCALARS for v in value):
            return _NATIVE_ENCODER.encode(value)
        return '[' + ','.join(native_json(v) for v in value) + ']'
    if isinstance(value, (datetime, date, UUID)):
        value = value.isoformat() if not isinstance(value, UUID) else str(value)
    return _NATIVE_ENCODER.encode(value)


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


@lru_cache(maxsize=4096)
def _input_group(source, dataset, subject, variant, order_kind):
    # The FULL semantic identity is part of this bounded ephemeral cache key.
    # Dates/values/tokens are not cached and no source confirmation is inferred.
    return digest([source, dataset, subject, variant, order_kind])


@lru_cache(maxsize=512)
def _representation(source, dataset, variant):
    # Representation remains source/variant-specific. The bound prevents an
    # unbounded provider identity set from retaining process memory indefinitely.
    return source + ':' + digest([dataset, variant])[:24]


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
    # A bounded native directory-head context is shared ephemerally by this
    # scan's inputs. It is neither business content nor a persisted source row.
    catalog_memberships: CatalogMemberships | None = field(default=None, compare=False, repr=False)

    @cached_property
    def order_ns(self) -> int:
        return instant_ns(self.observed_at)

    @property
    def group(self) -> str:
        return _input_group(self.source, self.dataset, self.subject, self.variant, self.order_kind)

    @property
    def representation_key(self) -> str:
        # Variant is a source-owned semantic scope. Never collapse different
        # adjustment/frequency/request semantics because their field names match.
        return _representation(self.source, self.dataset, self.variant)

    def __post_init__(self):
        if (self.source not in ('tonghuashun', 'tushare', 'synthetic') or
                any(type(v) is not str or not v or len(v.encode()) > n for v,n in
                    ((self.dataset,80),(self.subject,256),(self.variant,128))) or
                self.order_kind not in ('observation','current_table_snapshot','source_revision') or
                not isinstance(self.token, str) or not _TOKEN.fullmatch(self.token)):
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
    catalog_memberships: CatalogMemberships | None = field(default=None, compare=False, repr=False)

    @property
    def key(self):
        return (self.representation, self.subject, self.object_key)
