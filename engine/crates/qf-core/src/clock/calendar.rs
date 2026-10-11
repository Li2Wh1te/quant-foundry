//! Small authoritative calendar contracts, not holiday/weekend inference.
use super::TradingSession;
use crate::rules::{
    date::RuleDate,
    market::{AuctionPhase, SessionTemplate},
};
use crate::run::Frequency;
use crate::types::{MarketEvent, Nanoseconds, SessionKey};
use crate::{ErrorCode, QfError, QfResult};
use std::collections::BTreeSet;

pub const MINUTE_NS: i64 = 60_000_000_000;
pub const MAX_CALENDAR_SESSIONS: usize = 20_000;
pub const MAX_SESSION_WINDOWS: usize = 16;

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TimeWindow {
    pub start_ns: Nanoseconds,
    pub end_ns: Nanoseconds,
}
impl TimeWindow {
    pub fn contains(&self, time: Nanoseconds) -> bool {
        self.start_ns <= time && time <= self.end_ns
    }
}
/// Ordinals include the *whole* exchange trading week/month, even if this run
/// starts mid-period. D04 must supply them; a truncated run is not a calendar.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradingPeriod {
    pub id: String,
    pub trading_day: u16,
    pub total_trading_days: u16,
}
impl TradingPeriod {
    fn validate(&self, max: u16) -> QfResult<()> {
        crate::types::keys::label(&self.id, 128)?;
        if self.trading_day == 0
            || self.trading_day > self.total_trading_days
            || self.total_trading_days > max
        {
            return Err(invalid("周月交易日序号缺失或越界"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalendarSession {
    pub session: TradingSession,
    pub date: RuleDate,
    /// Supplied UTC instant for exchange-local midnight. No guessed UTC offset.
    pub local_midnight_ns: Nanoseconds,
    pub before_open_ns: Nanoseconds,
    pub after_close_ns: Nanoseconds,
    pub market_windows: Vec<TimeWindow>,
    /// Bar buckets may join adjacent continuous/closing-call phases, never lunch.
    pub bar_windows: Vec<TimeWindow>,
    pub week: TradingPeriod,
    pub month: TradingPeriod,
}
fn invalid(message: &str) -> QfError {
    QfError::new(ErrorCode::RuleUnavailable, "calendar", message)
}
fn at(midnight: Nanoseconds, minute: u16) -> QfResult<Nanoseconds> {
    midnight
        .get()
        .checked_add(i64::from(minute) * MINUTE_NS)
        .map(Nanoseconds::new)
        .ok_or_else(|| invalid("会话时间超出纳秒范围"))
}
fn validate_windows(windows: &[TimeWindow], session: &TradingSession) -> QfResult<()> {
    if windows.is_empty() || windows.len() > MAX_SESSION_WINDOWS {
        return Err(invalid("交易窗口缺失或超限"));
    }
    for (i, window) in windows.iter().enumerate() {
        if window.start_ns >= window.end_ns
            || window.start_ns < session.open_ns
            || window.end_ns > session.close_ns
            || i > 0 && windows[i - 1].end_ns >= window.start_ns
        {
            return Err(invalid("会话窗口重叠、反序或越界"));
        }
    }
    Ok(())
}
impl CalendarSession {
    pub fn validate(&self) -> QfResult<()> {
        crate::types::keys::label(&self.session.exchange_timezone, 128)?;
        let day_end = at(self.local_midnight_ns, 1440)?;
        if self.before_open_ns < self.local_midnight_ns
            || self.before_open_ns >= self.session.open_ns
            || self.session.open_ns >= self.session.close_ns
            || self.after_close_ns < self.session.close_ns
            || self.after_close_ns >= day_end
        {
            return Err(invalid("盘前、开闭市或盘后边界无效"));
        }
        validate_windows(&self.market_windows, &self.session)?;
        validate_windows(&self.bar_windows, &self.session)?;
        for bar in &self.bar_windows {
            if !self
                .market_windows
                .iter()
                .any(|market| market.start_ns <= bar.start_ns && bar.end_ns <= market.end_ns)
            {
                return Err(invalid("Bar窗口不在声明交易窗口内"));
            }
        }
        self.week.validate(7)?;
        self.month.validate(31)?;
        if self.month.id != self.date.as_str()[..7] {
            return Err(invalid("月交易日序号与日期不一致"));
        }
        Ok(())
    }
    /// Optional join of the D02 dated phase template with the D04 calendar's
    /// UTC/local mapping. This never invents a holiday or an effective rule.
    pub fn with_template(mut self, template: SessionTemplate) -> QfResult<Self> {
        if self.session.exchange_timezone != template.timezone() {
            return Err(invalid("交易时区与已选择规则模板冲突"));
        }
        let mut bars = template.continuous_windows().to_vec();
        if let Some((start, end)) = template.closing_call() {
            let last = bars
                .last_mut()
                .ok_or_else(|| invalid("模板没有连续交易窗口"))?;
            if last.1 != start {
                return Err(invalid("收盘集合竞价与连续阶段不相接"));
            }
            last.1 = end;
        }
        let window = |(start, end)| -> QfResult<TimeWindow> {
            Ok(TimeWindow {
                start_ns: at(self.local_midnight_ns, start)?,
                end_ns: at(self.local_midnight_ns, end)?,
            })
        };
        self.bar_windows = bars.iter().copied().map(window).collect::<QfResult<_>>()?;
        self.market_windows = std::iter::once(template.opening_call())
            .chain(bars)
            .map(window)
            .collect::<QfResult<_>>()?;
        self.validate()?;
        Ok(self)
    }
    pub fn local_time(&self, minute: u16) -> QfResult<Nanoseconds> {
        if minute >= 1440 {
            return Err(invalid("本地时间必须在当日范围"));
        }
        at(self.local_midnight_ns, minute)
    }
    /// Join the instrument's D02 dated template to the authoritative calendar.
    /// At an adjacent boundary the later phase wins. Temporary-halt auctions
    /// require actual status facts in RulesPort and must override this schedule.
    /// A reference Bar window alone cannot distinguish stock/fund auction phases.
    pub fn scheduled_phase_at(
        &self,
        template: SessionTemplate,
        time: Nanoseconds,
    ) -> QfResult<AuctionPhase> {
        if self.session.exchange_timezone != template.timezone()
            || !self
                .market_windows
                .iter()
                .any(|window| window.contains(time))
        {
            return Err(invalid("事件时点或时区不在权威交易窗口内"));
        }
        let contains = |(start, end)| -> QfResult<bool> {
            Ok(at(self.local_midnight_ns, start)? <= time
                && time <= at(self.local_midnight_ns, end)?)
        };
        if let Some(window) = template.closing_call()
            && contains(window)?
        {
            return Ok(AuctionPhase::ClosingCall);
        }
        if contains(template.opening_call())? {
            return Ok(AuctionPhase::OpeningCall);
        }
        for window in template.continuous_windows() {
            if contains(*window)? {
                return Ok(AuctionPhase::Continuous);
            }
        }
        Err(invalid("该时点没有适用模板的交易阶段"))
    }
    pub fn buckets(&self, frequency: Frequency) -> QfResult<BucketIter<'_>> {
        let duration = match frequency {
            Frequency::Minute => MINUTE_NS,
            Frequency::FiveMinutes => 5 * MINUTE_NS,
            Frequency::FifteenMinutes => 15 * MINUTE_NS,
            Frequency::ThirtyMinutes => 30 * MINUTE_NS,
            Frequency::Hour => 60 * MINUTE_NS,
            _ => {
                return Err(QfError::new(
                    ErrorCode::CapabilityUnavailable,
                    "bar_buckets",
                    "此频率不使用分钟窗口分桶",
                ));
            }
        };
        Ok(BucketIter {
            windows: &self.bar_windows,
            window: 0,
            start: None,
            duration,
        })
    }
    pub fn validate_event(&self, event: &MarketEvent, frequency: Frequency) -> QfResult<()> {
        event.validate()?;
        let (session, time) = match event {
            MarketEvent::Bar(bar) => {
                if frequency == Frequency::Day {
                    let first = self
                        .bar_windows
                        .first()
                        .ok_or_else(|| invalid("日Bar缺少声明交易窗口"))?;
                    if bar.interval_start_ns < self.session.open_ns
                        || bar.interval_start_ns > first.start_ns
                        || bar.interval_end_ns != self.session.close_ns
                    {
                        return Err(invalid("日Bar必须覆盖声明会话并在收盘完成"));
                    }
                } else if !self.buckets(frequency)?.any(|bucket| {
                    bucket.start_ns == bar.interval_start_ns && bucket.end_ns == bar.interval_end_ns
                }) {
                    return Err(invalid("Bar频率边界无效或跨越午休"));
                }
                (&bar.session, bar.interval_end_ns)
            }
            MarketEvent::TradeTick(tick) => (&tick.session, tick.time_ns),
            MarketEvent::QuoteTick(tick) => (&tick.session, tick.time_ns),
        };
        if session != &self.session.key || !self.market_windows.iter().any(|w| w.contains(time)) {
            return Err(invalid("事件会话或交易时段不符，不能把午休当交易"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BarBucket {
    pub start_ns: Nanoseconds,
    pub end_ns: Nanoseconds,
    pub full_interval: bool,
}
pub struct BucketIter<'a> {
    windows: &'a [TimeWindow],
    window: usize,
    start: Option<Nanoseconds>,
    duration: i64,
}
impl Iterator for BucketIter<'_> {
    type Item = BarBucket;
    fn next(&mut self) -> Option<Self::Item> {
        let window = self.windows.get(self.window)?;
        let start = self.start.unwrap_or(window.start_ns);
        let proposed = start.get().checked_add(self.duration).unwrap_or(i64::MAX);
        let end = Nanoseconds::new(proposed.min(window.end_ns.get()));
        if end == window.end_ns {
            self.window += 1;
            self.start = None;
        } else {
            self.start = Some(end);
        }
        Some(BarBucket {
            start_ns: start,
            end_ns: end,
            full_interval: end.get().checked_sub(start.get()) == Some(self.duration),
        })
    }
}

#[derive(Debug, Clone)]
pub struct SessionCalendar {
    reference: String,
    sessions: Vec<CalendarSession>,
    next_after_run: Option<SessionKey>,
}
impl SessionCalendar {
    pub fn new(
        reference: String,
        sessions: Vec<CalendarSession>,
        next_after_run: Option<SessionKey>,
    ) -> QfResult<Self> {
        crate::types::keys::label(&reference, 128)?;
        if sessions.is_empty() || sessions.len() > MAX_CALENDAR_SESSIONS {
            return Err(invalid("参考日历为空或超过预算"));
        }
        let mut keys = BTreeSet::new();
        for (i, row) in sessions.iter().enumerate() {
            row.validate()?;
            if !keys.insert(row.session.key.clone()) {
                return Err(invalid("日历会话键重复"));
            }
            if let Some(previous) = i.checked_sub(1).map(|i| &sessions[i]) {
                if previous.date >= row.date
                    || previous.after_close_ns >= row.before_open_ns
                    || previous.session.exchange_timezone != row.session.exchange_timezone
                {
                    return Err(invalid("日历日期、时点或参考时区不一致"));
                }
                for (before, after) in [(&previous.week, &row.week), (&previous.month, &row.month)]
                {
                    if before.id == after.id {
                        if before.total_trading_days != after.total_trading_days
                            || before.trading_day + 1 != after.trading_day
                        {
                            return Err(invalid("同周月交易日序号不连续"));
                        }
                    } else if before.trading_day != before.total_trading_days
                        || after.trading_day != 1
                    {
                        return Err(invalid("跨周月缺少完整交易日边界"));
                    }
                }
            }
        }
        if next_after_run
            .as_ref()
            .is_some_and(|next| keys.contains(next))
        {
            return Err(invalid("下一会话不能指回本run已有会话"));
        }
        Ok(Self {
            reference,
            sessions,
            next_after_run,
        })
    }
    pub fn reference(&self) -> &str {
        &self.reference
    }
    pub fn sessions(&self) -> &[CalendarSession] {
        &self.sessions
    }
    /// After the last market event at close, orders belong to the authoritative
    /// next session. A remaining same-ns Tick still permits the current session.
    pub fn effective_session(
        &self,
        index: usize,
        now: Nanoseconds,
        closing: bool,
    ) -> QfResult<SessionKey> {
        let row = self
            .sessions
            .get(index)
            .ok_or_else(|| invalid("当前会话越界"))?;
        if !closing && now <= row.session.close_ns {
            return Ok(row.session.key.clone());
        }
        self.sessions
            .get(index + 1)
            .map(|r| r.session.key.clone())
            .or_else(|| self.next_after_run.clone())
            .ok_or_else(|| invalid("下一可生效交易会话未知"))
    }
}
