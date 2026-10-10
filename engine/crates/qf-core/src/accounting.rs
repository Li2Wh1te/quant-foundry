use crate::QfResult;
use crate::matching::Fill;
use crate::orders::{OrderIntent, OrderResult};
use crate::types::{Money, Nanoseconds, Quantity, SecurityKey, SessionKey};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

mod actions;
mod ledger;
mod terms;
mod valuation;
pub use actions::*;
pub use ledger::*;
pub use terms::*;
pub use valuation::*;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PositionView {
    pub security: SecurityKey,
    pub quantity: Quantity,
    pub sellable: Quantity,
    pub frozen: Quantity,
    pub cost_basis_total: Money,
    pub average_cost: Money,
    pub market_value: Option<Money>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountView {
    /// Includes frozen_cash: cash = available_cash + frozen_cash.
    pub cash: Money,
    pub available_cash: Money,
    pub frozen_cash: Money,
    pub receivables: Money,
    pub total_value: Option<Money>,
    pub positions: BTreeMap<SecurityKey, PositionView>,
}
/// D09 holds the only mutable account state. D01 supplies no accounting engine.
pub trait AccountPort {
    fn validate_and_reserve(&mut self, intent: &OrderIntent) -> QfResult<OrderResult>;
    /// D09 assesses fees on the authoritative order accumulator before D03
    /// applies/notifies/persists a fill. The default preserves older adapters.
    /// Assessment may change only fee; it never commits money or quantity.
    fn assess_fill(&self, fill: &Fill) -> QfResult<Fill> {
        Ok(fill.clone())
    }
    fn apply_fill(&mut self, fill: &Fill) -> QfResult<()>;
    fn release(&mut self, order_id: &str) -> QfResult<()>;
    fn settle(&mut self, session: &SessionKey) -> QfResult<()>;
    fn value(&self, now: Nanoseconds) -> QfResult<AccountView>;
}
