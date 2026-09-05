"""Adapter that wraps an already-loaded page strategy module.

The adapter implements the engine-internal lifecycle protocol: it wraps a
module holding the published strategy's ``run(context, parameters)`` entry
point, provides default no-op lifecycle callbacks, and converts each raw
``run`` return value into an immutable :class:`StrategyDecision` through the
decision-mode registry.

This module deliberately has no source-loading capability: compiling and
executing strategy source only ever happens in the worker module that backs
the isolated backtest subprocess.  Callers pass an already-loaded module
object here, so the API process cannot accidentally execute private code
through this adapter.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from types import MappingProxyType, ModuleType
from typing import Any
from uuid import UUID

from .contract import STRATEGY_CONTRACT_VERSION, StrategyProtocolError
from .context import DecisionContext
from .decisions import DecisionModeRegistry, StrategyDecision, build_default_registry


class StrategyLoadError(StrategyProtocolError):
    """Raised when a strategy module cannot provide the required entry."""


def _freeze_parameters(value: object) -> object:
    """Recursively convert parameters into immutable containers.

    Parameters originate from the revision's JSONB storage, so mapping, list,
    tuple, and scalar values are the supported inputs; mappings become
    mapping proxies and sequences become tuples at every nesting level, so a
    mutated parameter can never leak into later steps of the same run.
    Arbitrary custom mutable Python objects are outside this JSON contract.
    """

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_parameters(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_parameters(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return value


class FunctionStrategyAdapter:
    """Wrap one already-loaded module-level ``run(context, parameters)`` function.

    The adapter satisfies the internal :class:`StrategyProgram` protocol:
    ``on_step`` performs the decision while the remaining callbacks default to
    no-ops so page functions never need to implement them.
    """

    __slots__ = ("_registry", "_parameters", "_run")

    def __init__(
        self,
        module: ModuleType,
        *,
        parameters: Mapping[str, Any],
        registry: DecisionModeRegistry | None = None,
    ) -> None:
        object.__setattr__(self, "_registry", registry or build_default_registry())
        # Parameters come from the published revision (validated against its
        # schema) and are fixed, deeply read-only for the whole run.
        object.__setattr__(self, "_parameters", _freeze_parameters(dict(parameters)))
        object.__setattr__(self, "_run", self._require_entry_point(module))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("the strategy adapter is read-only once constructed")

    @staticmethod
    def _require_entry_point(module: ModuleType):
        """Return the single module-level synchronous ``run`` function."""

        run = getattr(module, "run", None)
        if run is None or not callable(run):
            raise StrategyLoadError(
                "策略模块必须定义模块级 run(context, parameters) 函数。"
            )
        try:
            signature = inspect.signature(run)
        except (TypeError, ValueError) as exc:
            raise StrategyLoadError("无法解析 run 函数签名。") from exc
        parameters = list(signature.parameters.values())
        if len(parameters) != 2 or [
            parameter.name for parameter in parameters
        ] != ["context", "parameters"]:
            raise StrategyLoadError("run 函数签名必须为 run(context, parameters)。")
        if inspect.iscoroutinefunction(run):
            raise StrategyLoadError("run 函数必须是同步函数。")
        return run

    # ------------------------------------------------------------------
    # StrategyProgram lifecycle protocol
    # ------------------------------------------------------------------

    def on_start(self, context: Any) -> None:
        """Default no-op; page functions have no start hook."""

    def on_step(self, context: DecisionContext) -> StrategyDecision:
        """Run one decision and validate the returned payload."""

        raw = self._run(context, self._parameters)
        return self.build_decision(
            raw,
            step_sequence=context.step_sequence,
            decision_time=context.decision_time,
            known_instrument_ids=known_instrument_ids(context),
        )

    def on_order_update(self, update: Any) -> None:
        """Default no-op; page functions cannot observe order updates yet."""

    def on_fill(self, fill: Any) -> None:
        """Default no-op; page functions cannot observe fills."""

    def on_finish(self, context: Any) -> None:
        """Default no-op; page functions have no finish hook."""

    # ------------------------------------------------------------------

    def build_decision(
        self,
        payload: Any,
        *,
        step_sequence: int,
        decision_time,
        known_instrument_ids: set[UUID],
    ) -> StrategyDecision:
        """Validate a raw return value and build the immutable decision."""

        mode, targets = self._registry.validate(
            payload, known_instrument_ids=known_instrument_ids
        )
        reason = None
        if isinstance(payload, Mapping):
            raw_reason = payload.get("reason")
            if raw_reason is not None:
                if not isinstance(raw_reason, str):
                    from .contract import InvalidDecisionPayloadError

                    raise InvalidDecisionPayloadError("reason must be a string")
                reason = raw_reason
        return StrategyDecision(
            step_sequence=step_sequence,
            decision_time=decision_time,
            mode=mode,
            targets=targets,
            reason=reason,
            contract_version=STRATEGY_CONTRACT_VERSION,
        )


def known_instrument_ids(context: DecisionContext) -> set[UUID]:
    """Collect the identities a decision may reference in this step.

    The set combines the portfolio's non-zero positions with the candidate
    universe visible at ``data_cutoff``.  Targets outside this set fail the
    run instead of being silently accepted.
    """

    ids = {position.instrument_id for position in context.portfolio.positions}
    universe = getattr(context, "universe", None)
    query = getattr(universe, "query", None)
    if not callable(query):
        raise StrategyProtocolError(
            "strategy decision context must expose a bound universe query"
        )
    # The adapter runs after the strategy function.  For dynamic/hybrid
    # contexts, only an earlier strategy-owned query may authorize candidate
    # ids.  Calling ``query()`` here would make a hard-coded target appear
    # legal even when the strategy never consulted its current universe.
    scope_mode = getattr(universe, "scope_mode", None)
    has_queried = getattr(universe, "has_queried", None)
    dynamic = getattr(scope_mode, "value", scope_mode) in {"dynamic", "hybrid"}
    if dynamic and has_queried is not True:
        return ids
    # Static/legacy facades have no per-step authorization state, so retain
    # their historical query behavior.  A dynamic facade without a query
    # state remains fail-closed below rather than being eagerly queried.
    if dynamic and not callable(query):
        raise StrategyProtocolError(
            "strategy decision context must expose a bound universe query"
        )
    for candidate in tuple(query()):
        instrument_id = getattr(candidate, "instrument_id", None)
        if not isinstance(instrument_id, UUID):
            raise StrategyProtocolError(
                "strategy universe returned a candidate without instrument_id"
            )
        ids.add(instrument_id)
    return ids
