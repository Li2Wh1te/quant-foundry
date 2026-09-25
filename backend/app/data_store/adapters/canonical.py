"""Versioned canonical encoding; Decimal values never pass through float."""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from uuid import UUID


class NativeInputError(ValueError):
    """Stable machine code with a safe operator-facing Chinese explanation."""
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def normalized(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal")
        # Decimal.normalize() can round under the current context. Formatting
        # the original coefficient preserves all digits, including large values.
        result = format(value, 'f')
        return (result.rstrip('0').rstrip('.') if '.' in result else result) if value else '0'
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Naive datetime")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (date, UUID)):
        return str(value)
    if isinstance(value, float):
        raise ValueError("Binary floating point is not a canonical foundation value")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Canonical keys must be strings")
        return {key: normalized(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(type(value).__name__)


def encode(value) -> str:
    return json.dumps(normalized(value), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(domain: str, value) -> str:
    return hashlib.sha256(('foundation-canonical-v1\0' + domain + '\0' + encode(value)).encode()).hexdigest()
