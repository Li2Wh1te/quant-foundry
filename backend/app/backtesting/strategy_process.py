"""Minimal isolated strategy execution port for formal runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.strategy_protocol.checker import ContractCheckRequest, run_strategy_contract_check


@dataclass(frozen=True, slots=True)
class StrategyProcessInput:
    source_code: str
    parameter_schema: Mapping[str, Any]
    parameters: Mapping[str, Any]


def check_and_create_adapter(request: StrategyProcessInput):
    """Run startup contract check before returning an adapter port.

    The adapter itself must be constructed inside the isolated worker; this
    helper therefore only exposes the check result to orchestration callers.
    """
    return run_strategy_contract_check(ContractCheckRequest(
        source_code=request.source_code,
        parameter_schema=request.parameter_schema,
        default_parameters=request.parameters,
    ))


__all__ = ["StrategyProcessInput", "check_and_create_adapter"]
