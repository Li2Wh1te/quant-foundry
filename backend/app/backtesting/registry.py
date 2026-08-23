"""Versioned component registry for replaceable backtesting engine parts.

Every replaceable engine component (timing policy, execution model, decision
interpreter, ...) is constructed through this registry by its stable
``(key, version)`` pair.  The registry never falls back to a different
version and never exposes Python class names or module paths as user-facing
identifiers: entries carry Chinese and English display names, and
``display_name`` renders as ``中文名（English name）`` for selectors and
labels while the internal ``key``/``version`` stay reserved for submitted
configuration, audit, and result detail.

Renaming an entry's display names never changes its stable key; registering a
second entry under the same ``(key, version)`` is rejected instead of being
silently overwritten.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from app.backtesting.domain import DomainValidationError

__all__ = [
    "DECISION_INTERPRETER_KEY_TARGET_WEIGHTS",
    "EXECUTION_MODEL_KEY_BAR_MARKET",
    "TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN",
    "ComponentRegistry",
    "ComponentRegistryEntry",
    "DuplicateRegistryEntryError",
    "RegistryError",
    "UnknownComponentError",
    "build_default_component_registry",
]


class RegistryError(DomainValidationError):
    """Base error for every registry contract violation."""

    def __init__(
        self, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class DuplicateRegistryEntryError(RegistryError):
    """Raised when one ``(key, version)`` pair is registered twice."""


class UnknownComponentError(RegistryError):
    """Raised when a key/version pair cannot be resolved exactly."""


_COMPONENT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _freeze_value(value: Any) -> Any:
    """Deep-freeze one schema/capability value into immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _unfreeze_value(value: Any) -> Any:
    """Convert frozen containers back into plain JSON-serializable types."""

    if isinstance(value, Mapping):
        return {str(key): _unfreeze_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unfreeze_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ComponentRegistryEntry:
    """One immutable registry entry describing a replaceable component.

    ``parameter_schema`` is a JSON-schema-like description of the parameters
    accepted by ``factory``; it is metadata for configuration surfaces, not an
    enforcement mechanism inside the registry itself.
    """

    component_kind: str
    key: str
    version: int
    name_zh: str
    name_en: str
    factory: Callable[[Mapping[str, Any]], object]
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.component_kind, str) or not self.component_kind.strip():
            raise RegistryError("component_kind must be non-blank text")
        if (
            not isinstance(self.key, str)
            or not _COMPONENT_KEY_PATTERN.match(self.key)
        ):
            raise RegistryError(
                "component key must be a stable machine identifier matching "
                f"{_COMPONENT_KEY_PATTERN.pattern!r}"
            )
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise RegistryError("component version must be an integer")
        if self.version <= 0:
            raise RegistryError("component version must be positive")
        for field_name in ("name_zh", "name_en"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RegistryError(f"{field_name} must be non-blank text")
        if not callable(self.factory):
            raise RegistryError("factory must be callable")
        for field_name in ("parameter_schema", "capabilities"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise RegistryError(f"{field_name} must be a string-keyed mapping")
            object.__setattr__(
                self,
                field_name,
                MappingProxyType(
                    {str(k): _freeze_value(v) for k, v in value.items()}
                ),
            )

    @property
    def display_name(self) -> str:
        """User-facing label: Chinese name followed by the English name."""

        return f"{self.name_zh}（{self.name_en}）"

    def describe(self) -> dict[str, Any]:
        """Return the API-facing descriptor without exposing factory internals.

        The payload deliberately contains no Python class names or module
        paths: ordinary selection surfaces consume only these fields plus
        ``display_name``.
        """

        return {
            "component_kind": self.component_kind,
            "key": self.key,
            "version": self.version,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "display_name": self.display_name,
            # Plain dict/list all the way down: API surfaces serialize this
            # payload directly, so frozen proxies must never leak out.
            "parameter_schema": _unfreeze_value(self.parameter_schema),
            "capabilities": _unfreeze_value(self.capabilities),
        }

    def construct(self, parameters: Mapping[str, Any] | None = None) -> object:
        """Build one component instance from frozen parameter values."""

        params = MappingProxyType(dict(parameters or {}))
        return self.factory(params)


class ComponentRegistry:
    """Exact-version registry of replaceable backtesting components.

    Resolution always requires an explicit version: an unknown version raises
    :class:`UnknownComponentError` and is never silently mapped onto the
    newest registered version, because persisted run configurations must keep
    resolving to the component they were written against.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], ComponentRegistryEntry] = {}

    def register(self, entry: ComponentRegistryEntry) -> None:
        """Add one entry; a duplicate ``(key, version)`` is rejected."""

        if not isinstance(entry, ComponentRegistryEntry):
            raise RegistryError("entry must be a ComponentRegistryEntry")
        identity = (entry.key, entry.version)
        if identity in self._entries:
            raise DuplicateRegistryEntryError(
                f"component {entry.key}@{entry.version} is already registered"
            )
        self._entries[identity] = entry

    def resolve(self, key: str, version: int) -> ComponentRegistryEntry:
        """Resolve one entry by exact ``(key, version)``; never fall back."""

        entry = self._entries.get((key, version))
        if entry is None:
            known_versions = sorted(
                registered_version
                for (registered_key, registered_version) in self._entries
                if registered_key == key
            )
            raise UnknownComponentError(
                f"no component {key}@{version} is registered; versions never "
                "fall back to a different registered version",
                details={
                    "key": key,
                    "requested_version": version,
                    "known_versions": known_versions,
                },
            )
        return entry

    def entries(
        self,
        *,
        component_kind: str | None = None,
    ) -> tuple[ComponentRegistryEntry, ...]:
        """All registered entries, ordered stably by kind, key, and version."""

        selected = [
            entry
            for entry in self._entries.values()
            if component_kind is None or entry.component_kind == component_kind
        ]
        return tuple(
            sorted(
                selected,
                key=lambda e: (e.component_kind, e.key, e.version),
            )
        )

    def require_capabilities(
        self,
        key: str,
        version: int,
        requirements: Mapping[str, Any],
    ) -> None:
        """Validate that an entry declares every required capability.

        A scalar capability must equal the required value; a sequence
        capability must contain every required item.  A missing capability is
        always a failure so callers cannot accidentally run with an unverified
        assumption such as settlement timing.
        """

        entry = self.resolve(key, version)
        declared = entry.capabilities
        missing: list[str] = []
        mismatched: list[str] = []
        for name, required in dict(requirements).items():
            if name not in declared:
                missing.append(name)
                continue
            actual = declared[name]
            if isinstance(actual, (tuple, list)):
                wanted = required if isinstance(required, (tuple, list)) else (required,)
                if not set(wanted).issubset(set(actual)):
                    mismatched.append(name)
            elif actual != required:
                mismatched.append(name)
        if missing or mismatched:
            raise RegistryError(
                f"component {key}@{version} does not satisfy the requested "
                "capabilities",
                details={
                    "missing": sorted(missing),
                    "mismatched": sorted(mismatched),
                },
            )


# ---------------------------------------------------------------------------
# First-version registered components
# ---------------------------------------------------------------------------

TIMING_POLICY_KIND = "timing_policy"
EXECUTION_MODEL_KIND = "execution_model"
DECISION_INTERPRETER_KIND = "decision_interpreter"

TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN = "after_close_to_next_open"
EXECUTION_MODEL_KEY_BAR_MARKET = "bar_market"
DECISION_INTERPRETER_KEY_TARGET_WEIGHTS = "target_weights"


def _build_after_close_to_next_open(parameters: Mapping[str, Any]) -> object:
    # Imported lazily to keep the module import graph acyclic.
    from app.backtesting.timing import AfterCloseToNextOpenV1

    return AfterCloseToNextOpenV1()


def _build_bar_market_model(parameters: Mapping[str, Any]) -> object:
    from app.backtesting.execution import BarMarketExecutionModel
    from app.backtesting.fees import FeeCalculator, FeeRule, FeeRoundingLevel, FeeRoundingMode, FeeSchedule
    from app.backtesting.slippage import BpsSlippageModel

    # Fee configuration is explicit and mandatory: silently matching with a
    # zero-cost default schedule in a formal run would be an audit failure.
    missing = [
        name
        for name in ("commission_rate", "commission_minimum")
        if name not in parameters
    ]
    if missing:
        raise RegistryError(
            "bar_market@1 requires explicitly configured fee parameters; "
            f"missing: {missing}",
            details={"missing_parameters": missing},
        )
    slippage_bps = parameters.get("slippage_bps", 0)
    price_tick = parameters.get("price_tick", "0.01")
    schedule = FeeSchedule(
        key="bar_market_flat_commission",
        version=1,
        fee_rules=(
            FeeRule(
                key="commission",
                category="commission",
                rate=parameters["commission_rate"],
                minimum=parameters["commission_minimum"],
                rounding_level=FeeRoundingLevel.FEE_ITEM,
                rounding_scope="commission",
                rounding_mode=FeeRoundingMode.HALF_UP,
                rounding_precision="0.01",
            ),
        ),
    )
    return BarMarketExecutionModel(
        slippage_model=BpsSlippageModel(
            slippage_bps=slippage_bps, price_tick=price_tick
        ),
        fee_calculator=FeeCalculator(schedule),
        model_key=EXECUTION_MODEL_KEY_BAR_MARKET,
        model_version=1,
    )


def _build_target_weights_interpreter(parameters: Mapping[str, Any]) -> object:
    # Imported lazily to keep the module import graph acyclic.
    from app.backtesting.runtime import TargetWeightsInterpreter

    board_lot = parameters.get("board_lot", 100)
    return TargetWeightsInterpreter(board_lot=board_lot)


def build_default_component_registry() -> ComponentRegistry:
    """Create the first-version registry with its three built-in entries."""

    registry = ComponentRegistry()
    registry.register(
        ComponentRegistryEntry(
            component_kind=TIMING_POLICY_KIND,
            key=TIMING_POLICY_KEY_AFTER_CLOSE_TO_NEXT_OPEN,
            version=1,
            name_zh="收盘后决策、次日开盘成交",
            name_en="After-Close Decision to Next-Open Execution",
            factory=_build_after_close_to_next_open,
            parameter_schema={},
            capabilities={
                "frequency": "1d",
                "decision_timing": "after_close",
                "execution_timing": "next_open",
                "settlement_timing": "t_plus_1_before_open_match",
            },
        )
    )
    registry.register(
        ComponentRegistryEntry(
            component_kind=EXECUTION_MODEL_KIND,
            key=EXECUTION_MODEL_KEY_BAR_MARKET,
            version=1,
            name_zh="Bar 市价撮合",
            name_en="Bar Market Execution",
            factory=_build_bar_market_model,
            parameter_schema={
                "type": "object",
                "required": ["commission_rate", "commission_minimum"],
                "properties": {
                    "slippage_bps": {"type": "decimal-string", "default": "0"},
                    "price_tick": {"type": "decimal-string", "default": "0.01"},
                    "commission_rate": {
                        "type": "decimal-string",
                        "description": "mandatory explicit commission rate",
                    },
                    "commission_minimum": {
                        "type": "decimal-string",
                        "description": "mandatory explicit commission minimum",
                    },
                },
                "additionalProperties": False,
            },
            capabilities={
                "fill_timing": "next_open",
                "supported_order_types": ("market",),
                "partial_fills": False,
            },
        )
    )
    registry.register(
        ComponentRegistryEntry(
            component_kind=DECISION_INTERPRETER_KIND,
            key=DECISION_INTERPRETER_KEY_TARGET_WEIGHTS,
            version=1,
            name_zh="目标权重",
            name_en="Target Weights",
            factory=_build_target_weights_interpreter,
            parameter_schema={
                "type": "object",
                "properties": {
                    "board_lot": {"type": "integer", "default": 100},
                },
                "additionalProperties": False,
            },
            capabilities={
                "decision_mode": "target_weights",
                "daily_rebalance": True,
            },
        )
    )
    return registry
