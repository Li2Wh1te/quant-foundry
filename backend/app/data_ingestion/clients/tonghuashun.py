"""Bounded read-only Fuyao REST transport, without ingestion policy or writes.

Endpoint-specific normalization and persistence belong to later adapters. The
transport preserves nullable values and provider units, and never infers that a
successful empty response proves historical coverage or advances a checkpoint.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
import json
import math
import re
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from app.data_sources.errors import SourceError
from app.data_sources.tonghuashun_catalog import INTERFACES


ERRORS = {
    "unauthenticated": ("同花顺认证失败，请检查 API Key 是否有效。", 422),
    "forbidden": ("同花顺接口权限不足，请检查当前 API Key 的授权。", 422),
    "invalid_parameters": ("同花顺请求参数不符合接口要求，请检查标的、日期和查询范围。", 422),
    "not_found": ("同花顺未找到该标的，请检查完整标的代码。", 422),
    "data_not_ready": ("同花顺数据尚未就绪，请稍后重试；本次未确认数据覆盖。", 503),
    "unsupported": ("同花顺接口不支持该标的类型，请检查接口适用范围。", 422),
    "rate_limited": ("同花顺请求受到限流，请降低请求频率并稍后重试。", 503),
    "upstream_error": ("同花顺服务暂时不可用，请稍后重试。", 502),
    "network_error": ("同花顺连接失败，请检查 API 地址或网络。", 502),
    "timeout": ("同花顺请求超时，请稍后重试。", 504),
    "invalid_response": ("同花顺响应格式异常，未能确认请求成功。", 502),
    "response_too_large": ("同花顺响应超过大小限制，请缩小查询范围。", 502),
    "rejected": ("同花顺拒绝了请求，请检查接口配置与服务状态。", 502),
}
BUSINESS_ERRORS = {
    1001: "invalid_parameters", 1002: "invalid_parameters", 1003: "invalid_parameters",
    1004: "invalid_parameters", 2001: "unauthenticated", 2003: "forbidden",
    3001: "not_found", 3002: "data_not_ready", 3004: "unsupported",
    4001: "rate_limited", 5001: "upstream_error", 5002: "upstream_error", 5003: "upstream_error",
}
RETRYABLE = frozenset({"rate_limited", "upstream_error", "network_error", "timeout"})


def _reject_json_constant(value: str):
    """NaN and infinity are not valid provider financial facts or JSON values."""
    raise ValueError("non-standard JSON constant")


class TonghuashunError(SourceError):
    """No vendor message, response body, URL, or credential enters this error."""

    def __init__(self, kind: str, *, retry_after: float = 0):
        message, status = ERRORS[kind]
        super().__init__(message, status_code=status)
        self.kind = kind
        self.retry_after = retry_after


def validate_connection(api_url: Any, api_key: Any) -> tuple[str, str]:
    """Accept explicit HTTP(S) proxy roots while rejecting credential URLs."""
    if not isinstance(api_url, str) or not api_url.strip() or len(api_url) > 2048:
        raise SourceError("请填写有效的 API 地址。", field="api_url")
    api_url = api_url.strip().rstrip("/")
    try:
        parsed = urlsplit(api_url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in api_url))
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise SourceError("API 地址须为 HTTP(S)，不能包含账号、密码、查询参数或片段。", field="api_url")
    # Requests encodes header values as Latin-1; API keys must be printable
    # ASCII so invalid header input cannot escape as an unsanitized exception.
    if (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096
        or any(ord(char) < 33 or ord(char) > 126 for char in api_key.strip())):
        raise SourceError("请填写有效的同花顺 API Key。", field="api_key")
    return api_url, api_key.strip()


@dataclass(frozen=True)
class TonghuashunResponse:
    data: dict[str, Any]
    request_id: str | None


class _RequestGate:
    """Serialize in-process requests, including probes, within their deadlines.

    This is a per-process gate, not a distributed account quota. A 429 cooldown
    also applies to other clients in this process, including the next job.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.next_start = 0.0

    def enter(self, deadline: float, interval_seconds: float):
        if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TonghuashunError("timeout")
        delay = max(0, self.next_start - time.monotonic())
        if time.monotonic() + delay >= deadline:
            self.lock.release()
            raise TonghuashunError("rate_limited" if delay else "timeout", retry_after=delay)
        if delay:
            time.sleep(delay)
        self.next_start = time.monotonic() + interval_seconds


_gate = _RequestGate()


