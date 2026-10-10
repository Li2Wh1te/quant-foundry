use crate::QfResult;
use crate::orders::{Order, Side};
use crate::types::{ExactDecimal, MarketEvent, Nanoseconds, Price, Quantity, SecurityKey};
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
/// D07/D08 own matching; this port neither fetches data nor persists results.
pub trait Matcher {
    type Rules;
    fn consume(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &Self::Rules,
    ) -> QfResult<MatchOutcome>;
}
