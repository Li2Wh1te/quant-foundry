//! bar_next_interval_v1. D03 supplies completed, ordered Bar boundaries; D06
//! supplies stable order priority and D09's cumulative candidate budget. This
//! module has no account, order store, fee accumulator or market-data reader.
use super::{ExecutionModelDescription, Fill, FillBudget, MatchOutcome, Matcher};
use crate::orders::{Order, OrderStatus, Side, TimeInForce};
use crate::rules::market::{
    DailyPriceLimit, Instrument, RuleBook, RuleUse, TradeDayFacts, TradingStatus,
};
use crate::run::{ExecutionModel, Frequency, RunConfig};
use crate::types::{
    Bar, ExactDecimal, MarketEvent, Nanoseconds, Price, Quantity, QuantityStep, SecurityKey,
};
use crate::{ErrorCode, QfError, QfResult};
use std::collections::{BTreeMap, BTreeSet};

/// One D02-selected dated rule and explicit D04 daily facts. Missing/unknown
/// facts retain D02's refusal; callers cannot construct an unchecked bundle.
#[derive(Debug, Clone)]
pub struct BarRules {
    instrument: Instrument,
    book: RuleBook,
    facts: TradeDayFacts,
    usage: RuleUse,
}
impl BarRules {
    pub fn new(
        book: &RuleBook,
        instrument: Instrument,
        facts: TradeDayFacts,
        usage: RuleUse,
    ) -> QfResult<Self> {
        let selected = book.resolve(&instrument, &facts, &usage)?.rule().clone();
        Ok(Self {
            instrument,
            book: RuleBook::new(vec![selected])?,
            facts,
            usage,
        })
    }
}

pub struct BarMatcher {
    participation: ExactDecimal,
    slippage_bps: ExactDecimal,
    frequency: Frequency,
    universe: BTreeSet<SecurityKey>,
    // Only the last interval end for each authorized security. No Bar history
    // or successful-event ledger; duplicate/overlapping input cannot reset volume.
    last_end: BTreeMap<SecurityKey, Nanoseconds>,
}
impl BarMatcher {
    /// Only accepts the shared, validated immutable RunConfig. Defaults are
    /// normalized there, never in a matcher-specific constructor.
    pub fn from_run(config: &RunConfig) -> QfResult<Self> {
        if config.execution_model != ExecutionModel::BarNextIntervalV1 {
            return Err(failure(
                ErrorCode::InvalidRunConfig,
                "Bar撮合需要bar_next_interval_v1",
            ));
        }
        Ok(Self {
            participation: config.participation_rate,
            slippage_bps: config.slippage_bps,
            frequency: config.frequency,
            universe: config.universe.iter().cloned().collect(),
            last_end: BTreeMap::new(),
        })
    }

    fn candidate_price(
        &self,
        bar: &Bar,
        order: &Order,
        tick: Price,
    ) -> QfResult<Option<(Price, &'static str)>> {
        let (reference, source) = match (order.side, order.limit_price) {
            (_, None) => (bar.open, "开盘代理"),
            (Side::Buy, Some(limit)) if bar.open <= limit => (bar.open, "开盘优价代理"),
            (Side::Sell, Some(limit)) if bar.open >= limit => (bar.open, "开盘优价代理"),
            (Side::Buy, Some(limit)) if bar.low <= limit => (limit, "限价触及代理"),
            (Side::Sell, Some(limit)) if bar.high >= limit => (limit, "限价触及代理"),
            _ => return Ok(None),
        };
        // One original-rational rounding in tick units, even for 28-digit bps.
        let slipped = reference.get().slipped_price(
            self.slippage_bps,
            tick.get(),
            order.side == Side::Buy,
        )?;
        // Adverse slippage is capped at the observable/limit boundary. This is
        // a declared proxy assumption, not a reconstruction of intrabar trades.
        let bounded = match order.side {
            Side::Buy => slipped.min(order.limit_price.unwrap_or(bar.high).min(bar.high).get()),
            Side::Sell => slipped.max(order.limit_price.unwrap_or(bar.low).max(bar.low).get()),
        };
        Ok(Some((Price::new(bounded)?, source)))
    }