class TonghuashunClient:
    """Snapshot credentials per client; only documented GET paths are allowed."""

    def __init__(self, api_url: str, api_key: str, *, interval_ms: int = 1000,
                 max_attempts: int = 3, deadline_seconds: float = 30,
                 max_response_bytes: int = 8 * 1024 * 1024):
        self._api_url, self._api_key = validate_connection(api_url, api_key)
        if (not 0 <= interval_ms <= 60000 or not 1 <= max_attempts <= 3
            or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= 60
            or not 1 <= max_response_bytes <= 8 * 1024 * 1024):
            raise ValueError("invalid transport limits")
        self.interval_ms = interval_ms
        self.max_attempts = max_attempts
        self.deadline_seconds = deadline_seconds
        self.max_response_bytes = max_response_bytes

    @classmethod
    def from_settings(cls, settings):
        # Close the credential read transaction before any network I/O. Later
        # configuration changes affect newly created clients only.
        from sqlalchemy.orm import Session
        from app.db.session import get_engine
        from app.data_sources.service import runtime_credentials

        with Session(get_engine()) as session:
            values, secrets = runtime_credentials(session, settings, "tonghuashun")
        return cls(values["api_url"], secrets["api_key"],
                   interval_ms=settings.ingestion_request_interval_ms)

    def request(self, interface_key: str, params: Mapping[str, Any] | None = None) -> TonghuashunResponse:
        interface = INTERFACES.get(interface_key)
        if interface is None:
            raise SourceError("同花顺接口尚未登记，无法发起请求。")
        deadline = time.monotonic() + self.deadline_seconds
        for attempt in range(self.max_attempts):
            _gate.enter(deadline, self.interval_ms / 1000)
            failure = None
            try:
                return self._get(interface.path, dict(params or {}), deadline)
            except TonghuashunError as exc:
                failure = exc
                if exc.kind == "rate_limited":
                    _gate.next_start = max(_gate.next_start,
                        time.monotonic() + max(1, exc.retry_after, 2 ** attempt))
            finally:
                _gate.lock.release()
            if failure.kind not in RETRYABLE or attempt + 1 >= self.max_attempts:
                raise failure from None
            delay = max(2 ** attempt, failure.retry_after)
            if time.monotonic() + delay >= deadline:
                raise failure from None
            time.sleep(delay)
        raise TonghuashunError("timeout")  # Defensive; all attempts return or raise.

    def _get(self, path: str, params: dict, deadline: float) -> TonghuashunResponse:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TonghuashunError("timeout")
        try:
            # No persistent session means no cookies or credential-bearing
            # redirects. The API key is a header, never a query parameter.
            with requests.get(self._api_url + path, params=params,
                headers={"X-api-key": self._api_key, "Accept": "application/json"},
                timeout=(min(3, remaining), min(8, remaining)),
                allow_redirects=False, stream=True) as response:
                retry_after = self._retry_after(response.headers.get("Retry-After"))
                if response.status_code != 200:
                    kind = {401: "unauthenticated", 403: "forbidden", 429: "rate_limited",
                            408: "timeout", 500: "upstream_error", 502: "upstream_error",
                            503: "upstream_error", 504: "upstream_error"}.get(response.status_code, "rejected")
                    raise TonghuashunError(kind, retry_after=retry_after)
                raw = bytearray()
                # Check elapsed time on every arriving byte to bound slow-drip
                # servers as well as normal bulk bodies. The byte limit also
                # applies to decompressed content, not just Content-Length.
                for chunk in response.iter_content(1):
                    if time.monotonic() >= deadline:
                        raise TonghuashunError("timeout")
                    raw.extend(chunk)
                    if len(raw) > self.max_response_bytes:
                        raise TonghuashunError("response_too_large")
                if time.monotonic() >= deadline:
                    raise TonghuashunError("timeout")
                body = json.loads(raw, parse_float=Decimal, parse_constant=_reject_json_constant)
                if not isinstance(body, dict) or type(body.get("code")) is not int:
                    raise TonghuashunError("invalid_response")
                if body["code"] != 0:
                    raise TonghuashunError(BUSINESS_ERRORS.get(body["code"], "rejected"), retry_after=retry_after)
                if not isinstance(body.get("data"), dict):
                    raise TonghuashunError("invalid_response")
                request_id = body.get("request_id")
                # Keep bounded technical trace context without accepting a
                # reflected credential as a request identifier.
                if (not isinstance(request_id, str) or self._api_key in request_id
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id)):
                    request_id = None
                return TonghuashunResponse(body["data"], request_id)
        except requests.Timeout:
            raise TonghuashunError("timeout") from None
        except requests.RequestException:
            raise TonghuashunError("network_error") from None
        except (ValueError, TypeError, RecursionError):
            raise TonghuashunError("invalid_response") from None

    @staticmethod
    def _retry_after(value: str | None) -> float:
        """Honor both delta seconds and HTTP dates, ignoring malformed values."""
        if not isinstance(value, str):
            return 0
        try:
            if value.strip().isdigit():
                seconds = float(value)
            else:
                seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            return max(0, seconds) if math.isfinite(seconds) else 0
        except (ValueError, TypeError, OverflowError):
            return 0
