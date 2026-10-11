//! Basic prints or top-of-book quotes, selected once per run. No order book,
//! exchange queue, account, fee accumulator, data reader or per-tick ledger.
use super::{ExecutionModelDescription, Fill, FillBudget, MatchOutcome, Matcher};
use crate::orders::{Order, OrderStatus, Side, TimeInForce};
use crate::rules::market::{DailyPriceLimit, TradingStatus};
use crate::run::{ExecutionModel, Frequency, RunConfig};
use crate::types::{
    EventKey, ExactDecimal, MarketEvent, Price, Quantity, QuantityStep, SecurityKey, SessionKey,
};
use crate::{ErrorCode, QfError, QfResult};
use std::collections::{BTreeMap, BTreeSet};

/// Reuse D07's checked D02 dated instrument/facts bundle for both Tick models.
pub use super::bar::BarRules as TickRules;

#[derive(Clone)]
struct SideBalance {
    price: Option<Price>,
    displayed: Option<Quantity>,
    remaining: Quantity,
}
impl Default for SideBalance {
    fn default() -> Self {
        Self {
            price: None,
            displayed: None,
            remaining: Quantity::ZERO,
        }
    }
}
impl SideBalance {
    fn observe(
        &mut self,
        price: Option<Price>,
        quantity: Option<Quantity>,
        participation: ExactDecimal,
    ) -> QfResult<()> {
        // Missing is unknown, not a zero-size observation or a refill. Retain
        // the last known depletion across gaps, but never match using it.
        let Some(price) = price else { return Ok(()) };
        if self.price != Some(price) {
            self.price = Some(price);
            self.displayed = None;
            self.remaining = Quantity::ZERO;
        }
        let Some(quantity) = quantity else {
            return Ok(());
        };
        let ceiling = participation_quantity(quantity, participation)?;
        self.remaining = match self.displayed {
            None => ceiling,
            Some(old) if quantity > old => self.remaining.checked_add(participation_quantity(
                quantity.checked_sub(old)?,
                participation,
            )?)?,
            Some(_) => self.remaining.min(ceiling),
        };
        self.displayed = Some(quantity);
        Ok(())
    }
}
#[derive(Clone)]
struct QuoteBalance {
    session: SessionKey,
    bid: SideBalance,
    ask: SideBalance,
}
impl QuoteBalance {
    fn new(session: SessionKey) -> Self {
        Self {
            session,
            bid: SideBalance::default(),
            ask: SideBalance::default(),
        }
    }
}

pub struct TickMatcher {
    model: ExecutionModel,
    participation: ExactDecimal,
    slippage_bps: ExactDecimal,
    universe: BTreeSet<SecurityKey>,
    // One cursor plus two side balances per authorized security. Input length
    // cannot grow this state; no successful-event history is retained.
    last_key: Option<EventKey>,
    quotes: BTreeMap<SecurityKey, QuoteBalance>,
}
impl TickMatcher {
    pub fn from_run(config: &RunConfig) -> QfResult<Self> {
        if config.frequency != Frequency::Tick
            || !matches!(
                config.execution_model,
                ExecutionModel::TradeTickV1 | ExecutionModel::QuoteTickV1
            )
        {
            return Err(failure(
                ErrorCode::InvalidRunConfig,
                "Tick撮合需要tick频率和唯一Tick模型",
            ));
        }
        Ok(Self {
            model: config.execution_model,
            participation: config.participation_rate,
            slippage_bps: config.slippage_bps,
            universe: config.universe.iter().cloned().collect(),
            last_key: None,
            quotes: BTreeMap::new(),
        })
    }

