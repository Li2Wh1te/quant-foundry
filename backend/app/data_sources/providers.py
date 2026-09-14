"""Each provider owns its field schema, validation, and bounded read-only probe."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from app.data_sources.errors import SourceError


@dataclass(frozen=True)
class ProbeResult:
    status: str
    message: str
    checked_at: datetime

    @property
    def ok(self) -> bool:
        return self.status == "connected"


class TushareProvider:
    key = "tushare"
    name = "Tushare"
    fields = (
        {"key": "api_url", "label": "API 地址", "type": "url", "required": True,
         "default": "http://api.tushare.pro", "help": "支持 Tushare 官方地址或 HTTP(S) 代理地址。"},
        {"key": "token", "label": "Token", "type": "secret", "required": True,
         "help": "首次配置必须填写；已有凭据时留空沿用，原值不会回显。"},
    )

    def validate(self, values: dict[str, Any], secrets: dict[str, str]) -> tuple[dict, dict]:
        if set(values) - {field["key"] for field in self.fields}:
            raise SourceError("配置包含该数据源不支持的字段。")
        url = values.get("api_url", self.fields[0]["default"])
        if not isinstance(url, str) or not url.strip() or len(url) > 2048:
            raise SourceError("请填写有效的 API 地址。", field="api_url")
        url = url.strip().rstrip("/")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment
                     and not any(char.isspace() or ord(char) < 32 for char in url))
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise SourceError("API 地址须为 HTTP(S)，不能包含账号、密码、查询参数或片段。", field="api_url")
        token = values.get("token", "")
        if not isinstance(token, str) or len(token) > 4096:
            raise SourceError("Token 格式不正确。", field="token")
        token = token.strip() or secrets.get("token", "")
        if not token:
            raise SourceError("请填写 Tushare Token。", field="token")
        return {"api_url": url}, {"token": token}

    def probe(self, values: dict, secrets: dict) -> ProbeResult:
        # Probe one known trading day. Do not use the SDK here: its empty frame
        # on HTTP failures cannot distinguish a working connection from failure.
        payload = {"api_name": "trade_cal", "token": secrets["token"],
                   "params": {"exchange": "SSE", "start_date": "20250102", "end_date": "20250102",
                              "ts_type_name": values["api_url"]},
                   "fields": "cal_date,is_open"}
        status, message = "network_error", "连接失败，请检查 API 地址或网络。"
        deadline = time.monotonic() + 15
        try:
            with requests.post(values["api_url"] + "/trade_cal", json=payload,
                               timeout=(3, 8), allow_redirects=False, stream=True) as response:
                if response.status_code in (401, 403):
                    status, message = "rejected", "认证或权限检查未通过，请检查 Token 与接口权限。"
                elif response.status_code == 200:
                    # Bound response memory and never expose vendor messages:
                    # proxies may reflect the submitted credential in errors.
                    raw = bytearray()
                    # A single-byte iterator also bounds a slow-drip response:
                    # the wall deadline is checked as each byte arrives, rather
                    # than waiting indefinitely for a full buffered chunk.
                    for chunk in response.iter_content(1):
                        if time.monotonic() > deadline:
                            raise requests.Timeout()
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise ValueError("oversized probe response")
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("invalid response")
                    if data.get("code") != 0:
                        status, message = "rejected", "服务拒绝了连接测试，请检查 Token、接口权限或调用额度。"
                    else:
                        result = data.get("data")
                        if (not isinstance(result, dict)
                            or not isinstance(result.get("fields"), list)
                            or not {"cal_date", "is_open"}.issubset(result["fields"])
                            or not isinstance(result.get("items"), list)
                            or not result["items"]
                            or not all(isinstance(row, list) and len(row) == len(result["fields"]) for row in result["items"])):
                            raise ValueError("invalid data")
                        date_index = result["fields"].index("cal_date")
                        open_index = result["fields"].index("is_open")
                        if not any(row[date_index] == "20250102" and row[open_index] in (0, 1)
                                   for row in result["items"]):
                            raise ValueError("requested day missing")
                        status, message = "connected", "连接测试通过，交易日历接口可用；其他接口仍受各自权限与额度限制。"
        except requests.Timeout:
            status, message = "timeout", "连接测试超时，请检查地址或稍后重试。"
        except requests.RequestException:
            pass
        except (ValueError, TypeError):
            status, message = "invalid_response", "服务响应格式异常，未能确认连接可用。"
        return ProbeResult(status, message, datetime.now(UTC))


class TonghuashunProvider:
    """Fuyao REST credentials are independent of the Tushare token schema."""

    key = "tonghuashun"
    name = "同花顺"
    fields = (
        {"key": "api_url", "label": "API 地址", "type": "url", "required": True,
         "default": "https://fuyao.aicubes.cn",
         "help": "同花顺金融数据 API 服务地址，不包含 /api 接口路径。"},
        {"key": "api_key", "label": "API Key", "type": "secret", "required": True,
         "help": "在同花顺金融数据 API 管理页创建；已有凭据时留空沿用，原值不会回显。"},
    )

    def validate(self, values: dict[str, Any], secrets: dict[str, str]) -> tuple[dict, dict]:
        from app.data_ingestion.clients.tonghuashun import validate_connection

        if set(values) - {field["key"] for field in self.fields}:
            raise SourceError("配置包含该数据源不支持的字段。")
        url = values.get("api_url", self.fields[0]["default"])
        key = values.get("api_key", "")
        if not isinstance(key, str):
            raise SourceError("API Key 格式不正确。", field="api_key")
        key = key.strip() or secrets.get("api_key", "")
        url, key = validate_connection(url, key)
        return {"api_url": url}, {"api_key": key}

    def probe(self, values: dict, secrets: dict) -> ProbeResult:
        from app.data_ingestion.clients.tonghuashun import TonghuashunClient, TonghuashunError

        # One bounded metadata read proves this capability only. Do not run
        # all endpoints, import market data, or retry during a form submission.
        try:
            client = TonghuashunClient(values["api_url"], secrets["api_key"],
                max_attempts=1, deadline_seconds=12, max_response_bytes=65536)
            result = client.request("meta.tickers.list", {"asset_type": "fund-etf", "limit": 1, "offset": 0})
            items = result.data.get("item")
            if (not isinstance(items, list) or len(items) != 1
                or not isinstance(items[0], dict)
                or not isinstance(items[0].get("thscode"), str)
                or not items[0]["thscode"].strip()
                or items[0].get("asset_type") != "fund-etf"):
                raise TonghuashunError("invalid_response")
            return ProbeResult("connected",
                "连接测试通过，ETF 标的列表接口可用；其他接口权限和数据覆盖仍需分别验证。",
                datetime.now(UTC))
        except TonghuashunError as exc:
            return ProbeResult(exc.kind, str(exc), datetime.now(UTC))


# Adding a provider requires its own schema/validator/probe; the API and form
# must not assume every provider authenticates with the Tushare field pair.
PROVIDERS = {provider.key: provider for provider in (TushareProvider(), TonghuashunProvider())}


def require_provider(key: str):
    provider = PROVIDERS.get(key)
    if provider is None:
        raise SourceError("数据源尚未接入。", status_code=404)
    return provider