    fn match_bar(
        &self,
        bar: &Bar,
        orders: &[Order],
        rules: &BarRules,
        budget: &dyn FillBudget,
    ) -> QfResult<MatchOutcome> {
        let mut outcome = MatchOutcome {
            fills: vec![],
            orders: vec![],
        };
        if rules.instrument.security != bar.security
            || rules.facts.session != bar.session
            || bar.identity.source_session != bar.session
        {
            return Err(failure(
                ErrorCode::InvalidContract,
                "Bar身份/会话与适用规则不一致",
            ));
        }
        let resolved = rules
            .book
            .resolve(&rules.instrument, &rules.facts, &rules.usage)?;
        let volume = bar.quantity.ok_or_else(|| {
            scoped(
                bar,
                ErrorCode::RuleUnavailable,
                "缺少可验证Bar成交量；不能分配流动性",
            )
        })?;
        let step = QuantityStep::new(1)?; // Integer shares, not submission lots.
        let mut remaining =
            ExactDecimal::legal_quantity(volume, self.participation, ExactDecimal::ONE, step)?;
        let mut ids = BTreeSet::new();
        for order in orders {
            if !ids.insert(&order.order_id)
                || order.security != bar.security
                || !order.status.is_active()
                || order.remaining()? == Quantity::ZERO
            {
                return Err(scoped(
                    bar,
                    ErrorCode::InvalidContract,
                    "活动订单重复、标的或数量状态无效",
                ));
            }
        }
        if rules.facts.status != TradingStatus::Halted {
            for price in [bar.open, bar.high, bar.low, bar.close] {
                resolved.validate_execution(price)?;
            }
        }
        for order in orders {
            // Recheck the actual interval even when called outside D03. Never
            // use iteration position, close time, or eligible_after_event alone.
            if order.submitted_ns > bar.interval_start_ns
                || order
                    .eligible_interval_start
                    .is_none_or(|start| start > bar.interval_start_ns)
                || (order.tif == TimeInForce::Day && order.effective_session != bar.session)
            {
                continue;
            }
            let blocked = if rules.facts.status == TradingStatus::Halted {
                Some("已知停牌，本Bar不能成交")
            } else if remaining == Quantity::ZERO {
                Some("本Bar共享参与量为零或已用完，余单保持活动")
            } else {
                None
            };
            if let Some(message) = blocked {
                unfilled(&mut outcome, order, None, message);
                continue;
            }
            let Some((price, reference)) =
                self.candidate_price(bar, order, resolved.rule().price_tick)?
            else {
                unfilled(&mut outcome, order, None, "本Bar未触及限价，余单保持活动");
                continue;
            };
            resolved.validate_execution(price)?;
            if let DailyPriceLimit::Limited { lower, upper, .. } = rules.facts.price_limit {
                let message = match order.side {
                    Side::Buy if price == upper => Some("成交代理处于涨停，无法证明买入队列可成交"),
                    Side::Sell if price == lower => {
                        Some("成交代理处于跌停，无法证明卖出队列可成交")
                    }
                    _ => None,
                };
                if let Some(message) = message {
                    unfilled(&mut outcome, order, None, message);
                    continue;
                }
            }
            if price < bar.low
                || price > bar.high
                || order.limit_price.is_some_and(|limit| match order.side {
                    Side::Buy => price > limit,
                    Side::Sell => price < limit,
                })
            {
                return Err(scoped(
                    bar,
                    ErrorCode::InvalidContract,
                    "候选成交违反限价或Bar区间",
                ));
            }
            let requested = order.remaining()?.min(remaining);
            let allowance =
                budget.allowance_after(&outcome.fills, &order.order_id, requested, price, step)?;
            if allowance.requested_quantity != requested || allowance.effective_quantity > requested
            {
                return Err(scoped(
                    bar,
                    ErrorCode::InvalidContract,
                    "账户预算改变请求或放大候选成交量",
                ));
            }
            let quantity = allowance.effective_quantity;
            if quantity == Quantity::ZERO {
                unfilled(
                    &mut outcome,
                    order,
                    allowance.reason_code,
                    &allowance.message,
                );
                continue;
            }
            remaining = remaining.checked_sub(quantity)?;
            outcome.fills.push(Fill {
                trade_id: budget.trade_id(outcome.fills.len())?,
                order_id: order.order_id.clone(),
                security: order.security.clone(),
                side: order.side,
                quantity,
                price,
                fee: ExactDecimal::ZERO,
                execution_time_ns: bar.interval_end_ns,
            });
            let mut changed = order.clone();
            changed.filled_quantity = changed.filled_quantity.checked_add(quantity)?;
            changed.status = if changed.filled_quantity == changed.quantity {
                OrderStatus::Filled
            } else {
                OrderStatus::PartiallyFilled
            };
            changed.reason_code = allowance.reason_code;
            changed.message = format!("Bar结束确认；{reference}；{}", allowance.message);
            outcome.orders.push(changed);
        }
        Ok(outcome)
    }
}

