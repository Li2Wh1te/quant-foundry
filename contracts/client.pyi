# Generated from SDK client.py; run scripts/generate_s3_contracts.py.
from typing import Any, Mapping, Sequence

class RunConfig:

    def __init__(self, *, strategy_revision_id: str, account_id: str, initial_cash: str, start: str, end: str, frequency: str, universe: Sequence[str], execution_model: str, parameters: Mapping[str, Any] | None=None, benchmark: str | None=None, currency: str='CNY', reference_calendar: str | None=None, seed: int=0, participation_rate: str='0.10', slippage_bps: str='0', risk_free_rate: str='0', annualization_sessions: int=252, result_sampling: str='session_close', cost_overrides: Mapping[str, str] | None=None, api_schema: str='qf.backtest.v2') -> None:
        ...

    def to_dict(self) -> dict[str, Any]:
        ...

class BacktestClient:

    def __init__(self, *, base_url: str, token: str) -> None:
        ...

    def preflight(self, config: RunConfig) -> Mapping[str, Any]:
        ...

    def submit(self, config: RunConfig, *, idempotency_key: str) -> Mapping[str, Any]:
        ...

    def status(self, run_id: str) -> Mapping[str, Any]:
        ...

    def cancel(self, run_id: str) -> Mapping[str, Any]:
        ...

    def result(self, run_id: str, *, kind: str='summary', limit: int=100, cursor: str | None=None) -> Mapping[str, Any]:
        ...

    def batch(self, configs: Sequence[RunConfig], *, idempotency_key: str) -> Mapping[str, Any]:
        ...

    def batch_status(self, batch_id: str, *, limit: int=100, cursor: str | None=None) -> Mapping[str, Any]:
        ...

    def cancel_batch(self, batch_id: str) -> Mapping[str, Any]:
        ...
