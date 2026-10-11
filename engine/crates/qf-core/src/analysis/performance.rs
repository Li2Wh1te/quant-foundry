//! Constant-space session statistics. Account money is never recomputed here.
use super::{EquityPoint, Metric};
use crate::accounting::AccountTotals;
use crate::matching::Fill;
use crate::orders::{Order, OrderStatus, Side};
use crate::results::FiniteStatistic;
use crate::run::{Frequency, ResultSampling, RunConfig};
use crate::types::{ExactDecimal, Money, Nanoseconds, SessionKey};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

fn invalid() -> QfError {
    QfError::new(
        ErrorCode::InvalidContract,
        "analysis",
        "分析采样边界、权益或基准无效",
    )
}
pub fn na(reason: &str) -> Metric {
    Metric::Unavailable {
        reason: reason.into(),
    }
}
pub fn metric(value: f64) -> Metric {
    FiniteStatistic::new(value)
        .map(|value| Metric::Available { value })
        .unwrap_or_else(|_| na("NUMERIC_RANGE_UNSUPPORTED"))
}
fn float(value: ExactDecimal) -> f64 {
    // Decimal -> float is restricted to explicitly labelled statistical values.
    value
        .canonical()
        .parse()
        .expect("finite Decimal representation")
}
fn return_rate(value: Money, base: Money) -> Metric {
    if base <= Money::ZERO {
        return na("NONPOSITIVE_DENOMINATOR");
    }
    match value.checked_sub(base) {
        Ok(delta) => metric(float(delta) / float(base)),
        // Research ratios are float64, not a cash settlement. Opposite-scale
        // valid money (e.g. 100 and 1e-28) can have an unrepresentable Decimal
        // difference although the wealth ratio is perfectly valid.
        Err(_) => metric(float(value) / float(base) - 1.0),
    }
}
fn available(m: &Metric) -> Option<f64> {
    match m {
        Metric::Available { value } => Some(value.get()),
        _ => None,
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnalysisPolicy {
    pub formula_id: String,
    pub statistic_precision: String,
    pub annualization_sessions: u16,
    pub risk_free_rate: ExactDecimal,
    pub reference_calendar: String,
    pub frequency: Frequency,
    pub curve_sampling: ResultSampling,
    pub statistics_sampling: String,
    pub benchmark: Option<String>,
    pub turnover_definition: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BenchmarkPoint {
    pub time_ns: Nanoseconds,
    pub value: Option<ExactDecimal>,
    pub unavailable_reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradeStatistics {
    pub fills: u64,
    pub buy_fills: u64,
    pub sell_fills: u64,
    pub buy_quantity: crate::types::Sequence,
    pub sell_quantity: crate::types::Sequence,
    pub buy_notional: Money,
    pub sell_notional: Money,
    pub total_notional: Money,
    pub fill_fees: Money,
    pub order_state_events: BTreeMap<String, u64>,
    pub mean_fill_notional: Metric,
    pub account_totals: Option<AccountTotals>,
    pub account_totals_unavailable_reason: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PerformanceSummary {
    pub policy: AnalysisPolicy,
    pub expected_sessions: u64,
    pub sampled_sessions: u64,
    pub valid_return_samples: u64,
    pub missing_equity_sessions: u64,
    pub initial_equity: Money,
    pub terminal_equity: Option<Money>,
    pub cumulative_return: Metric,
    pub annualized_return: Metric,
    pub maximum_drawdown: Metric,
    pub volatility: Metric,
    pub sharpe: Metric,
    pub benchmark_return: Metric,
    pub cumulative_excess_return: Metric,
    pub turnover: Metric,
    pub mean_session_equity: Metric,
    pub fees_to_initial_equity: Metric,
    pub trades: TradeStatistics,
}
#[derive(Debug, Clone, Default)]
struct Moments {
    n: u64,
    mean: f64,
    m2: f64,
}
impl Moments {
    fn push(&mut self, value: f64) {
        self.n += 1;
        let delta = value - self.mean;
        self.mean += delta / self.n as f64;
        self.m2 += delta * (value - self.mean);
    }
    fn std(&self) -> Option<f64> {
        (self.n >= 2).then(|| (self.m2.max(0.0) / (self.n - 1) as f64).sqrt())
    }
}
#[derive(Debug, Clone)]
pub struct Performance {
    policy: AnalysisPolicy,
    expected: u64,
    initial: Money,
    previous: Option<Money>,
    terminal: Option<Money>,
    last_time: Option<Nanoseconds>,
    sessions: u64,
    missing: u64,
    invalid_denominator: bool,
    numeric_failure: bool,
    returns: Moments,
    equities: Moments,
    peak: Money,
    curve_peak: Money,
    curve_missing: bool,
    max_drawdown: f64,
    benchmark_initial: Option<ExactDecimal>,
    benchmark_terminal: Option<ExactDecimal>,
    benchmark_missing: bool,
    benchmark_reason: Option<String>,
    trades: TradeStatistics,
}
impl Performance {
    pub fn from_run(config: &RunConfig, calendar: &str, expected: u64) -> QfResult<Self> {
        crate::types::keys::label(calendar, 128)?;
        Ok(Self {
            policy: AnalysisPolicy {
                formula_id: "qf.performance.v1".into(), statistic_precision: "float64; exact money strings".into(),
                annualization_sessions: config.annualization_sessions, risk_free_rate: config.risk_free_rate,
                reference_calendar: calendar.into(), frequency: config.frequency, curve_sampling: config.result_sampling,
                statistics_sampling: "session_close; initial capital is the interval origin; ddof=1".into(),
                benchmark: config.benchmark.clone(),
                turnover_definition: "(buy_notional+sell_notional)/(2*mean(all valid session-close equities)); NA on gaps".into(),
            },
            expected, initial: config.initial_cash, previous: Some(config.initial_cash), terminal: Some(config.initial_cash),
            last_time: None, sessions: 0, missing: 0, invalid_denominator: false, numeric_failure: false,
            returns: Moments::default(), equities: Moments::default(), peak: config.initial_cash, curve_peak: config.initial_cash, curve_missing:false, max_drawdown: 0.0,
            benchmark_initial: None, benchmark_terminal: None, benchmark_missing: false, benchmark_reason: None,
            trades: TradeStatistics {
                fills: 0, buy_fills: 0, sell_fills: 0, buy_quantity: crate::types::Sequence::new(0),
                sell_quantity: crate::types::Sequence::new(0), buy_notional: Money::ZERO, sell_notional: Money::ZERO,
                total_notional: Money::ZERO, fill_fees: Money::ZERO, order_state_events: BTreeMap::new(),
                mean_fill_notional: na("NO_TRADES"), account_totals: None,
                account_totals_unavailable_reason: Some("ACCOUNT_TOTALS_UNAVAILABLE".into()),
            },
        })
    }
    pub fn policy(&self) -> &AnalysisPolicy {
        &self.policy
    }
    /// The benchmark origin must have the exact same boundary as initial capital.
    pub fn begin(
        &mut self,
        time: Nanoseconds,
        point: Option<BenchmarkPoint>,
    ) -> QfResult<EquityPoint> {
        if self.last_time.is_some() {
            return Err(invalid());
        }
        self.last_time = Some(time);
        self.benchmark_initial = self.benchmark(time, point)?;
        Ok(self.curve(
            time,
            None,
            "initial",
            (Some(self.initial), None),
            na("INITIAL_POINT"),
            self.benchmark_initial,
        ))
    }
    fn benchmark(
        &mut self,
        time: Nanoseconds,
        point: Option<BenchmarkPoint>,
    ) -> QfResult<Option<ExactDecimal>> {
        if self.policy.benchmark.is_none() {
            return Ok(None);
        }
        let value = match point {
            Some(p)
                if p.time_ns == time
                    && p.value.is_some_and(|v| v > ExactDecimal::ZERO)
                    && p.unavailable_reason.is_none() =>
            {
                p.value
            }
            Some(p) if p.time_ns == time && p.value.is_none() => {
                self.benchmark_reason.get_or_insert(
                    p.unavailable_reason
                        .unwrap_or_else(|| "MISSING_BENCHMARK".into()),
                );
                None
            }
            Some(_) => {
                self.benchmark_reason
                    .get_or_insert("BENCHMARK_INTERVAL_MISMATCH_OR_INVALID_VALUE".into());
                None
            }
            None => {
                self.benchmark_reason
                    .get_or_insert("MISSING_BENCHMARK".into());
                None
            }
        };
        self.benchmark_missing |= value.is_none();
        Ok(value)
    }
    /// Every calendar session must be passed, including an unvalued close.
    pub fn session(
        &mut self,
        index: u64,
        session: &SessionKey,
        time: Nanoseconds,
        equity: Option<Money>,
        reason: Option<String>,
        benchmark: Option<BenchmarkPoint>,
    ) -> QfResult<EquityPoint> {
        if index != self.sessions
            || index >= self.expected
            || self.last_time.is_none_or(|last| time <= last)
            || equity.is_some_and(|v| v.is_negative())
            || equity.is_some() && reason.is_some()
        {
            return Err(invalid());
        }
        self.benchmark_terminal = self.benchmark(time, benchmark)?;
        let ret = match (equity, self.previous) {
            (Some(e), Some(p)) => return_rate(e, p),
            _ => na("MISSING_EQUITY_SEGMENT"),
        };
        if let Some(r) = available(&ret) {
            self.returns.push(r);
        } else if equity.is_some() && self.previous.is_some() {
            self.invalid_denominator |= self.previous.is_some_and(|p| p <= Money::ZERO);
            self.numeric_failure |= !self.invalid_denominator;
        }
        if let Some(e) = equity {
            self.equities.push(float(e));
            if e > self.curve_peak {
                self.curve_peak = e;
            }
            if e > self.peak {
                self.peak = e;
            }
            if let Some(d) = available(&return_rate(e, self.peak)) {
                self.max_drawdown = self.max_drawdown.max(-d);
            } else {
                self.numeric_failure = true;
            }
        } else {
            self.curve_missing = true;
            self.missing += 1;
        }
        self.sessions += 1;
        self.last_time = Some(time);
        self.previous = equity; // A missing close breaks BOTH adjacent returns.
        self.terminal = equity;
        Ok(self.curve(
            time,
            Some(session.clone()),
            "session_close",
            (
                equity,
                equity
                    .is_none()
                    .then(|| reason.unwrap_or_else(|| "MISSING_VALUATION".into())),
            ),
            ret,
            self.benchmark_terminal,
        ))
    }
    /// Optional fine curves never feed session moments or annualization.
    pub fn bar_curve(
        &mut self,
        session: &SessionKey,
        time: Nanoseconds,
        equity: Option<Money>,
    ) -> EquityPoint {
        if let Some(e) = equity {
            self.curve_peak = self.curve_peak.max(e);
        } else {
            self.curve_missing = true;
        }
        self.curve(
            time,
            Some(session.clone()),
            "bar_close",
            (equity, equity.is_none().then(|| "MISSING_VALUATION".into())),
            na("SESSION_STATISTICS_ONLY"),
            None,
        )
    }
    fn curve(
        &self,
        time_ns: Nanoseconds,
        session: Option<SessionKey>,
        sampling: &str,
        valuation: (Option<Money>, Option<String>),
        session_return: Metric,
        benchmark_value: Option<ExactDecimal>,
    ) -> EquityPoint {
        let (equity, unavailable_reason) = valuation;
        let cumulative = if self.missing > 0 {
            na("MISSING_EQUITY")
        } else {
            equity.map_or_else(|| na("MISSING_EQUITY"), |e| return_rate(e, self.initial))
        };
        let benchmark_return = if self.policy.benchmark.is_none() {
            na("NO_BENCHMARK")
        } else if sampling == "bar_close" {
            na("SESSION_STATISTICS_ONLY")
        } else if self.benchmark_missing {
            na(self
                .benchmark_reason
                .as_deref()
                .unwrap_or("MISSING_BENCHMARK"))
        } else {
            match (benchmark_value, self.benchmark_initial) {
                (Some(value), Some(origin)) => return_rate(value, origin),
                _ => na("MISSING_BENCHMARK"),
            }
        };
        let excess = match (&cumulative, &benchmark_return) {
            (Metric::Available { value: a }, Metric::Available { value: b }) => {
                metric(a.get() - b.get())
            }
            (Metric::Unavailable { reason }, _) | (_, Metric::Unavailable { reason }) => na(reason),
        };
        EquityPoint {
            time_ns,
            session,
            sampling: sampling.into(),
            equity,
            unavailable_reason,
            net_value: equity.map_or_else(
                || na("MISSING_EQUITY"),
                |e| metric(float(e) / float(self.initial)),
            ),
            session_return,
            cumulative_return: cumulative,
            benchmark_value,
            benchmark_cumulative_return: benchmark_return,
            cumulative_excess_return: excess,
            drawdown: if self.curve_missing {
                na("MISSING_EQUITY")
            } else {
                equity.map_or_else(|| na("MISSING_EQUITY"), |e| return_rate(e, self.curve_peak))
            },
        }
    }
    pub fn fill(&mut self, fill: &Fill) -> QfResult<()> {
        if fill.quantity.get() == 0 || fill.fee.is_negative() {
            return Err(invalid());
        }
        let amount = fill
            .price
            .get()
            .checked_mul(Money::from_integer(fill.quantity.get()))?;
        let mut next = self.trades.clone();
        next.fills = next.fills.checked_add(1).ok_or_else(invalid)?;
        match fill.side {
            Side::Buy => {
                next.buy_fills += 1;
                next.buy_quantity = crate::types::Sequence::new(
                    next.buy_quantity
                        .get()
                        .checked_add(fill.quantity.get() as u64)
                        .ok_or_else(invalid)?,
                );
                next.buy_notional = next.buy_notional.checked_add(amount)?;
            }
            Side::Sell => {
                next.sell_fills += 1;
                next.sell_quantity = crate::types::Sequence::new(
                    next.sell_quantity
                        .get()
                        .checked_add(fill.quantity.get() as u64)
                        .ok_or_else(invalid)?,
                );
                next.sell_notional = next.sell_notional.checked_add(amount)?;
            }
        }
        next.total_notional = next.total_notional.checked_add(amount)?;
        next.fill_fees = next.fill_fees.checked_add(fill.fee)?;
        next.mean_fill_notional = metric(float(next.total_notional) / next.fills as f64);
        self.trades = next;
        Ok(())
    }
    pub fn order(&mut self, order: &Order) -> QfResult<()> {
        let key = match order.status {
            OrderStatus::Accepted => "accepted",
            OrderStatus::Open => "open",
            OrderStatus::PartiallyFilled => "partially_filled",
            OrderStatus::Filled => "filled",
            OrderStatus::Cancelled => "cancelled",
            OrderStatus::Expired => "expired",
            OrderStatus::Rejected => "rejected",
        };
        let count = self
            .trades
            .order_state_events
            .entry(key.into())
            .or_default();
        *count = count.checked_add(1).ok_or_else(invalid)?;
        Ok(())
    }
    pub fn summary(&self, totals: Option<AccountTotals>) -> PerformanceSummary {
        let reason = if self.sessions == 0 {
            Some("NO_SESSION_SAMPLES")
        } else if self.sessions != self.expected {
            Some("INCOMPLETE_SESSIONS")
        } else if self.missing > 0 {
            Some("MISSING_EQUITY")
        } else if self.numeric_failure {
            Some("NUMERIC_RANGE_UNSUPPORTED")
        } else {
            None
        };
        let cumulative = reason.map_or_else(
            || {
                self.terminal
                    .map_or_else(|| na("MISSING_EQUITY"), |e| return_rate(e, self.initial))
            },
            na,
        );
        let annual = reason.map_or_else(
            || {
                available(&cumulative).map_or_else(
                    || na("NUMERIC_RANGE_UNSUPPORTED"),
                    |r| {
                        // Preserve near-total losses: float (1+r) can become 0
                        // while the exact terminal/initial wealth is positive.
                        // log1p keeps tiny ordinary returns precise instead.
                        let log_wealth = if r > -0.5 {
                            r.ln_1p()
                        } else {
                            (float(self.terminal.expect("valid terminal")) / float(self.initial))
                                .ln()
                        };
                        metric(
                            (log_wealth * self.policy.annualization_sessions as f64
                                / self.sessions as f64)
                                .exp_m1(),
                        )
                    },
                )
            },
            na,
        );
        let std_reason = reason.or(if self.invalid_denominator {
            Some("NONPOSITIVE_DENOMINATOR")
        } else if self.returns.n < 2 {
            Some("INSUFFICIENT_RETURN_SAMPLES")
        } else {
            None
        });
        let std = self.returns.std();
        let volatility = std_reason.map_or_else(
            || metric(std.unwrap() * (self.policy.annualization_sessions as f64).sqrt()),
            na,
        );
        let sharpe = std_reason
            .or(if std == Some(0.0) {
                Some("ZERO_VOLATILITY")
            } else {
                None
            })
            .map_or_else(
                || {
                    let rf = float(self.policy.risk_free_rate);
                    let rf_log = if rf > -0.5 {
                        rf.ln_1p()
                    } else {
                        // A valid Decimal rf>-1 must not round to float -1
                        // before its annual-to-session growth conversion.
                        float(
                            self.policy
                                .risk_free_rate
                                .checked_add(Money::ONE)
                                .expect("negative rf>-1 has representable positive growth"),
                        )
                        .ln()
                    };
                    let rf_daily = (rf_log / self.policy.annualization_sessions as f64).exp_m1();
                    metric(
                        (self.returns.mean - rf_daily) / std.unwrap()
                            * (self.policy.annualization_sessions as f64).sqrt(),
                    )
                },
                na,
            );
        let benchmark = if self.policy.benchmark.is_none() {
            na("NO_BENCHMARK")
        } else if self.sessions != self.expected || self.sessions == 0 {
            na("INCOMPLETE_BENCHMARK_INTERVAL")
        } else if self.benchmark_missing {
            na(self
                .benchmark_reason
                .as_deref()
                .unwrap_or("MISSING_BENCHMARK"))
        } else {
            match (self.benchmark_initial, self.benchmark_terminal) {
                (Some(b), Some(e)) => return_rate(e, b),
                _ => na("MISSING_BENCHMARK"),
            }
        };
        let excess = match (available(&cumulative), available(&benchmark)) {
            (Some(a), Some(b)) => metric(a - b),
            _ if reason.is_some() => na(reason.unwrap()),
            _ => match &benchmark {
                Metric::Unavailable { reason } => na(reason),
                _ => na("NUMERIC_RANGE_UNSUPPORTED"),
            },
        };
        let turnover = reason.map_or_else(
            || {
                if self.equities.mean <= 0.0 {
                    na("NONPOSITIVE_DENOMINATOR")
                } else {
                    metric(float(self.trades.total_notional) / (2.0 * self.equities.mean))
                }
            },
            na,
        );
        let mut trades = self.trades.clone();
        trades.account_totals = totals;
        trades.account_totals_unavailable_reason = trades
            .account_totals
            .is_none()
            .then(|| "ACCOUNT_TOTALS_UNAVAILABLE".into());
        let fees = trades
            .account_totals
            .as_ref()
            .map_or(trades.fill_fees, |t| t.trading_fees);
        PerformanceSummary {
            policy: self.policy.clone(),
            expected_sessions: self.expected,
            sampled_sessions: self.sessions,
            valid_return_samples: self.returns.n,
            missing_equity_sessions: self.missing,
            initial_equity: self.initial,
            terminal_equity: self.terminal,
            cumulative_return: cumulative,
            annualized_return: annual,
            maximum_drawdown: reason.map_or_else(|| metric(self.max_drawdown), na),
            volatility,
            sharpe,
            benchmark_return: benchmark,
            cumulative_excess_return: excess,
            turnover,
            mean_session_equity: reason.map_or_else(|| metric(self.equities.mean), na),
            fees_to_initial_equity: metric(float(fees) / float(self.initial)),
            trades,
        }
    }
}

impl PerformanceSummary {
    pub fn invalidate(&mut self, reason: &str) {
        self.cumulative_return = na(reason);
        self.annualized_return = na(reason);
        self.maximum_drawdown = na(reason);
        self.volatility = na(reason);
        self.sharpe = na(reason);
        self.benchmark_return = na(reason);
        self.cumulative_excess_return = na(reason);
        self.turnover = na(reason);
        self.mean_session_equity = na(reason);
    }
}
