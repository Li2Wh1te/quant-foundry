use super::CalendarSession;
use crate::run::Frequency;
use crate::types::{Nanoseconds, SecurityKey};
use crate::{ErrorCode, QfError, QfResult};
use std::collections::BTreeSet;

pub const MAX_REGISTRATIONS: usize = 1024;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallbackId(String);
impl CallbackId {
    pub fn new(value: impl Into<String>) -> QfResult<Self> {
        let value = value.into();
        crate::types::keys::label(&value, 128)?;
        Ok(Self(value))
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cadence {
    Daily,
    Weekly(i16),
    Monthly(i16),
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScheduleTime {
    BeforeOpen,
    AfterClose,
    LocalMinute(u16),
}
impl std::str::FromStr for ScheduleTime {
    type Err = QfError;
    fn from_str(value: &str) -> QfResult<Self> {
        match value {
            "before_open" => Ok(Self::BeforeOpen),
            "after_close" => Ok(Self::AfterClose),
            _ => {
                let bytes = value.as_bytes();
                if bytes.len() != 5
                    || bytes[2] != b':'
                    || ![bytes[0], bytes[1], bytes[3], bytes[4]]
                        .iter()
                        .all(u8::is_ascii_digit)
                {
                    return Err(invalid("定时时间必须为before_open、after_close或HH:MM"));
                }
                let hour = u16::from(bytes[0] - b'0') * 10 + u16::from(bytes[1] - b'0');
                let minute = u16::from(bytes[3] - b'0') * 10 + u16::from(bytes[4] - b'0');
                if hour >= 24 || minute >= 60 {
                    return Err(invalid("本地时间越界"));
                }
                Ok(Self::LocalMinute(hour * 60 + minute))
            }
        }
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubscriptionKind {
    Bar,
    Tick,
}
#[derive(Debug, Clone)]
struct Registered {
    callback: CallbackId,
    cadence: Cadence,
    time: ScheduleTime,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScheduledCall {
    pub callback: CallbackId,
    pub time_ns: Nanoseconds,
    pub time: ScheduleTime,
    pub registration_index: usize,
}
#[derive(Debug, Clone)]
pub struct Registration {
    universe: BTreeSet<SecurityKey>,
    frequency: Frequency,
    callbacks: Vec<Registered>,
    // After any explicit subscribe, only the explicit sets are used.
    bars: Option<BTreeSet<SecurityKey>>,
    ticks: Option<BTreeSet<SecurityKey>>,
    subscription_calls: usize,
    sealed: bool,
}
fn invalid(message: &str) -> QfError {
    QfError::new(ErrorCode::InvalidContract, "strategy_registration", message)
}
fn limited() -> QfError {
    QfError::new(
        ErrorCode::ResourceLimit,
        "strategy_registration",
        "策略注册数量超过边界",
    )
}
impl Registration {
    pub fn new(universe: &[SecurityKey], frequency: Frequency) -> QfResult<Self> {
        let unique: BTreeSet<_> = universe.iter().cloned().collect();
        if unique.is_empty() || unique.len() != universe.len() || unique.len() > 10_000 {
            return Err(invalid("授权标的集合为空、重复或超限"));
        }
        Ok(Self {
            universe: unique,
            frequency,
            callbacks: Vec::new(),
            bars: None,
            ticks: None,
            subscription_calls: 0,
            sealed: false,
        })
    }
    fn mutable(&self) -> QfResult<()> {
        if self.sealed {
            Err(invalid("调度和订阅只能在initialize注册"))
        } else {
            Ok(())
        }
    }
    pub fn register(
        &mut self,
        callback: CallbackId,
        cadence: Cadence,
        time: ScheduleTime,
    ) -> QfResult<()> {
        self.mutable()?;
        if self.callbacks.len() == MAX_REGISTRATIONS {
            return Err(limited());
        }
        if matches!(cadence, Cadence::Weekly(n) | Cadence::Monthly(n) if n != -1 && n <= 0) {
            return Err(invalid("交易日参数仅支持正N或-1最后交易日"));
        }
        if let ScheduleTime::LocalMinute(minute) = time {
            if minute >= 1440 {
                return Err(invalid("本地时间越界"));
            }
            if self.frequency == Frequency::Day {
                return Err(QfError::new(
                    ErrorCode::CapabilityUnavailable,
                    "strategy_registration",
                    "日线不支持盘中精确定时",
                ));
            }
        }
        // Repeated registrations intentionally remain repeated calls.
        self.callbacks.push(Registered {
            callback,
            cadence,
            time,
        });
        Ok(())
    }
    pub fn subscribe(
        &mut self,
        securities: &[SecurityKey],
        kind: SubscriptionKind,
    ) -> QfResult<()> {
        self.mutable()?;
        if self.subscription_calls == MAX_REGISTRATIONS {
            return Err(limited());
        }
        if securities.len() > 10_000 || securities.iter().any(|s| !self.universe.contains(s)) {
            return Err(invalid("订阅必须在run授权集合内"));
        }
        let subscription = match kind {
            SubscriptionKind::Bar => &mut self.bars,
            SubscriptionKind::Tick => &mut self.ticks,
        };
        subscription
            .get_or_insert_with(BTreeSet::new)
            .extend(securities.iter().cloned());
        self.subscription_calls += 1;
        Ok(())
    }
    pub fn seal(&mut self) {
        self.sealed = true;
    }
    pub fn subscribed(&self, security: &SecurityKey, kind: SubscriptionKind) -> bool {
        let set = match kind {
            SubscriptionKind::Bar => &self.bars,
            SubscriptionKind::Tick => &self.ticks,
        };
        set.as_ref().map_or(
            self.subscription_calls == 0 && self.universe.contains(security),
            |set| set.contains(security),
        )
    }
    pub fn authorized(&self, security: &SecurityKey) -> bool {
        self.universe.contains(security)
    }
    pub fn for_session(&self, session: &CalendarSession) -> QfResult<Vec<ScheduledCall>> {
        let mut calls = Vec::with_capacity(self.callbacks.len());
        for (registration_index, item) in self.callbacks.iter().enumerate() {
            let period = match item.cadence {
                Cadence::Daily => None,
                Cadence::Weekly(n) => Some((n, &session.week)),
                Cadence::Monthly(n) => Some((n, &session.month)),
            };
            let matches = period.is_none_or(|(n, p)| {
                if n == -1 {
                    p.trading_day == p.total_trading_days
                } else {
                    p.trading_day == n as u16
                }
            });
            // Validate precision even when this particular N does not occur.
            let time_ns = match item.time {
                ScheduleTime::BeforeOpen => session.before_open_ns,
                ScheduleTime::AfterClose => session.after_close_ns,
                ScheduleTime::LocalMinute(minute) => {
                    let time = session.local_time(minute)?;
                    let windows = if self.frequency == Frequency::Tick {
                        &session.market_windows
                    } else {
                        &session.bar_windows
                    };
                    if !windows.iter().any(|w| w.contains(time)) {
                        return Err(QfError::new(
                            ErrorCode::CapabilityUnavailable,
                            "strategy_registration",
                            "本地定时不在声明交易窗口内（含午休）",
                        ));
                    }
                    if self.frequency != Frequency::Tick
                        && !session
                            .buckets(self.frequency)?
                            .any(|b| b.start_ns == time || b.end_ns == time)
                    {
                        return Err(QfError::new(
                            ErrorCode::CapabilityUnavailable,
                            "strategy_registration",
                            "定时时点精度与输入Bar频率不兼容",
                        ));
                    }
                    time
                }
            };
            if matches {
                calls.push(ScheduledCall {
                    callback: item.callback.clone(),
                    time_ns,
                    time: item.time,
                    registration_index,
                });
            }
        }
        calls.sort_by_key(|call| (call.time_ns, call.registration_index));
        Ok(calls)
    }
}
