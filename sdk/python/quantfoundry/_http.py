"""Bounded direct HTTP transport; no redirects, proxies, retries or raw errors."""
from __future__ import annotations

import ipaddress
from http.client import HTTPException
import json
import math
import re
import socket
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 1024 * 1024


class BacktestError(Exception):
    """Safe client/HTTP diagnostic. Never holds a request or credential."""
    def __init__(self, code: str, message: str, *, operation: str = "client",
                 scope: Mapping[str, Any] | None = None,
                 status_code: int | None = None) -> None:
        self.code, self.message, self.operation = code, message, operation
        self.scope, self.status_code = dict(scope or {}), status_code
        super().__init__(f"{code}: {message}")

    def diagnostic(self) -> dict[str, Any]:
        return dict(code=self.code, message=self.message, operation=self.operation,
                    scope=self.scope, status_code=self.status_code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def endpoint(value: str) -> str:
    try:
        if (not isinstance(value, str) or len(value) > 2048
                or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
            raise ValueError()
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise ValueError()
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError()
        if parts.scheme == "http" and parts.hostname != "localhost":
            if not ipaddress.ip_address(parts.hostname).is_loopback:
                raise ValueError()
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise BacktestError("INVALID_CLIENT_CONFIG", "base_url 须为无凭据/查询的服务源站；HTTP 仅限本机，其他地址使用 HTTPS") from None


def credential(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(not 33 <= ord(c) <= 126 for c in value)):
        raise BacktestError("UNAUTHENTICATED", "需要有效 Bearer token；请使用环境变量或隐藏输入")
    return value


def redact(value: Any, token: str) -> Any:
    """Even an echoing/erroring server cannot put this credential in output."""
    if isinstance(value, str):
        for secret in {token, quote(token, safe=""), quote_plus(token, safe="")}:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, dict):
        return {redact(key, token): redact(item, token) for key, item in value.items()}
    return value


def _decode(data: bytes) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError()
        return value

    def constant(_):
        raise ValueError()

    value = json.loads(data, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError()
    return value


class HttpTransport:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.origin = endpoint(base_url)
        self._token = credential(token)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise BacktestError("INVALID_CLIENT_CONFIG", "timeout 须为 0..300 秒内的正数")
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, operation: str, *, payload=None,
                idempotency_key: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": "Bearer " + self._token,
                   "Accept": "application/json", "User-Agent": "quantfoundry-sdk"}
        if idempotency_key is not None:
            if (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
                    or any(not 33 <= ord(c) <= 126 for c in idempotency_key)):
                raise BacktestError("INVALID_RUN_CONFIG", "Idempotency-Key 须为 1..128 个可见 ASCII 字符", operation=operation)
            if self._token in idempotency_key:
                raise BacktestError("INVALID_RUN_CONFIG", "凭据不能用作幂等键", operation=operation)
            headers["Idempotency-Key"] = idempotency_key
        try:
            data = None if payload is None else json.dumps(payload, ensure_ascii=False,
                allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise BacktestError("INVALID_RUN_CONFIG", "请求必须为有限 JSON", operation=operation) from None
        if data is not None:
            if len(data) > MAX_BYTES:
                raise BacktestError("RESOURCE_LIMIT", "HTTP 控制请求超过 1MiB", operation=operation)
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + "/api/admin/backtests" + path, data=data, headers=headers, method=method)
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                if 300 <= status < 400:
                    raise BacktestError("REDIRECT_REFUSED", "服务返回重定向；请核对源站，客户端不会转发凭据", operation=operation, status_code=status)
                chunks, size, deadline = [], 0, time.monotonic() + self.timeout
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError()
                    chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise BacktestError("RESOURCE_LIMIT", "HTTP 响应超过 1MiB；请缩小分页", operation=operation, status_code=status)
                    chunks.append(chunk)
                try:
                    value = redact(_decode(b"".join(chunks)), self._token)
                except (ValueError, UnicodeError, RecursionError):
                    raise BacktestError("INVALID_RESPONSE", "服务未返回有效的有限 JSON 对象", operation=operation, status_code=status) from None
                if not 200 <= status < 300:
                    detail = value.get("detail", value)
                    detail = detail if isinstance(detail, dict) else {}
                    code = detail.get("code", "UNAUTHENTICATED" if status == 401 else
                                      "DATA_RESTRICTED" if status in (403, 404) else "HTTP_ERROR")
                    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
                        code = "HTTP_ERROR"
                    message = detail.get("message")
                    if not isinstance(message, str) or not message:
                        message = "身份无效或已失效" if status == 401 else "请求被服务拒绝，请核对权限、配置和服务状态"
                    scope = detail.get("scope")
                    raise BacktestError(code, message[:2048], operation=operation,
                                        scope=scope if isinstance(scope, dict) else {}, status_code=status)
                return value
        except BacktestError:
            raise
        except (TimeoutError, socket.timeout):
            raise BacktestError("HTTP_TIMEOUT", "请求超时；提交结果可能已受理，请以同一配置和幂等键核对", operation=operation) from None
        except (URLError, OSError, ValueError, HTTPException):
            raise BacktestError("HTTP_UNAVAILABLE", "无法连接或读取运行服务；请核对源站和 TLS，提交结果未知时复用原幂等键", operation=operation) from None
