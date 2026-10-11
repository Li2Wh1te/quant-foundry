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
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub enum Metric {
    Available {
        value: crate::results::FiniteStatistic,
    },
    Unavailable {
        reason: String,
    },
}
impl<'de> Deserialize<'de> for Metric {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        // Deserialize the float directly, without serde's internally-tagged
        // Content buffer (which maps arbitrary_precision numbers to objects).
        // Track presence separately so a wrong variant's null field is refused.
        #[derive(Default)]
        struct Field(Option<Box<serde_json::value::RawValue>>);
        impl<'de> Deserialize<'de> for Field {
            fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                Box::<serde_json::value::RawValue>::deserialize(d).map(|v| Self(Some(v)))
            }
        }
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            status: String,
            #[serde(default)]
            value: Field,
            #[serde(default)]
            reason: Field,
        }
        let wire = Wire::deserialize(deserializer)?;
        match (wire.status.as_str(), wire.value.0, wire.reason.0) {
            ("available", Some(value), None) => serde_json::from_str(value.get())
                .map(|value| Self::Available { value })
                .map_err(serde::de::Error::custom),
            ("unavailable", None, Some(reason)) => serde_json::from_str(reason.get())
                .map(|reason| Self::Unavailable { reason })
                .map_err(serde::de::Error::custom),
            _ => Err(serde::de::Error::custom("invalid metric variant")),
        }
    }
}
