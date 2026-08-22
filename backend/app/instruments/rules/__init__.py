"""Versioned instrument rule packages: contracts, registry, and resolver.

This subpackage is pure domain logic.  It must never import ORM models,
database sessions, FastAPI, Tushare, or concrete data-source clients.
"""

from app.instruments.rules.contracts import (
    CAPABILITY_DIMENSIONS,
    FactQualityStatus,
    ParseMode,
    ResolvedFactSummary,
    ResolutionStatus,
    RuleExceptionEntry,
    RuleExceptionPolicy,
    RuleExceptionSetDefinition,
    RuleFactCandidate,
    RuleFieldDefinition,
    RuleFieldType,
    RulePackageDefinition,
    RulePackageIssue,
    RulePackageIssueCode,
    RulePackageResolution,
    StrategyRuleDeclaration,
    TradingStatusRequirement,
    canonical_decimal_string,
    stable_hash,
)
from app.instruments.rules.registry import (
    RulePackageNotRegisteredError,
    RulePackageRegistrationError,
    RulePackageRegistry,
)
from app.instruments.rules.resolver import PARSER_REVISION, RulePackageResolver
from app.instruments.rules.etf_china import (
    FORMAL_SETTLEMENT_RULE_CLASSES,
    KNOWN_SETTLEMENT_RULE_CLASSES,
    PACKAGE_KEY,
    PACKAGE_VERSION,
    PARSE_ORDER,
    build_definition,
    register_china_listed_etf_rules,
)

__all__ = [
    "CAPABILITY_DIMENSIONS",
    "FORMAL_SETTLEMENT_RULE_CLASSES",
    "KNOWN_SETTLEMENT_RULE_CLASSES",
    "PARSER_REVISION",
    "PACKAGE_KEY",
    "PACKAGE_VERSION",
    "PARSE_ORDER",
    "FactQualityStatus",
    "ParseMode",
    "ResolvedFactSummary",
    "ResolutionStatus",
    "RuleExceptionEntry",
    "RuleExceptionPolicy",
    "RuleExceptionSetDefinition",
    "RuleFactCandidate",
    "RuleFieldDefinition",
    "RuleFieldType",
    "RulePackageDefinition",
    "RulePackageIssue",
    "RulePackageIssueCode",
    "RulePackageNotRegisteredError",
    "RulePackageRegistrationError",
    "RulePackageRegistry",
    "RulePackageResolution",
    "RulePackageResolver",
    "StrategyRuleDeclaration",
    "TradingStatusRequirement",
    "build_definition",
    "canonical_decimal_string",
    "register_china_listed_etf_rules",
    "stable_hash",
]
