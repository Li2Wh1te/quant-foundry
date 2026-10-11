pub mod error;
pub mod keys;
pub mod market;
pub mod numeric;

pub use error::{ErrorCode, QfError, QfResult};
pub use keys::{EventIdentity, Nanoseconds, SecurityKey, Sequence, SessionKey};
pub use market::{Bar, EventKey, EventPhase, MarketEvent, QuoteTick, TradeTick};
pub use numeric::{ExactDecimal, Money, Price, Quantity, QuantityStep, RoundingPolicy};

/// An omitted optional field differs from an explicitly invalid null value.
pub(crate) fn optional_non_null<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: serde::Deserialize<'de>,
{
    T::deserialize(d).map(Some)
}
