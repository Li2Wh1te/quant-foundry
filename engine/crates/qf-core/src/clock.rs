//! Session scheduling is implemented by D03; these are its shared inputs.
use crate::types::{Nanoseconds, SessionKey};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradingSession {
    pub key: SessionKey,
    pub exchange_timezone: String,
    pub open_ns: Nanoseconds,
    pub close_ns: Nanoseconds,
}
