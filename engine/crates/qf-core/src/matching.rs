use crate::QfResult;
use crate::orders::{Order, Side};
use crate::types::{
    ExactDecimal, MarketEvent, Nanoseconds, Price, Quantity, QuantityStep, SecurityKey,
};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fill {
    pub trade_id: String,
    pub order_id: String,
    pub security: SecurityKey,
    pub side: Side,
    pub quantity: Quantity,
    pub price: Price,
    pub fee: ExactDecimal,
    pub execution_time_ns: Nanoseconds,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradePage {
    pub items: Vec<Fill>,
    pub next_cursor: Option<String>,
}
impl TradePage {
    pub fn validate(&self) -> QfResult<()> {
        crate::results::validate_page(self.items.len(), self.next_cursor.as_deref())
    }
}
pub struct MatchOutcome {
    pub fills: Vec<Fill>,
    pub orders: Vec<Order>,
}
/// D06/D09's read-only candidate-price check. D07/D08 choose liquidity, price,
/// and the fill unit; D06 never implements a second matching algorithm.
pub trait FillBudget {
    fn allowance(
        &self,
        order_id: &str,
        requested: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance>;
    /// Preview earlier fills chosen in this same event through the one account.
    /// No cash is committed or allocated twice by independent candidate quotes.
    fn allowance_after(
        &self,
        prior: &[Fill],
        order_id: &str,
        requested: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        if !prior.is_empty() {
            return Err(crate::QfError::new(
                crate::ErrorCode::CapabilityUnavailable,
                "fill_budget",
                "累计候选账户预算未接入",
            ));
        }
        self.allowance(order_id, requested, price, step)
    }
    /// Stable run-local IDs, in emitted fill order. No growing replay ledger.
    fn trade_id(&self, offset: usize) -> QfResult<String>;
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FillAllowance {
    pub requested_quantity: Quantity,
    pub effective_quantity: Quantity,
    pub reason_code: Option<crate::ErrorCode>,
    pub message: String,
}
/// D07/D08 own matching; this port neither fetches data nor persists results.
pub trait Matcher {
    type Rules;
    fn consume(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &Self::Rules,
    ) -> QfResult<MatchOutcome>;
    /// Existing isolated adapters remain source compatible. Real D07/D08 must
    /// consult the supplied account budget and allocate shared liquidity once.
    fn consume_with_budget(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &Self::Rules,
        _budget: &dyn FillBudget,
    ) -> QfResult<MatchOutcome> {
        self.consume(event, orders, rules)
    }
}
