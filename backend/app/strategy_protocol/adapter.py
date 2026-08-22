"""Adapter that connects a page ``run(context, parameters)`` function.

The adapter implements the engine-internal lifecycle protocol: it loads the
published strategy module exactly once per run, provides default no-op
lifecycle callbacks, and converts each raw ``run`` return value into an
immutable :class:`StrategyDecision` through the decision-mode registry.

Loading and executing strategy source only ever happens inside the isolated
backtest subprocess (or its contract-check sibling); the API process never
imports user code.
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
    """Raised when the published source cannot provide the required entry."""


ISOLATED_SUBPROCESS_SCOPE = "isolated_subprocess"
"""Execution-scope acknowledgment required before user source is executed.

Loading and running strategy source is only ever legitimate inside the
isolated backtest subprocess (or its contract-check sibling).  Callers must
pass this exact scope to :meth:`FunctionStrategyAdapter.from_source` so the
API or any future Runner process cannot accidentally execute private code.
"""


def _freeze_parameters(value: object) -> object:
    """Recursively convert parameters into immutable containers.

    The strategy receives a read-only object: mappings become mapping proxies
    and lists become tuples at every nesting level, so a mutated parameter can
    never leak into later steps of the same run.
    """

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_parameters(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_parameters(item) for item in value)
    return value


class FunctionStrategyAdapter:
    """Wrap one module-level ``run(context, parameters)`` function.

    The adapter satisfies the internal :class:`StrategyProgram` protocol:
    ``on_step`` performs the decision while the remaining callbacks default to
    no-ops so page functions never need to implement them.
    """

    def __init__(
        self,
        module: ModuleType,
        *,
        parameters: Mapping[str, Any],
        registry: DecisionModeRegistry | None = None,
    ) -> None:
        self._registry = registry or build_default_registry()
        # Parameters come from the published revision (validated against its
        # schema) and are fixed, deeply read-only for the whole run.
        self._parameters = _freeze_parameters(dict(parameters))
        self._run = self._require_entry_point(module)

    @classmethod
    def from_source(
        cls,
        source_code: str,
        *,
        parameters: Mapping[str, Any],
        execution_scope: str,
        module_name: str = "published_strategy",
        registry: DecisionModeRegistry | None = None,
    ) -> "FunctionStrategyAdapter":
        """Load a published revision's source into a fresh module namespace.

        ``execution_scope`` must be exactly
        :data:`ISOLATED_SUBPROCESS_SCOPE`.  Requiring the explicit
        acknowledgment keeps ``exec`` out of reach of accidental callers such
        as API request handlers; only the isolated worker passes it.
        """

        if execution_scope != ISOLATED_SUBPROCESS_SCOPE:
            raise StrategyLoadError(
                "strategy source may only be loaded inside the isolated "
                "backtest subprocess"
            )
        module = ModuleType(module_name)
        compiled = compile(source_code, module_name, "exec")
        exec(compiled, module.__dict__)  # noqa: S102 - isolated subprocess only
        return cls(module, parameters=parameters, registry=registry)

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
    for candidate in context.universe.query():
        ids.add(candidate.instrument_id)
    return ids