fn failure(code: ErrorCode, message: &str) -> QfError {
    QfError::new(code, "bar_matching", message)
}
fn scoped(bar: &Bar, code: ErrorCode, message: &str) -> QfError {
    let mut error = failure(code, message);
    error
        .scope
        .insert("security".into(), bar.security.as_str().into());
    error
        .scope
        .insert("session".into(), bar.session.as_str().into());
    error
}
fn unfilled(outcome: &mut MatchOutcome, order: &Order, reason: Option<ErrorCode>, message: &str) {
    // Reasons are user-visible order changes, not one record per unfilled Bar.
    if order.status == OrderStatus::Accepted
        || order.reason_code != reason
        || order.message != message
    {
        let mut changed = order.clone();
        if changed.status == OrderStatus::Accepted {
            changed.status = OrderStatus::Open;
        }
        changed.reason_code = reason;
        changed.message = message.into();
        outcome.orders.push(changed);
    }
}

impl Matcher for BarMatcher {
    type Rules = BarRules;
    fn model_description(&self) -> Option<ExecutionModelDescription> {
        Some(ExecutionModelDescription {
            model: ExecutionModel::BarNextIntervalV1, frequency: self.frequency,
            participation_rate: self.participation, slippage_bps: self.slippage_bps,
            fill_unit: QuantityStep::new(1).expect("integer share unit"),
            price_reference: "市价用下一合格Bar的open；限价用open优价或low/high触及代理".into(),
            confirmation: "成交仅在Bar结束确认，execution_time_ns=interval_end_ns".into(),
            price_rounding: "不利滑点原始比例一次取tick：买向上、卖向下；截至限价/OHLC边界".into(),
            liquidity: "同标的同Bar所有自有买卖订单共享floor(volume*participation_rate)，按D06稳定委托序分配".into(),
            assumptions: vec![
                "OHLC不能重建盘中路径、证明开盘可得量或真实触价顺序".into(),
                "成交代理在涨停价不撮合买入，在跌停价不撮合卖出；一字板不保证成交，反方向仍需量和账户预算".into(),
                "无真实队列或成交保证；零滑点是明示理想化代理，费用仍由唯一账户累计核定".into(),
                "资格按eligible_interval_start和submitted_ns锁定；DAY会话/到期由D03/D06管理".into(),
            ],
        })
    }
    fn consume(
        &mut self,
        _event: &MarketEvent,
        _orders: &[Order],
        _rules: &BarRules,
    ) -> QfResult<MatchOutcome> {
        Err(failure(
            ErrorCode::CapabilityUnavailable,
            "Bar撮合必须接入D06/D09真实候选预算",
        ))
    }
    fn consume_with_budget(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &BarRules,
        budget: &dyn FillBudget,
    ) -> QfResult<MatchOutcome> {
        event.validate()?;
        if orders.len() > crate::results::MAX_PAGE_ITEMS {
            return Err(failure(
                ErrorCode::ResourceLimit,
                "Bar候选活动订单超过共享数量预算",
            ));
        }
        let MarketEvent::Bar(bar) = event else {
            return Err(failure(
                ErrorCode::InvalidContract,
                "Bar模型不能消费Tick撮合源",
            ));
        };
        if !self.universe.contains(&bar.security)
            || self
                .last_end
                .get(&bar.security)
                .is_some_and(|end| bar.interval_start_ns < *end)
        {
            return Err(scoped(
                bar,
                ErrorCode::InvalidContract,
                "Bar未授权、重复或与已处理区间重叠",
            ));
        }
        let outcome = self.match_bar(bar, orders, rules, budget)?;
        // Candidate failures leave both the matcher cursor and live account
        // untouched. D03/D06 then perform their one atomic account/order commit.
        self.last_end
            .insert(bar.security.clone(), bar.interval_end_ns);
        Ok(outcome)
    }
}
