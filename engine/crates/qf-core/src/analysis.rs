use crate::types::{Money, Nanoseconds};
use serde::{Deserialize, Serialize};

pub mod indicators;
pub mod performance;
pub use performance::*;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EquityPoint {
    pub time_ns: Nanoseconds,
    pub session: Option<crate::types::SessionKey>,
    pub sampling: String,
    pub equity: Option<Money>,
    pub unavailable_reason: Option<String>,
    pub net_value: Metric,
    pub session_return: Metric,
    pub cumulative_return: Metric,
    pub drawdown: Metric,
    pub benchmark_value: Option<crate::types::ExactDecimal>,
    pub benchmark_cumulative_return: Metric,
    pub cumulative_excess_return: Metric,
}
/// D11 implements formulas; unavailable metrics carry their reason.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum Metric {
    Available {
        value: crate::results::FiniteStatistic,
    },
    Unavailable {
        reason: String,
    },
}
