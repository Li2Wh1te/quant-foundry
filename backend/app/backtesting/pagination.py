"""Stable opaque-cursor pagination for backtest result lists.

Long result ranges are read exclusively through keyset pagination backed by
an opaque JSON cursor token.  The token carries:

- ``version``: cursor format version;
- ``query_digest``: SHA-256 over the canonicalized query (run id, every
  filter, sort direction/keys, page-size policy, and the result upper
  bound captured for running runs);
- ``last_sort_key``: the sort-key tuple of the last row on the previous page;
- ``query_upper_bound``: the maximum sort-key tuple visible when the first
  page was created, so appended rows never leak into an existing cursor.

Tokens are canonical JSON encoded as unpadded base64url and signed with a
server-side HMAC-SHA256 (``<payload>.<signature>``).  The signature covers
every payload field, including ``last_sort_key`` and ``query_upper_bound``,
so a token edited into another syntactically valid shape is still rejected.
The ``query_digest`` itself covers the canonicalized query plus the encoded
upper bound, binding the cursor to one exact server-side query state.
Clients must treat tokens as opaque; any tampering, version mismatch,
format error, or query mismatch raises :class:`CursorError` so callers can
return a clear parameter error instead of silently restarting from the
first page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from uuid import UUID


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
CURSOR_VERSION = 1

# Supported sort-element kinds.  Each maps to one deterministic wire
# representation and back, preserving ordering within its kind.


class CursorError(ValueError):
    """Base class for every rejected cursor condition."""


class MalformedCursorError(CursorError):
    """The token is not decodable base64url JSON with the expected shape."""


class UnsupportedCursorVersionError(CursorError):
    """The token was written by an incompatible cursor format version."""


class CursorQueryMismatchError(CursorError):
    """The token belongs to a different query than the current request."""


@dataclass(frozen=True, slots=True)
class CursorPage:
    """Uniform envelope for every long-range result list."""

    items: tuple[Any, ...]
    next_cursor: str | None
    has_more: bool


def normalize_limit(value: int) -> int:
    """Validate the requested page size against the first-version policy."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer between 1 and 500")
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    return value


def canonical_query_payload(payload: Mapping[str, Any]) -> str:
    """Serialize a query description canonically for digest computation."""

    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("query payload must be canonically serializable") from exc


