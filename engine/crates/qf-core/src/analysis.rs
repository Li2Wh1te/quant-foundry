use crate::types::{Money, Nanoseconds};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EquityPoint {
    pub time_ns: Nanoseconds,
    pub equity: Option<Money>,
    pub unavailable_reason: Option<String>,
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
