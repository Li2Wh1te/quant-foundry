"""Domain-separated, bounded, signed resolution and key-cursor encoding.

Tokens contain references, never credentials. They establish content identity;
callers still authenticate and acquire the current issue guard on every read.
"""
import base64
import hashlib
import hmac
import json
import time
from app.data_foundation.canonical import FoundationError, encode


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


class ResolutionCodec:
    def __init__(self, secret, clock=time.time):
        self.key=hmac.digest(secret.encode(),b'quant-foundry/foundation-resolution/v1','sha256')
        self.clock=clock

    def sign(self, kind, value):
        payload=b64(encode({'kind':kind,'schema':1,**value}).encode())
        return payload+'.'+b64(hmac.digest(self.key,payload.encode(),'sha256'))

    def read(self, token, kind):
        try:
            if len(token)>32768:raise ValueError()
            payload,signature=token.split('.')
            expected=b64(hmac.digest(self.key,payload.encode(),'sha256'))
            if not hmac.compare_digest(signature,expected):raise ValueError()
            value=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
            if value.get('schema')!=1 or value.get('kind')!=kind:raise ValueError()
            if kind=='resolution' and self.clock()>=value['expires_at']:
                raise FoundationError('RESOLUTION_EXPIRED','检查已过期，请基于原正式版本重新检查。')
            return value
        except (ValueError,KeyError,TypeError) as exc:
            if isinstance(exc,FoundationError):raise
            raise FoundationError('INVALID_RESOLUTION','读取凭据无效或已随认证更新失效，请重新检查。') from None


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
