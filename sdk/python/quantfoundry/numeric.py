"""Input conversion only; all supported-range checks and arithmetic are Rust."""
from decimal import Decimal
from . import _native

DecimalLike = Decimal | int | str

def _range_error() -> _native.ContractError:
    error = _native.ContractError("NUMERIC_RANGE_UNSUPPORTED: 数值不能无损表示或超出受支持范围")
    error.code = "NUMERIC_RANGE_UNSUPPORTED"
    error.operation = "numeric"
    error.message = "数值不能无损表示或超出受支持范围"
    error.scope = {}
    return error

def _fixed_length(sign: int, digits: int, exponent: int) -> int:
    if exponent >= 0:
        return sign + digits + exponent
    return sign + (digits + 1 if digits + exponent > 0 else 2 - exponent)

def _text(value: DecimalLike) -> str:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise TypeError("Decimal input requires Decimal, int or decimal string")
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)  # Rust rejects NaN/Infinity using the common error.
        parts = value.as_tuple()
        if _fixed_length(parts.sign, len(parts.digits), parts.exponent) <= 128:
            return format(value, "f")
        # Avoid allocating billions of exponent-padding bytes. Shed only exact
        # trailing zeroes; Decimal.normalize() would use the caller's context.
        end = len(parts.digits)
        while end and parts.digits[end - 1] == 0:
            end -= 1
        if not end:
            return "0"
        exponent = parts.exponent + len(parts.digits) - end
        if _fixed_length(parts.sign, end, exponent) > 128:
            raise _range_error()
        digits = "".join(str(digit) for digit in parts.digits[:end])
        point = end + exponent
        if exponent >= 0:
            text = digits + "0" * exponent
        elif point > 0:
            text = digits[:point] + "." + digits[point:]
        else:
            text = "0." + "0" * -point + digits
        return ("-" if parts.sign else "") + text
    if isinstance(value, int) and value.bit_length() > 426:
        raise _range_error()  # Cannot fit even the bounded decimal input text.
    return str(value)

def decimal_input(value: DecimalLike) -> Decimal:
    return Decimal(_native.parse_decimal(_text(value)))

def price_input(value: DecimalLike) -> Decimal:
    return Decimal(_native.parse_price(_text(value)))

def nanoseconds(value: int | str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("nanoseconds require int or decimal string")
    return int(_native.roundtrip_ns(str(value)))
