"""RunConfig is executable; transport belongs to D14 and fails explicitly here."""
import json
from typing import Any, Mapping, Sequence
from . import _native

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

def _unavailable() -> None:
    error = _native.ContractError("CAPABILITY_UNAVAILABLE: D01仅提供共享契约，运行服务与客户端传输尚未实现")
    error.code = "CAPABILITY_UNAVAILABLE"
    error.operation = "client"
    error.scope = {}
    raise error

class BacktestClient:
    def __init__(self, *, base_url: str, token: str) -> None:
        self.base_url = base_url
        # This boundary does not persist, log or transmit the supplied token.

    def preflight(self, config: RunConfig) -> Mapping[str, Any]:
        _unavailable()
    def submit(self, config: RunConfig, *, idempotency_key: str) -> Mapping[str, Any]:
        _unavailable()
    def status(self, run_id: str) -> Mapping[str, Any]:
        _unavailable()
    def cancel(self, run_id: str) -> Mapping[str, Any]:
        _unavailable()
    def result(self, run_id: str, *, kind: str = "summary", limit: int = 100,
               cursor: str | None = None) -> Mapping[str, Any]:
        _unavailable()
    def batch(self, configs: Sequence[RunConfig], *, idempotency_key: str) -> Mapping[str, Any]:
        _unavailable()
    def batch_status(self, batch_id: str, *, limit: int = 100,
                     cursor: str | None = None) -> Mapping[str, Any]:
        _unavailable()
    def cancel_batch(self, batch_id: str) -> Mapping[str, Any]:
        _unavailable()
