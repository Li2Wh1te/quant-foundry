use crate::types::{EventKey, ExactDecimal, Nanoseconds, Price, Quantity, SecurityKey, SessionKey};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};

mod execution;
mod manager;
pub use manager::*;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    Buy,
    Sell,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TimeInForce {
    Day,
    Gtc,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum OrderStyle {
    Market,
    Limit { price: Price },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum IntentValue {
    /// Absolute nonnegative quantity; direction is explicit.
    Quantity(Quantity),
    Value(ExactDecimal),
    TargetQuantity(Quantity),
    TargetValue(ExactDecimal),
    TargetPercent(ExactDecimal),
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderIntent {
    pub security: SecurityKey,
    pub side: Side,
    pub value: IntentValue,
    pub style: OrderStyle,
    pub tif: TimeInForce,
    pub submitted_at: EventKey,
}
impl OrderIntent {
    pub fn validate(&self) -> QfResult<()> {
        let valid = match self.value {
            IntentValue::Value(v) | IntentValue::TargetValue(v) => !v.is_negative(),
            IntentValue::TargetPercent(v) => (ExactDecimal::ZERO..=ExactDecimal::ONE).contains(&v),
            _ => true,
        };
        if !valid {
            return Err(QfError::new(
                ErrorCode::InvalidOrder,
                "order",
                "目标价值或比例超出范围",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderStatus {
    Accepted,
    Open,
    PartiallyFilled,
    Filled,
    Cancelled,
    Expired,
    Rejected,
}
impl OrderStatus {
    pub fn is_active(self) -> bool {
        matches!(self, Self::Accepted | Self::Open | Self::PartiallyFilled)
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Order {
    pub order_id: String,
    pub security: SecurityKey,
    pub side: Side,
    pub quantity: Quantity,
    pub filled_quantity: Quantity,
    pub status: OrderStatus,
    pub submitted_ns: Nanoseconds,
    /// Actual status-change boundary, including the complete market/command key.
    #[serde(default)]
    pub updated_at: Option<Box<EventKey>>,
    pub limit_price: Option<Price>,
    pub tif: TimeInForce,
    pub effective_session: SessionKey,
    pub eligible_interval_start: Option<Nanoseconds>,
    pub eligible_after_event: Option<EventKey>,
    pub reason_code: Option<ErrorCode>,
    pub message: String,
}
impl Order {
    pub fn remaining(&self) -> QfResult<Quantity> {
        self.quantity.checked_sub(self.filled_quantity)
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderResult {
    pub accepted: bool,
    pub order_id: Option<String>,
    pub reason_code: Option<ErrorCode>,
    pub message: String,
    pub unchanged: bool,
    pub requested_quantity: Option<Quantity>,
    pub effective_quantity: Option<Quantity>,
}
