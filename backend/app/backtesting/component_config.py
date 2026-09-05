"""One resolution boundary for the supported formal daily-run profile.

Fixed infrastructure policies are explicit versioned selections too. Unsupported
alternatives fail here rather than being silently replaced inside the Worker.
"""

from collections.abc import Mapping
from app.backtesting.spec import ComponentSelection
from app.backtesting.registry import build_default_component_registry, RegistryError


FIXED_POLICIES = {
    "data_provider": ("etf_ingestion", "ETF 数据", "ETF Data"),
    "rule_package": ("china_listed_etf_rules", "境内 ETF 规则", "China ETF Rules"),
    "calendar_axis_policy": ("strict_compatible", "严格兼容日历", "Strict Compatible Calendars"),
    "time_axis": ("trading_day", "交易日时间轴", "Trading Day Axis"),
    "accounting_policy": ("accounting_policy", "T+1 账户会计", "T+1 Accounting"),
    "corporate_action_timing": ("after_open_match", "现金分红有效日", "Cash Dividend Effective Date"),
}


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
        expected = requested[kind]
        if (selected.key, selected.version) != (expected.key, expected.version):
            raise RegistryError(f"unsupported formal component: {kind} {selected.key}@{selected.version}")
        requested[kind] = selected
    requested["slippage_model"] = slippage or ComponentSelection("none", 1, {"price_tick": "0.01"})

    def resolve(kind, selected):
        if kind in FIXED_POLICIES:
            key, zh, en = FIXED_POLICIES[kind]
            if selected.parameters:
                raise RegistryError(f"{kind} has no configurable parameters in v1")
            return {"key": key, "version": 1, "kind": kind, "name_zh": zh, "name_en": en,
                    "display_name": f"{zh}（{en}）", "parameters": {}, "capabilities": {"frequency": "1d"}}
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
    for kind, (key, _, _) in FIXED_POLICIES.items():
        selected = components.get(kind)
        if selected is None and not require_all:
            continue
        if not isinstance(selected, Mapping) or (
            selected.get("key"), selected.get("version"), dict(selected.get("parameters", {}))
        ) != (key, 1, {}):
            raise RegistryError(f"runtime cannot honor frozen policy {kind}")
    if request.provider_key != FIXED_POLICIES["data_provider"][0]:
        raise RegistryError("runtime cannot honor frozen data provider")
    for kind in ("rule_package", "calendar_axis_policy"):
        actual = getattr(request, kind)
        if (actual.key, actual.version) != (FIXED_POLICIES[kind][0], 1):
            raise RegistryError(f"runtime cannot honor data request {kind}")
