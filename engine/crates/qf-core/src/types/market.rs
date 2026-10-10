use super::{EventIdentity, Nanoseconds, Price, Quantity, SecurityKey, SessionKey};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Currency {
    CNY,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QuantityUnit {
    Shares,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarketUnits {
    pub price_currency: Currency,
    pub quantity_unit: QuantityUnit,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bar {
    pub security: SecurityKey,
    pub session: SessionKey,
    pub identity: EventIdentity,
    pub interval_start_ns: Nanoseconds,
    pub interval_end_ns: Nanoseconds,
    pub open: Price,
    pub high: Price,
    pub low: Price,
    pub close: Price,
    pub quantity: Option<Quantity>,
    pub units: MarketUnits,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradeTick {
    pub security: SecurityKey,
    pub session: SessionKey,
    pub identity: EventIdentity,
    pub time_ns: Nanoseconds,
    pub price: Price,
    pub quantity: Quantity,
    pub units: MarketUnits,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuoteTick {
    pub security: SecurityKey,
    pub session: SessionKey,
    pub identity: EventIdentity,
    pub time_ns: Nanoseconds,
    pub bid: Option<Price>,
    pub ask: Option<Price>,
    pub bid_quantity: Option<Quantity>,
    pub ask_quantity: Option<Quantity>,
    pub units: MarketUnits,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    content = "event",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum MarketEvent {
    Bar(Bar),
    TradeTick(TradeTick),
    QuoteTick(QuoteTick),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventPhase {
    Settlement,
    Market,
    Notification,
    Callback,
}

/// Timestamp is a sort component, never the event identity by itself.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventKey {
    pub time_ns: Nanoseconds,
    pub phase: EventPhase,
    pub security: SecurityKey,
    pub identity: EventIdentity,
}

impl MarketEvent {
    pub fn key(&self) -> EventKey {
        let (time_ns, security, identity) = match self {
            Self::Bar(e) => (e.interval_end_ns, &e.security, &e.identity),
            Self::TradeTick(e) => (e.time_ns, &e.security, &e.identity),
            Self::QuoteTick(e) => (e.time_ns, &e.security, &e.identity),
        };
        EventKey {
            time_ns,
            phase: EventPhase::Market,
            security: security.clone(),
            identity: identity.clone(),
        }
    }
    pub fn validate(&self) -> QfResult<()> {
        match self {
            Self::Bar(b)
                if b.interval_start_ns >= b.interval_end_ns
                    || b.low > b.high
                    || b.open < b.low
                    || b.open > b.high
                    || b.close < b.low
                    || b.close > b.high =>
            {
                Err(QfError::new(
                    ErrorCode::InvalidContract,
                    "market_event",
                    "Bar区间或OHLC边界无效",
                ))
            }
            Self::QuoteTick(q)
                if (q.bid.is_none() && q.bid_quantity.is_some())
                    || (q.ask.is_none() && q.ask_quantity.is_some()) =>
            {
                Err(QfError::new(
                    ErrorCode::InvalidContract,
                    "market_event",
                    "盘口数量必须关联对应方向价格",
                ))
            }
            _ => Ok(()),
        }
    }
}
