"""Shared exact scalar validation; no account or execution state."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
ZERO = Decimal("0")

class DomainValidationError(ValueError):
    """Raised when a runtime domain object would violate an invariant."""

def _decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize supported numeric inputs while rejecting binary floats.

    Accepting a float would reintroduce the precision problem that the
    ``Decimal`` domain contract is intended to prevent.  Integers and strings
    are accepted for ergonomic construction of test fixtures and API adapters;
    all values are converted before they enter a state object.
    """

    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{field_name} must be Decimal, int, or str; float is unsupported")

    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainValidationError(f"{field_name} must be a valid decimal") from exc

    if not normalized.is_finite():
        raise DomainValidationError(f"{field_name} must be finite")
    return normalized

def _non_negative(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize a decimal value that cannot be negative."""

    normalized = _decimal(value, field_name)
    if normalized < ZERO:
        raise DomainValidationError(f"{field_name} must be non-negative")
    return normalized

def _positive(value: Decimal | int | str, field_name: str) -> Decimal:
    """Normalize a decimal value that must be strictly positive."""

    normalized = _decimal(value, field_name)
    if normalized <= ZERO:
        raise DomainValidationError(f"{field_name} must be positive")
    return normalized

def _aware_datetime(value: datetime, field_name: str) -> datetime:
    """Require an explicitly timezone-aware timestamp for auditability."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return value
