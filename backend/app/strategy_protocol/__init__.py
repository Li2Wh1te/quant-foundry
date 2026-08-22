"""Page-strategy protocol: contract version 1 domain objects and checks.

Public surface of the strategy protocol package:

* :mod:`contract` — protocol version, lookback limit, failure phase, errors;
* :mod:`decisions` — immutable ``StrategyDecision`` and the mode registry;
* :mod:`context` — read-only ``DecisionContext`` and nested DTOs;
* :mod:`data_view` — data/universe query contract with PIT boundaries;
* :mod:`adapter` — ``FunctionStrategyAdapter`` for ``run(context, parameters)``;
* :mod:`synthetic` — deterministic synthetic fixture for the contract check;
* :mod:`checker` / :mod:`worker` — isolated subprocess contract check.
"""

from .contract import (
    FAILURE_PHASE_STRATEGY_CONTRACT_CHECK,
    MAX_LOOKBACK_SESSIONS,
    STRATEGY_CONTRACT_VERSION,
)
from .context import DecisionContext
from .data_view import StrategyDataDTO, UniverseQueryDTO
from .decisions import DecisionModeRegistry, StrategyDecision, build_default_registry

__all__ = [
    "FAILURE_PHASE_STRATEGY_CONTRACT_CHECK",
    "MAX_LOOKBACK_SESSIONS",
    "STRATEGY_CONTRACT_VERSION",
    "DecisionContext",
    "DecisionModeRegistry",
    "StrategyDataDTO",
    "StrategyDecision",
    "UniverseQueryDTO",
    "build_default_registry",
]
