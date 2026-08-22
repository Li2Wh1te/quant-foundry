"""Definition and registration helper for ``china_listed_etf_rules@1``.

This module only *declares* the v1 contract: field definitions, the
capability schema, known versus formally supported settlement classes,
the named-exception policy, and the fixed parse order.  It contains no
production numbers — every ``lot_size``, ``price_tick``, currency, and
session template must come from facts at resolution time.
"""

from __future__ import annotations

from app.instruments.domain import VersionedReference
from app.instruments.rules.contracts import (
    CAPABILITY_DIMENSIONS,
    RuleExceptionPolicy,
    RuleFieldDefinition,
    RuleFieldType,
    RulePackageDefinition,
)
from app.instruments.rules.registry import RulePackageRegistry

PACKAGE_KEY = "china_listed_etf_rules"
PACKAGE_VERSION = 1

#: Settlement classes the parser can recognize as stable machine values.
KNOWN_SETTLEMENT_RULE_CLASSES: frozenset[str] = frozenset(
    {"t1_before_open_match", "same_day", "t_plus_1"}
)

#: The only settlement class formally supported in Phase 1.
FORMAL_SETTLEMENT_RULE_CLASSES: frozenset[str] = frozenset(
    {"t1_before_open_match"}
)

#: Fixed parse order; participates in the package semantic hash.
PARSE_ORDER: tuple[str, ...] = (
    "load_package_exact_key_version",
    "validate_asset_class",
    "validate_fact_package_reference",
    "select_normal_fact_by_validity_and_cutoff",
    "locate_named_exception_by_instrument_and_interval",
    "select_exception_fact_by_reference",
    "overlay_exception_fields_on_normal_fact",
    "check_required_fields_present",
    "validate_types_precision_multiples_and_strategy_refs",
    "validate_capability_declaration_explicitness",
    "validate_settlement_class_known",
    "block_known_but_not_formally_supported_settlement_class",
    "validate_source_quality_and_mode_gate",
    "build_immutable_resolution_summary_and_hash",
)


def build_definition() -> RulePackageDefinition:
    """Build the immutable ``china_listed_etf_rules@1`` definition."""

    fields = (
        RuleFieldDefinition("lot_size", RuleFieldType.POSITIVE_DECIMAL, True),
        RuleFieldDefinition(
            "quantity_precision", RuleFieldType.NON_NEGATIVE_INT, True
        ),
        RuleFieldDefinition(
            "price_precision", RuleFieldType.NON_NEGATIVE_INT, True
        ),
        RuleFieldDefinition("price_tick", RuleFieldType.POSITIVE_DECIMAL, True),
        RuleFieldDefinition(
            "contract_multiplier", RuleFieldType.POSITIVE_DECIMAL, True
        ),
        RuleFieldDefinition(
            "trading_session_template",
            RuleFieldType.VERSIONED_REFERENCE,
            True,
        ),
        RuleFieldDefinition(
            "settlement_rule_class", RuleFieldType.SETTLEMENT_CLASS, True
        ),
        RuleFieldDefinition("sellable_rule", RuleFieldType.STRATEGY_RULE, True),
        RuleFieldDefinition("fee_categories", RuleFieldType.STRING_SET, True),
        RuleFieldDefinition(
            "trading_status_applicability",
            RuleFieldType.TRADING_STATUS_APPLICABILITY,
            True,
        ),
        RuleFieldDefinition("currency", RuleFieldType.CURRENCY_CODE, True),
        RuleFieldDefinition("order_types", RuleFieldType.STRING_SET, True),
        RuleFieldDefinition(
            "minimum_order_quantity", RuleFieldType.POSITIVE_DECIMAL, True
        ),
        RuleFieldDefinition(
            "price_limit_rule", RuleFieldType.STRATEGY_RULE, True
        ),
        RuleFieldDefinition(
            "cash_availability_rule", RuleFieldType.STRATEGY_RULE, True
        ),
        RuleFieldDefinition(
            "position_availability_rule", RuleFieldType.STRATEGY_RULE, True
        ),
    )
    return RulePackageDefinition(
        reference=VersionedReference(key=PACKAGE_KEY, version=PACKAGE_VERSION),
        supported_asset_classes=frozenset({"etf"}),
        field_definitions=fields,
        capability_schema=CAPABILITY_DIMENSIONS,
        known_settlement_rule_classes=KNOWN_SETTLEMENT_RULE_CLASSES,
        formal_settlement_rule_classes=FORMAL_SETTLEMENT_RULE_CLASSES,
        exception_policy=RuleExceptionPolicy(
            allowed_match_keys=("instrument_id", "validity_interval"),
        ),
        parse_order=PARSE_ORDER,
    )


def register_china_listed_etf_rules(
    registry: RulePackageRegistry,
) -> RulePackageDefinition:
    """Register ``china_listed_etf_rules@1`` into ``registry`` exactly once."""

    definition = build_definition()
    registry.register(definition)
    return definition
