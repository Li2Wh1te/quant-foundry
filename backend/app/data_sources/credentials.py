"""Encrypt credential envelopes with a deployment-owned, independent key."""

import base64
import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.data_sources.providers import SourceError


def cipher(settings: Settings) -> Fernet:
    key = settings.data_source_encryption_key
    if key is None:
        raise SourceError("数据源加密密钥未配置，请执行 make selfhost 或配置 QF_DATA_SOURCE_ENCRYPTION_KEY 后重启服务。", status_code=503)
    return Fernet(base64.urlsafe_b64encode(bytes.fromhex(key.get_secret_value())))


def encrypt(settings: Settings, key: str, secrets: dict) -> str:
    return cipher(settings).encrypt(json.dumps({"source": key, "secrets": secrets}).encode()).decode()


def decrypt(settings: Settings, key: str, encrypted: str | None) -> dict:
    if encrypted is None:
        return {}
    try:
        envelope = json.loads(cipher(settings).decrypt(encrypted.encode()))
        if envelope["source"] != key or not isinstance(envelope["secrets"], dict):
            raise ValueError("credential envelope mismatch")
        return envelope["secrets"]
    except (InvalidToken, ValueError, KeyError, TypeError):
        # Never replace an unreadable envelope with a legacy .env value.
        raise SourceError("数据源凭据无法解密，请恢复原有加密密钥；现有配置未被修改。", status_code=503) from None
