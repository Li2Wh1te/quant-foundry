"""Adapt the selected saved account fee schedule to Rust commission settings.

D13 supplies the existing account/profile fee_schedule JSON, after ownership
checks, plus an explicit commission-inclusion agreement. No database migration,
fee catalog rates, old fee calculation, defaults or account writes occur here.
The accepted small result is captured with the run, and joined to dated market
facts by qf-core, rather than deriving fees from the submission date.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from quantfoundry import _native
from quantfoundry.contracts import CommissionConfig
from quantfoundry.numeric import decimal_input

_COMPONENTS = {"stamp_duty", "transfer_fee", "regulatory_fee", "handling_fee"}


def _refuse(field: str, message: str) -> None:
    error = _native.ContractError(f"RULE_UNAVAILABLE: {message}")
    error.code = "RULE_UNAVAILABLE"
    error.operation = "account_commission_config"
    error.message = message
    error.scope = {"field": field}
    raise error


def commission_from_account_fee_schedule(
    fee_schedule: Mapping[str, Any],
    *,
    side: str,
    currency: str | None = None,
    applicability_context: Mapping[str, str] | None = None,
    included_components: Sequence[str] | None = None,
    settlement_scale: int | None = None,
    rounding: str | None = None,
    cost_overrides: Mapping[str, str] | None = None,
) -> CommissionConfig:
    """Read only commission rows; all compulsory fees remain rule-derived.

    Explicit options may also come from fee_schedule.metadata.s3_commission.
    Old fee_item/on_fill minimum charging is deliberately replaced by the new
    per-order accumulator; the saved commission rate/minimum remain unchanged.
    A caller must select buy/sell independently if the saved schedule differs.
    """
    if not isinstance(side, str) or side not in {"buy", "sell"} or not isinstance(fee_schedule, Mapping):
        _refuse("fee_schedule", "账户费用配置或方向无效")
    rules = fee_schedule.get("fee_rules")
    metadata = fee_schedule.get("metadata")
    if not isinstance(rules, list) or len(rules) > 256 or not isinstance(metadata, Mapping):
        _refuse("fee_schedule", "账户费用配置缺失或超过预算")
    if (currency if currency is not None else metadata.get("currency")) != "CNY":
        _refuse("currency", "账户费用币种必须明确为CNY")
    if applicability_context is not None and (not isinstance(applicability_context, Mapping) or len(applicability_context) > 16 or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v or len(k) > 128 or len(v) > 128 for k, v in applicability_context.items())):
        _refuse("applicability_context", "佣金适用身份事实无效或超过预算")
    agreement = metadata.get("s3_commission", {})
    if not isinstance(agreement, Mapping):
        _refuse("s3_commission", "账户佣金约定无效")
    selected = []
    other_components: set[str] = set()
    keys: set[str] = set()
    for rule in rules:
        if not isinstance(rule, Mapping) or not isinstance(rule.get("key"), str) or not rule["key"] or len(rule["key"]) > 128:
            _refuse("fee_rules", "账户费用项目无效")
        if rule["key"] in keys:
            _refuse("fee_rules", "账户费用项目重复")
        keys.add(rule["key"])
        rule_side = rule.get("side")
        if rule_side is not None and not isinstance(rule_side, str):
            _refuse("side", "账户费用方向无效")
        if rule_side not in {None, "both", "buy", "sell"}:
            _refuse("side", "账户费用方向未知")
        if rule.get("side") not in {None, "both", side}:
            continue
        applicability = rule.get("applicability", {})
        if not isinstance(applicability, Mapping) or len(applicability) > 16 or any(not isinstance(k, str) or not isinstance(v, str) or not k or not v or len(k) > 128 or len(v) > 128 for k, v in applicability.items()):
            _refuse("applicability", "账户费用适用条件无效")
        if applicability:
            if applicability_context is None or any(k not in applicability_context for k in applicability):
                _refuse("applicability", "佣金适用条件需要D13提供已核验的标的身份事实")
            if any(applicability_context[k] != v for k, v in applicability.items()):
                continue
        category = rule.get("category")
        if not isinstance(category, str):
            _refuse("category", "账户费用项目类型无效")
        if category == "commission":
            selected.append(rule)
        elif category in _COMPONENTS:
            if category in other_components:
                _refuse("fee_rules", "账户税费项目重复")
            other_components.add(category)
        else:
            _refuse("category", "账户含未支持费用项目，不能静默丢弃")
    if len(selected) != 1:
        _refuse("commission", "佣金配置缺失或同一方向有重复佣金")
    rule = selected[0]
    if "rate" not in rule or "minimum" not in rule:
        _refuse("commission", "佣金费率或最低收费缺失")
    if rule.get("base_measure", "gross_notional") != "gross_notional" or rule.get("rule_type", "simple_rate") != "simple_rate" or rule.get("charge_timing", "on_fill") != "on_fill":
        _refuse("commission", "此账户佣金条件需要D13显式选择适用配置")
    if decimal_input(rule.get("fixed_amount", "0")) != 0:
        _refuse("fixed_amount", "当前佣金契约不支持另加固定费用")
    included = included_components if included_components is not None else agreement.get("included_components")
    if not isinstance(included, Sequence) or isinstance(included, (str, bytes)) or len(included) > 4 or any(not isinstance(k, str) or k not in _COMPONENTS for k in included):
        _refuse("included_components", "佣金已包含项目必须明确声明，空列表也需显式提供")
    if len(set(included)) != len(included) or other_components.intersection(included):
        _refuse("included_components", "佣金已含项目与单列税费重复")
    # The existing saved rule is authoritative for its fee rounding convention;
    # unsupported legacy modes fail instead of becoming a new implicit default.
    scale = settlement_scale if settlement_scale is not None else agreement.get("settlement_scale")
    if scale is None and rule.get("rounding_precision") is not None and decimal_input(rule["rounding_precision"]) == decimal_input("0.01"):
        scale = 2
    mode = rounding if rounding is not None else agreement.get("rounding", rule.get("rounding_mode"))
    if isinstance(mode, str):
        mode = {"down": "toward_zero", "up": "away_from_zero"}.get(mode, mode)
    if isinstance(scale, bool) or not isinstance(scale, int) or not isinstance(mode, str) or mode not in {"half_even", "half_up", "toward_zero", "away_from_zero"}:
        _refuse("settlement", "费用结算精度及舍入方式缺失或不支持")
    schedule_key = fee_schedule.get("key")
    if not isinstance(schedule_key, str) or not schedule_key or len(schedule_key) > 100:
        _refuse("fee_schedule", "账户费用配置引用缺失或过长")
    config = {
        "commission_rate": str(decimal_input(rule["rate"])),
        "minimum_commission": str(decimal_input(rule["minimum"])),
        "currency": "CNY", "settlement_scale": scale, "rounding": mode,
        "included_components": list(included), "basis": f"saved-account:{schedule_key}:{side}",
    }
    if cost_overrides is not None and (not isinstance(cost_overrides, Mapping) or len(cost_overrides) > 2):
        _refuse("cost_overrides", "仅接受佣金费率及最低收费覆盖")
    try:
        config_json = json.dumps(config, ensure_ascii=False, allow_nan=False)
        override_json = json.dumps({} if cost_overrides is None else dict(cost_overrides), allow_nan=False)
    except (TypeError, ValueError):
        _refuse("cost_overrides", "佣金覆盖参数必须是有界十进制字符串配置")
    return json.loads(_native.validate_commission_config_json(config_json, override_json))
