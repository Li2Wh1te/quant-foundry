"""Input conversion only; all supported-range checks and arithmetic are Rust."""
from decimal import Decimal
from . import _native

DecimalLike = Decimal | int | str

def _text(value: DecimalLike) -> str:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise TypeError("Decimal input requires Decimal, int or decimal string")
    return format(value, "f") if isinstance(value, Decimal) else str(value)

def decimal_input(value: DecimalLike) -> Decimal:
    return Decimal(_native.parse_decimal(_text(value)))

def price_input(value: DecimalLike) -> Decimal:
    return Decimal(_native.parse_price(_text(value)))

def nanoseconds(value: int | str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("nanoseconds require int or decimal string")
    return int(_native.roundtrip_ns(str(value)))
