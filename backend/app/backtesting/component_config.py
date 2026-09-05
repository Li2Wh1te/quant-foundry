"""One resolution boundary for the supported formal daily-run profile.

Fixed infrastructure policies are explicit versioned selections too. Unsupported
alternatives fail here rather than being silently replaced inside the Worker.
"""

from collections.abc import Mapping
from app.backtesting.spec import ComponentSelection
from app.backtesting.registry import build_default_component_registry, RegistryError


from app.backtesting.infrastructure import INFRASTRUCTURE

# Compatibility name for callers inspecting default selections; this map is
# never used to construct a component or bypass its registered schema.
FIXED_POLICIES = {kind: (key, zh, en) for kind, key, zh, en, _ in INFRASTRUCTURE}


def resolve_components(slippage=None, selections=None, analyzers=()):
    """Validate and freeze every selected component, including its defaults."""
    registry = build_default_component_registry()
    requested = {
        "timing_policy": ComponentSelection("after_close_to_next_open", 1),
        "execution_model": ComponentSelection("bar_market", 1, {"commission_rate": "0.0003", "commission_minimum": "5"}),
        "decision_interpreter": ComponentSelection("long_only_target_weights", 1, {"weight_sum_tolerance": "0"}),
        **{kind: ComponentSelection(identity[0], 1) for kind, identity in FIXED_POLICIES.items()},
    }
    selections = selections or {}
    unknown = set(selections) - set(requested)
    if unknown:
        raise RegistryError(f"unsupported component kinds: {sorted(unknown)}")
    # The first formal profile supports one implementation per infrastructure
    # policy. Making this constraint explicit preserves future version freedom.
    for kind, selected in selections.items():
        requested[kind] = selected
    requested["slippage_model"] = slippage or ComponentSelection("none", 1, {"price_tick": "0.01"})

    def resolve(kind, selected):
        entry = registry.resolve(selected.key, selected.version)
        if entry.component_kind != kind:
            raise RegistryError(f"{selected.key} is not a {kind}")
        parameters = dict(selected.parameters)
        properties = entry.parameter_schema.get("properties", {})
        if set(parameters) - set(properties):
            raise RegistryError(f"{selected.key} received unknown parameters")
        for name, rule in properties.items():
            if name not in parameters and isinstance(rule, Mapping) and "default" in rule:
                parameters[name] = rule["default"]
        if any(name not in parameters for name in entry.parameter_schema.get("required", ())):
            raise RegistryError(f"{selected.key} is missing required parameters")
        # Construct once during admission so invalid values cannot survive as
        # apparently valid snapshots until the Worker starts.
        constructed = entry.construct(parameters)
        if kind == "analyzer":
            from app.backtesting.analyzers import validate_v1_analyzer_spec, resolve_config_rf_daily
            validate_v1_analyzer_spec(constructed)
            if selected.key == "sharpe_config_rf":
                resolve_config_rf_daily(constructed)
        return {"key": entry.key, "version": entry.version, "kind": kind,
                "name_zh": entry.name_zh, "name_en": entry.name_en, "display_name": entry.display_name,
                "parameters": parameters, "parameter_schema": dict(entry.parameter_schema),
                "capabilities": dict(entry.capabilities)}

    result = {kind: resolve(kind, selected) for kind, selected in requested.items()}
    identities = [(item.key, item.version) for item in analyzers]
    if len(identities) != len(set(identities)):
        raise RegistryError("analyzers must not repeat")
    if analyzers and sum(item.key.startswith("sharpe_") for item in analyzers) != 1:
        raise RegistryError("select exactly one Sharpe convention for an analyzed run")
    result["analyzer"] = [resolve("analyzer", selected) for selected in analyzers]
    return result


def selections_from_snapshot(snapshot):
    return {kind: ComponentSelection(item["key"], item["version"], item.get("parameters", {}))
            for kind, item in snapshot.items() if kind not in ("analyzer", "slippage_model")}


def validate_runtime_policies(components, request, *, require_all=False):
    """Check frozen policy identities against the concrete SQL daily runtime.

    Legacy snapshots may omit newly explicit policies, but an identity present
    in any snapshot must never be silently replaced by the Worker.
    """
    registry = build_default_component_registry()
    for kind in FIXED_POLICIES:
        selected = components.get(kind)
        if selected is None and not require_all:
            continue
        if not isinstance(selected, Mapping):
            raise RegistryError(f"runtime is missing frozen policy {kind}")
        try:
            entry = registry.resolve(selected["key"], selected["version"])
        except RegistryError as exc:
            raise RegistryError(f"runtime cannot resolve frozen {kind}: {exc}") from exc
        if entry.component_kind != kind:
            raise RegistryError(f"frozen component is not a {kind}")
        entry.construct(selected.get("parameters", {}))
    for kind, actual in (("data_provider", request.provider_key),
                         ("rule_package", request.rule_package.key),
                         ("calendar_axis_policy", request.calendar_axis_policy.key)):
        selected = components.get(kind)
        if selected and selected["key"] != actual:
            raise RegistryError(f"runtime cannot honor frozen {kind.replace(chr(95), chr(32))}")
    for kind in ("rule_package", "calendar_axis_policy"):
        selected = components.get(kind)
        if selected and selected["version"] != getattr(request, kind).version:
            raise RegistryError(f"runtime cannot honor frozen {kind} version")
