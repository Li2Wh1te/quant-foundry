"""Bounded canonical JSON used for accepted control state, never market rows."""
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


def exact(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("unsupported JSON value")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False, default=exact).encode()


def canonical_hash(value):
    return "sha256:" + hashlib.sha256(encoded(value)).hexdigest()
