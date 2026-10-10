//! Contract-driven simulated time. No wall clock or inferred holiday calendar.
use crate::types::{Nanoseconds, SessionKey};
use serde::{Deserialize, Serialize};

pub mod calendar;
pub mod merge;
pub mod schedule;

pub use calendar::{CalendarSession, SessionCalendar, TimeWindow, TradingPeriod};
pub use merge::{Checkpoint, EventSource, MergeLimits, StreamingMerge};
pub use schedule::{Cadence, CallbackId, Registration, ScheduleTime, SubscriptionKind};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradingSession {
    pub key: SessionKey,
    pub exchange_timezone: String,
    pub open_ns: Nanoseconds,
    pub close_ns: Nanoseconds,
}

/// The last *published* market key matters for same-nanosecond Tick reads.
/// Prefetched events are never part of this boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Visibility {
    pub now_ns: Nanoseconds,
    pub market_through: Option<crate::types::EventKey>,
}
impl Visibility {
    /// Completed history before the callback instant is readable even before
    /// this run's first event. At the current ns, require the publication cursor.
    pub fn contains_market(&self, key: &crate::types::EventKey) -> bool {
        key.time_ns < self.now_ns
            || key.time_ns == self.now_ns
                && self
                    .market_through
                    .as_ref()
                    .is_some_and(|visible| key <= visible)
    }
}

#[derive(Debug, Clone)]
pub struct SimClock {
    visible: Visibility,
}
impl SimClock {
    pub fn new(start: Nanoseconds) -> Self {
        Self {
            visible: Visibility {
                now_ns: start,
                market_through: None,
            },
        }
    }
    pub fn visibility(&self) -> &Visibility {
        &self.visible
    }
    pub fn advance(&mut self, now: Nanoseconds) -> crate::QfResult<()> {
        if now < self.visible.now_ns {
            return Err(crate::QfError::new(
                crate::ErrorCode::InvalidContract,
                "clock",
                "模拟时钟不能倒退",
            ));
        }
        self.visible.now_ns = now;
        Ok(())
    }
    pub fn publish(&mut self, key: crate::types::EventKey) -> crate::QfResult<()> {
        if self
            .visible
            .market_through
            .as_ref()
            .is_some_and(|last| &key <= last)
        {
            return Err(crate::QfError::new(
                crate::ErrorCode::InvalidContract,
                "clock",
                "市场事件键必须严格递增",
            ));
        }
        self.advance(key.time_ns)?;
        self.visible.market_through = Some(key);
        Ok(())
    }
}
