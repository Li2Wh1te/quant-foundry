use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, fmt};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ErrorCode {
    CapabilityUnavailable,
    DataRestricted,
    DataChanged,
    LookaheadForbidden,
    RuleUnavailable,
    NumericRangeUnsupported,
    InsufficientCash,
    InsufficientSellable,
    InvalidOrder,
    InvalidRunConfig,
    InvalidContract,
    StrategyError,
    ResourceLimit,
    ResultBudgetExceeded,
    Cancelled,
    LegacyEngineRetired,
    IdempotencyConflict,
    StaleClaim,
}

impl ErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::CapabilityUnavailable => "CAPABILITY_UNAVAILABLE",
            Self::DataRestricted => "DATA_RESTRICTED",
            Self::DataChanged => "DATA_CHANGED",
            Self::LookaheadForbidden => "LOOKAHEAD_FORBIDDEN",
            Self::RuleUnavailable => "RULE_UNAVAILABLE",
            Self::NumericRangeUnsupported => "NUMERIC_RANGE_UNSUPPORTED",
            Self::InsufficientCash => "INSUFFICIENT_CASH",
            Self::InsufficientSellable => "INSUFFICIENT_SELLABLE",
            Self::InvalidOrder => "INVALID_ORDER",
            Self::InvalidRunConfig => "INVALID_RUN_CONFIG",
            Self::InvalidContract => "INVALID_CONTRACT",
            Self::StrategyError => "STRATEGY_ERROR",
            Self::ResourceLimit => "RESOURCE_LIMIT",
            Self::ResultBudgetExceeded => "RESULT_BUDGET_EXCEEDED",
            Self::Cancelled => "CANCELLED",
            Self::LegacyEngineRetired => "LEGACY_ENGINE_RETIRED",
            Self::IdempotencyConflict => "IDEMPOTENCY_CONFLICT",
            Self::StaleClaim => "STALE_CLAIM",
        }
    }
}

/// Safe diagnostic context contains public scope only, never paths/DSNs/tokens/SQL.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QfError {
    pub code: ErrorCode,
    pub operation: String,
    pub message: String,
    pub scope: BTreeMap<String, String>,
}

impl QfError {
    pub fn new(code: ErrorCode, operation: &str, message: &str) -> Self {
        Self {
            code,
            operation: operation.into(),
            message: message.into(),
            scope: BTreeMap::new(),
        }
    }
}

impl fmt::Display for QfError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}
impl std::error::Error for QfError {}
pub type QfResult<T> = Result<T, QfError>;
