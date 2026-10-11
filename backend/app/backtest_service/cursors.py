"""Signed, owner/query-bound cursors. No client-selected result filesystem path."""
import base64
import hashlib
import hmac
import json
from .runtime_contracts import RuntimeFault
from .serialization import encoded


def encode(value, scope, key):
    payload = base64.urlsafe_b64encode(encoded(dict(value=value, scope=scope))).decode().rstrip("=")
    return payload + "." + hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def decode(cursor, scope, key):
    try:
        if type(cursor) is not str or len(cursor) > 8192:
            raise ValueError()
        payload, signature = cursor.split(".")
        expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload + "="*(-len(payload)%4)))
        if data["scope"] != scope:
            raise ValueError()
        return data["value"]
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise RuntimeFault("INVALID_CONTRACT", "游标无效或不属于本查询") from None