    fn match_tick(
        &self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &TickRules,
        budget: &dyn FillBudget,
        quote: &mut Option<QuoteBalance>,
    ) -> QfResult<MatchOutcome> {
        let key = event.key();
        let (session, identity, prices) = match event {
            MarketEvent::TradeTick(t) => (&t.session, &t.identity, [Some(t.price), None]),
            MarketEvent::QuoteTick(q) => {
                if q.bid.zip(q.ask).is_some_and(|(bid, ask)| bid > ask) {
                    return Err(scoped(
                        event,
                        ErrorCode::InvalidContract,
                        "交叉盘口不能作为基础Tick输入",
                    ));
                }
                (&q.session, &q.identity, [q.bid, q.ask])
            }
            MarketEvent::Bar(_) => {
                return Err(failure(ErrorCode::InvalidContract, "Tick模型不能消费Bar"));
            }
        };
        if rules.instrument.security != key.security
            || rules.facts.session != *session
            || identity.source_session != *session
        {
            return Err(scoped(
                event,
                ErrorCode::InvalidContract,
                "Tick身份/会话与适用规则不一致",
            ));
        }
        let resolved = rules
            .book
            .resolve(&rules.instrument, &rules.facts, &rules.usage)?;
        if rules.facts.status != TradingStatus::Halted {
            for price in prices.into_iter().flatten() {
                resolved.validate_execution(price)?;
            }
        }
        let mut ids = BTreeSet::new();
        for order in orders {
            crate::types::keys::label(&order.order_id, 128)?;
            if !ids.insert(&order.order_id)
                || order.security != key.security
                || !order.status.is_active()
                || order.remaining()? == Quantity::ZERO
            {
                return Err(scoped(
                    event,
                    ErrorCode::InvalidContract,
                    "活动订单重复、标的或数量状态无效",
                ));
            }
        }
        let mut outcome = MatchOutcome {
            fills: vec![],
            orders: vec![],
        };
        let selected = matches!(
            (event, self.model),
            (MarketEvent::TradeTick(_), ExecutionModel::TradeTickV1)
                | (MarketEvent::QuoteTick(_), ExecutionModel::QuoteTickV1)
        );
        // The other stream still advances D03's clock/callbacks and D06's
        // claim, but cannot match, create or refresh either quote balance.
        if !selected {
            return Ok(outcome);
        }
        let mut print_remaining = Quantity::ZERO;
        match event {
            MarketEvent::TradeTick(t) => {
                // quantity is explicitly incremental shares in qf.market.v1.
                // Cumulative source volume requires a D04-declared conversion.
                print_remaining = participation_quantity(t.quantity, self.participation)?;
            }
            MarketEvent::QuoteTick(q) => {
                let balance = quote.get_or_insert_with(|| QuoteBalance::new(q.session.clone()));
                if balance.session != q.session {
                    *balance = QuoteBalance::new(q.session.clone());
                }
                balance
                    .bid
                    .observe(q.bid, q.bid_quantity, self.participation)?;
                balance
                    .ask
                    .observe(q.ask, q.ask_quantity, self.participation)?;
            }
            MarketEvent::Bar(_) => unreachable!(),
        }
        let step = QuantityStep::new(1)?;
        for order in orders {
            if order.submitted_ns > key.time_ns
                || order
                    .eligible_after_event
                    .as_ref()
                    .is_some_and(|last| key <= *last)
                || (order.tif == TimeInForce::Day && order.effective_session != *session)
            {
                continue;
            }
            if rules.facts.status == TradingStatus::Halted {
                unfilled(&mut outcome, order, None, "已知停牌，本Tick不能成交");
                continue;
            }
            let (reference, remaining) = match event {
                MarketEvent::TradeTick(t) => (Some(t.price), &mut print_remaining),
                MarketEvent::QuoteTick(q) => {
                    let balance = quote.as_mut().expect("selected quote balance");
                    let (price, quantity, side) = match order.side {
                        Side::Buy => (q.ask, q.ask_quantity, &mut balance.ask),
                        Side::Sell => (q.bid, q.bid_quantity, &mut balance.bid),
                    };
                    if price.is_none() || quantity.is_none() {
                        unfilled(
                            &mut outcome,
                            order,
                            Some(ErrorCode::RuleUnavailable),
                            "本侧报价价格或数量未知；不能借用此前或未来盘口",
                        );
                        continue;
                    }
                    (price, &mut side.remaining)
                }
                MarketEvent::Bar(_) => unreachable!(),
            };
            if *remaining == Quantity::ZERO {
                unfilled(
                    &mut outcome,
                    order,
                    None,
                    "本侧共享参与量为零或已用完，余单保持活动",
                );
                continue;
            }
            let reference = reference.expect("known selected price");
            let slipped = reference.get().slipped_price(
                self.slippage_bps,
                resolved.rule().price_tick.get(),
                order.side == Side::Buy,
            )?;
            if slipped <= ExactDecimal::ZERO {
                unfilled(
                    &mut outcome,
                    order,
                    None,
                    "不利滑点后没有合法正价格，余单保持活动",
                );
                continue;
            }
            let price = Price::new(slipped)?;
            if order.limit_price.is_some_and(|limit| match order.side {
                Side::Buy => price > limit,
                Side::Sell => price < limit,
            }) {
                unfilled(
                    &mut outcome,
                    order,
                    None,
                    "打印/本侧报价及不利滑点未满足限价，余单保持活动",
                );
                continue;
            }
            if let DailyPriceLimit::Limited { lower, upper, .. } = rules.facts.price_limit
                && (price < lower
                    || price > upper
                    || (order.side == Side::Buy && price == upper)
                    || (order.side == Side::Sell && price == lower))
            {
                unfilled(
                    &mut outcome,
                    order,
                    None,
                    "当日价格约束或涨跌停方向无法证明可成交，余单保持活动",
                );
                continue;
            }
            resolved.validate_execution(price)?;
            let requested = order.remaining()?.min(*remaining);
            let mut candidate = Fill {
                trade_id: budget.trade_id(outcome.fills.len())?,
                order_id: order.order_id.clone(),
                security: order.security.clone(),
                side: order.side,
                quantity: requested,
                price,
                fee: ExactDecimal::ZERO,
                execution_time_ns: key.time_ns,
            };
            crate::types::keys::label(&candidate.trade_id, 128)?;
            if outcome
                .fills
                .iter()
                .any(|f| f.trade_id == candidate.trade_id)
            {
                return Err(scoped(event, ErrorCode::InvalidContract, "候选成交ID重复"));
            }
            let allowance = budget.candidate_allowance_after(&outcome.fills, &candidate, step)?;
            if allowance.requested_quantity != requested || allowance.effective_quantity > requested
            {
                return Err(scoped(
                    event,
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
            *remaining = remaining.checked_sub(quantity)?;
            candidate.quantity = quantity;
            outcome.fills.push(candidate);
            let mut changed = order.clone();
            changed.filled_quantity = changed.filled_quantity.checked_add(quantity)?;
            changed.status = if changed.filled_quantity == changed.quantity {
                OrderStatus::Filled
            } else {
                OrderStatus::PartiallyFilled
            };
            changed.reason_code = allowance.reason_code;
            changed.message = format!("Tick事件确认；{}", allowance.message);
            outcome.orders.push(changed);
        }
        Ok(outcome)
    }
}
fn participation_quantity(quantity: Quantity, participation: ExactDecimal) -> QfResult<Quantity> {
    ExactDecimal::legal_quantity(
        quantity,
        participation,
        ExactDecimal::ONE,
        QuantityStep::new(1)?,
    )
}
fn failure(code: ErrorCode, message: &str) -> QfError {
    QfError::new(code, "tick_matching", message)
}
fn scoped(event: &MarketEvent, code: ErrorCode, message: &str) -> QfError {
    let mut error = failure(code, message);
    error
        .scope
        .insert("security".into(), event.key().security.as_str().into());
    error
}
fn unfilled(outcome: &mut MatchOutcome, order: &Order, reason: Option<ErrorCode>, message: &str) {
    // Only changed business order state, never one success record per Tick.
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
impl Matcher for TickMatcher {
    type Rules = TickRules;
    fn model_description(&self) -> Option<ExecutionModelDescription> {
        Some(ExecutionModelDescription {
            model: self.model, frequency: Frequency::Tick, participation_rate: self.participation,
            slippage_bps: self.slippage_bps, fill_unit: QuantityStep::new(1).expect("integer shares"),
            price_reference: match self.model { ExecutionModel::TradeTickV1 => "生效后打印成交价", _ => "生效后本事件买ask/卖bid" }.into(),
            confirmation: "按D03完整总序事件确认；execution_time_ns保留原始ns，新命令不回填触发事件".into(),
            price_rounding: "不利滑点原始比例一次取tick：买向上、卖向下；违反限价/日价格约束不成交".into(),
            liquidity: "打印共享floor(quantity*参与率)；报价bid/ask独立共享剩余量，同价同量不补额，增量floor(delta*参与率)，减量裁剪，新价格/会话建额".into(),
            assumptions: vec![
                "quantity只接受明确增量份额，不推测累计成交量；缺报价量是unknown，不能补零/前填或借未来盘口".into(),
                "单run仅所选Tick模型提供流动性；其他Tick流保留研究/回调但不匹配或刷新余额".into(),
                "未知报价间隙保留已消耗余额，同价再次出现不会恢复；新会话需要本会话的有效报价".into(),
                "按D06稳定自有订单优先序，无真实盘口排队、逐单簿、补单重建或精确撤单延迟".into(),
                "涨停不撮合买入、跌停不撮合卖出；零滑点是明示理想化假设，费用由唯一账户累计核定".into(),
            ],
        })
    }
    fn consume(
        &mut self,
        _event: &MarketEvent,
        _orders: &[Order],
        _rules: &Self::Rules,
    ) -> QfResult<MatchOutcome> {
        Err(failure(
            ErrorCode::CapabilityUnavailable,
            "Tick撮合必须接入D06/D09真实候选预算",
        ))
    }
    fn consume_with_budget(
        &mut self,
        event: &MarketEvent,
        orders: &[Order],
        rules: &Self::Rules,
        budget: &dyn FillBudget,
    ) -> QfResult<MatchOutcome> {
        event.validate()?;
        let key = event.key();
        if orders.len() > crate::results::MAX_PAGE_ITEMS {
            return Err(failure(
                ErrorCode::ResourceLimit,
                "Tick候选活动订单超过共享数量预算",
            ));
        }
        if !self.universe.contains(&key.security)
            || self.last_key.as_ref().is_some_and(|last| &key <= last)
        {
            return Err(scoped(
                event,
                ErrorCode::InvalidContract,
                "Tick未授权、完整事件键重复或反序",
            ));
        }
        // Prepare only this security's tiny balance. Candidate errors leave
        // both cursor/balances and the unique live account untouched.
        let mut quote = self.quotes.get(&key.security).cloned();
        let outcome = self.match_tick(event, orders, rules, budget, &mut quote)?;
        if let Some(quote) = quote {
            self.quotes.insert(key.security.clone(), quote);
        }
        self.last_key = Some(key);
        Ok(outcome)
    }
}