def compute_query_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of the canonicalized query payload."""

    return hashlib.sha256(canonical_query_payload(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sort-key element encoding
# ---------------------------------------------------------------------------


def _encode_element(kind: str, value: Any) -> dict[str, Any]:
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError("sort element declared as str")
        return {"k": kind, "v": value}
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("sort element declared as int")
        return {"k": kind, "v": value}
    if kind == "uuid":
        if not isinstance(value, UUID):
            raise ValueError("sort element declared as uuid")
        return {"k": kind, "v": str(value)}
    if kind == "ts":
        if not isinstance(value, datetime):
            raise ValueError("sort element declared as ts")
        # Normalize to UTC ISO-8601 so identical instants always produce
        # identical tokens regardless of the source timezone.
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sort timestamps must be timezone-aware")
        return {"k": kind, "v": value.astimezone(timezone.utc).isoformat()}
    if kind == "dec":
        if isinstance(value, float):
            raise ValueError("sort elements never carry binary floats")
        if not isinstance(value, Decimal):
            try:
                value = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError("sort element declared as dec") from exc
        return {"k": kind, "v": str(value)}
    raise ValueError(f"unsupported sort element kind: {kind!r}")


def _decode_element(element: Any, kind: str, label: str) -> Any:
    if not isinstance(element, dict):
        raise MalformedCursorError(f"{label} must contain tagged elements")
    element_kind = element.get("k")
    raw_value = element.get("v")
    if element_kind != kind:
        raise MalformedCursorError(
            f"{label} element kind {element_kind!r} does not match expected {kind!r}"
        )
    try:
        if kind == "str":
            if not isinstance(raw_value, str):
                raise ValueError
            return raw_value
        if kind == "int":
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise ValueError
            return raw_value
        if kind == "uuid":
            if not isinstance(raw_value, str):
                raise ValueError
            return UUID(raw_value)
        if kind == "ts":
            if not isinstance(raw_value, str):
                raise ValueError
            parsed = datetime.fromisoformat(raw_value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError
            return parsed
        if kind == "dec":
            if not isinstance(raw_value, str):
                raise ValueError
            return Decimal(raw_value)
    except (ValueError, InvalidOperation) as exc:
        raise MalformedCursorError(f"{label} contains an invalid {kind} value") from exc
    raise MalformedCursorError(f"unsupported sort element kind: {kind!r}")


# ---------------------------------------------------------------------------
# Token building and parsing
# ---------------------------------------------------------------------------


def encode_sort_element(kind: str, value: Any) -> dict[str, Any]:
    """Public wrapper so callers can canonicalize bound values for digests."""

    return _encode_element(kind, value)


@dataclass(frozen=True, slots=True)
class ParsedCursor:
    """Decoded cursor payload with typed sort-key values."""

    version: int
    query_digest: str
    last_sort_key: tuple[Any, ...]
    query_upper_bound: Mapping[str, Any]


def build_cursor(
    *,
    signing_key: str,
    query_digest: str,
    key_kinds: Sequence[str],
    last_sort_key: Sequence[Any],
    upper_bound_columns: Mapping[str, str],
    query_upper_bound: Mapping[str, Any],
) -> str:
    """Encode a signed cursor token from typed sort-key values.

    ``upper_bound_columns`` maps each bound column to its element kind.
    Raises :class:`ValueError` when values do not match their declared
    kinds; callers should treat that as an internal programming error
    because both sides come from the same result-kind specification.
    """

    key = _require_signing_key(signing_key)
    if len(key_kinds) != len(last_sort_key):
        raise ValueError("last_sort_key length must match key_kinds")
    if set(upper_bound_columns) != set(query_upper_bound):
        raise ValueError("query_upper_bound keys must match upper_bound_columns")
    payload = {
        "version": CURSOR_VERSION,
        "query_digest": query_digest,
        "last_sort_key": [
            encode_sort_element(kind, value)
            for kind, value in zip(key_kinds, last_sort_key)
        ],
        "query_upper_bound": {
            column: encode_sort_element(upper_bound_columns[column], query_upper_bound[column])
            for column in sorted(query_upper_bound)
        },
    }
    return sign_token(payload, key)


def _encode_token(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return urlsafe_b64encode(canonical.encode("utf-8")).decode("ascii").rstrip("=")


def _require_signing_key(signing_key: str | None) -> str:
    if not isinstance(signing_key, str) or not signing_key.strip():
        raise ValueError("cursor signing key must be non-blank text")
    return signing_key


def sign_token(payload: dict[str, Any], signing_key: str) -> str:
    """Encode the payload and append a server-side HMAC-SHA256 signature.

    The signature covers the exact base64url payload bytes, so any change to
    any field (including ``last_sort_key`` and ``query_upper_bound``)
    invalidates the token.
    """

    encoded = _encode_token(payload)
    mac = hmac.new(
        signing_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{mac}"


# Tokens are base64url payloads plus a hex signature joined by a dot; any
# other character (including non-ASCII) is malformed and must map to
# MalformedCursorError instead of leaking codec errors to clients.
_TOKEN_CHARSET = re.compile(r"[A-Za-z0-9_\-.]+")


def _split_signed_token(token: str) -> tuple[str, str]:
    """Split a token into its raw (payload-b64, signature) parts.

    No decoding happens here: the caller must verify the MAC over the raw
    payload bytes before interpreting any content.
    """

    if not isinstance(token, str) or not token or len(token) > 8192:
        raise MalformedCursorError("cursor must be a non-empty short token")
    if _TOKEN_CHARSET.fullmatch(token) is None:
        raise MalformedCursorError("cursor contains unsupported characters")
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise MalformedCursorError("cursor is missing its signature")
    return parts[0], parts[1]


def _decode_signed_payload(encoded_payload: str) -> dict[str, Any]:
    padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
    try:
        raw = urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MalformedCursorError("cursor is not valid base64url JSON") from exc
    if not isinstance(payload, dict):
        raise MalformedCursorError("cursor payload must be a JSON object")
    return payload


def parse_cursor(
    token: str,
    *,
    signing_key: str,
    expected_query_digest: str | None = None,
    key_kinds: Sequence[str] | None = None,
    upper_bound_columns: Mapping[str, str] | None = None,
) -> ParsedCursor:
    """Decode and fully validate a cursor token.

    The server-side MAC is verified over the raw payload bytes BEFORE the
    content is decoded or interpreted; tokens without a valid signature are
    rejected outright.  ``key_kinds`` declares the positional sort-key kinds
    of the result type; ``upper_bound_columns`` declares the expected
    upper-bound columns and their kinds.  When provided, structural
    mismatches are rejected.
    """

    key = _require_signing_key(signing_key)
    encoded_payload, signature = _split_signed_token(token)
    recomputed = hmac.new(
        key.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac_compare(signature, recomputed):
        raise MalformedCursorError("cursor signature verification failed")

    # The signature is valid; only now is the content decoded and trusted.
    payload = _decode_signed_payload(encoded_payload)

    version = payload.get("version")
    if version != CURSOR_VERSION:
        raise UnsupportedCursorVersionError(
            f"cursor version {version!r} is unsupported; expected {CURSOR_VERSION}"
        )
    query_digest = payload.get("query_digest")
    if not isinstance(query_digest, str) or not query_digest:
        raise MalformedCursorError("cursor query_digest must be non-empty text")
    if (
        expected_query_digest is not None
        and not hmac_compare(query_digest, expected_query_digest)
    ):
        raise CursorQueryMismatchError(
            "cursor belongs to a different query; restart from the first page"
        )

    raw_last_key = payload.get("last_sort_key")
    if not isinstance(raw_last_key, list):
        raise MalformedCursorError("cursor last_sort_key must be a list")
    if key_kinds is not None and len(raw_last_key) != len(key_kinds):
        raise MalformedCursorError("cursor last_sort_key has an unexpected length")
    last_sort_key = tuple(
        _decode_element(element, kind, "last_sort_key")
        for element, kind in zip(
            raw_last_key,
            key_kinds if key_kinds is not None else ("str",) * len(raw_last_key),
        )
    )

    raw_bound = payload.get("query_upper_bound")
    if not isinstance(raw_bound, dict):
        raise MalformedCursorError("cursor query_upper_bound must be an object")
    if upper_bound_columns is not None:
        if set(raw_bound) != set(upper_bound_columns):
            raise MalformedCursorError(
                "cursor query_upper_bound columns do not match this result type"
            )
        decoded_bound = {
            column: _decode_element(raw_bound[column], kind, f"query_upper_bound[{column}]")
            for column, kind in upper_bound_columns.items()
        }
    else:
        decoded_bound = dict(raw_bound)

    return ParsedCursor(
        version=payload["version"],
        query_digest=query_digest,
        last_sort_key=last_sort_key,
        query_upper_bound=decoded_bound,
    )


def hmac_compare(left: str, right: str) -> bool:
    """Constant-time string comparison for digest equality checks."""

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


__all__ = [
    "CURSOR_VERSION",
    "CursorError",
    "CursorPage",
    "CursorQueryMismatchError",
    "DEFAULT_PAGE_SIZE",
    "MalformedCursorError",
    "MAX_PAGE_SIZE",
    "ParsedCursor",
    "UnsupportedCursorVersionError",
    "build_cursor",
    "canonical_query_payload",
    "compute_query_digest",
    "encode_sort_element",
    "hmac_compare",
    "normalize_limit",
    "parse_cursor",
    "sign_token",
]
