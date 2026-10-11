"""HTTP submission SDK using the single D13 service and native RunConfig policy."""
import json
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode
from uuid import UUID
from . import _native
from ._http import BacktestError, HttpTransport

def _json_value(value: Any, parent_depth: int = 0) -> Any:
    if isinstance(value, Mapping):
        if parent_depth >= 9 or len(value) > 1024 * 1024 or any(not isinstance(k, str) for k in value):
            raise ValueError("invalid JSON object")
        return {key: _json_value(item, parent_depth + 1) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if parent_depth >= 9 or len(value) > 1024 * 1024:
            raise ValueError("invalid JSON array")
        return [_json_value(item, parent_depth + 1) for item in value]
    if isinstance(value, str) and len(value) > 1024 * 1024:
        raise ValueError("oversized JSON string")
    return value

def _json(value: Any) -> str:
    try:
        return json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        error = _native.ContractError("INVALID_RUN_CONFIG: 运行参数必须为有界有限JSON")
        error.code = "INVALID_RUN_CONFIG"
        error.operation = "run_config"
        error.scope = {}
        raise error from exc

class RunConfig:
    def __init__(self, *, strategy_revision_id: str, account_id: str,
                 initial_cash: str, start: str, end: str, frequency: str,
                 universe: Sequence[str], execution_model: str,
                 parameters: Mapping[str, Any] | None = None,
                 benchmark: str | None = None, currency: str = "CNY",
                 reference_calendar: str | None = None, seed: int = 0,
                 participation_rate: str = "0.10", slippage_bps: str = "0",
                 risk_free_rate: str = "0", annualization_sessions: int = 252,
                 result_sampling: str = "session_close",
                 cost_overrides: Mapping[str, str] | None = None,
                 api_schema: str = "qf.backtest.v2") -> None:
        config = dict(api_schema=api_schema, strategy_revision_id=strategy_revision_id,
                      account_id=account_id, initial_cash=initial_cash, start=start, end=end,
                      frequency=frequency, universe=universe, execution_model=execution_model,
                      parameters={} if parameters is None else parameters, currency=currency,
                      seed=seed, participation_rate=participation_rate, slippage_bps=slippage_bps,
                      risk_free_rate=risk_free_rate, annualization_sessions=annualization_sessions,
                      result_sampling=result_sampling)
        if benchmark is not None:
            config["benchmark"] = benchmark
        if reference_calendar is not None:
            config["reference_calendar"] = reference_calendar
        if cost_overrides is not None:
            config["cost_overrides"] = cost_overrides
        self._json = _native.validate_run_config_json(_json(config))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._json)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunConfig":
        """Validate a complete wire config, including schema and unknown fields."""
        return cls(**json.loads(_native.validate_run_config_json(_json(value))))


def _identity(value: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError()
        return str(UUID(value))
    except ValueError:
        raise BacktestError("INVALID_RUN_CONFIG", "运行/批次标识须为有效 UUID") from None


def _page(limit: int, cursor: str | None, maximum: int) -> str:
    from ._results import validate_result_request
    validate_result_request("orders", limit, cursor)
    if limit > maximum:
        raise BacktestError("INVALID_RUN_CONFIG", f"此分页最多 {maximum} 项")
    return "?" + urlencode(dict(limit=limit, **({"cursor": cursor} if cursor else {})))

class BacktestClient:
    def __init__(self, *, base_url: str, token: str, timeout: float = 30) -> None:
        self._http = HttpTransport(base_url, token, timeout)
        self.base_url = self._http.origin

    def capabilities(self) -> Mapping[str, Any]:
        return self._http.request("GET", "/capabilities", "capabilities")

    def preflight(self, config: RunConfig) -> Mapping[str, Any]:
        return self._http.request("POST", "/preflight", "preflight", payload=config.to_dict())
    def submit(self, config: RunConfig, *, idempotency_key: str) -> Mapping[str, Any]:
        return self._http.request("POST", "", "submit", payload=config.to_dict(), idempotency_key=idempotency_key)
    def status(self, run_id: str) -> Mapping[str, Any]:
        return self._http.request("GET", "/" + _identity(run_id), "status")
    def cancel(self, run_id: str) -> Mapping[str, Any]:
        return self._http.request("POST", "/" + _identity(run_id) + "/cancel", "cancel")
    def result(self, run_id: str, *, kind: str = "summary", limit: int = 100,
               cursor: str | None = None) -> Mapping[str, Any]:
        from ._results import validate_result_request
        validate_result_request(kind, limit, cursor)
        if kind == "summary" and cursor is not None:
            raise BacktestError("INVALID_RUN_CONFIG", "summary 不接受分页游标")
        value = self._http.request("GET", "/" + _identity(run_id) + "/results/" + kind + _page(limit, cursor, 10000), "result")
        if "items" in value and (not isinstance(value["items"], list) or len(value["items"]) > limit):
            raise BacktestError("INVALID_RESPONSE", "结果页超过请求条数")
        return value
    def batch(self, configs: Sequence[RunConfig], *, idempotency_key: str) -> Mapping[str, Any]:
        if not isinstance(configs, Sequence) or not 1 <= len(configs) <= 256 or any(not isinstance(c, RunConfig) for c in configs):
            raise BacktestError("INVALID_RUN_CONFIG", "批次需要 1..256 个 RunConfig；参数网格请显式展开")
        return self._http.request("POST", "/batches", "batch", payload={"configs": [c.to_dict() for c in configs]}, idempotency_key=idempotency_key)
    def batch_status(self, batch_id: str, *, limit: int = 100,
                     cursor: str | None = None) -> Mapping[str, Any]:
        value = self._http.request("GET", "/batches/" + _identity(batch_id) + _page(limit, cursor, 256), "batch_status")
        if not isinstance(value.get("items"), list) or len(value["items"]) > limit:
            raise BacktestError("INVALID_RESPONSE", "批次页超过请求条数")
        return value
    def cancel_batch(self, batch_id: str) -> Mapping[str, Any]:
        return self._http.request("POST", "/batches/" + _identity(batch_id) + "/cancel", "cancel_batch")

    def retry_failed(self, batch_id: str, *, idempotency_key: str) -> Mapping[str, Any]:
        return self._http.request("POST", "/batches/" + _identity(batch_id) + "/retry-failed", "retry_failed", idempotency_key=idempotency_key)

    def compare(self, run_ids: Sequence[str]) -> Mapping[str, Any]:
        if isinstance(run_ids, (str, bytes)) or not isinstance(run_ids, Sequence) or not 2 <= len(run_ids) <= 16:
            raise BacktestError("INVALID_RUN_CONFIG", "比较需要 2..16 个不同运行")
        ids = [_identity(value) for value in run_ids]
        if len(set(ids)) != len(ids):
            raise BacktestError("INVALID_RUN_CONFIG", "比较运行不能重复")
        return self._http.request("POST", "/compare", "compare", payload={"run_ids": ids})
