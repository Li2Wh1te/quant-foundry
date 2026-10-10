//! One bounded mutable account. Completed orders/trades belong to ResultSink.
use super::{
    AccountPort, AccountTerms, AccountView, ActionState, CorporateAction, SaleProceedsTiming,
    ValuationMark,
};
use crate::clock::CalendarSession;
use crate::matching::Fill;
use crate::orders::{IntentValue, OrderIntent, OrderResult, OrderStyle, Side, TimeInForce};
use crate::rules::fees::{FeeCharge, FeeScope, OrderFeeAccumulator};
use crate::rules::market::{RuleUse, SellAvailability, TradingStatus};
use crate::types::{
    ExactDecimal as D, Money, Nanoseconds, Price, Quantity, QuantityStep, RoundingPolicy,
    SecurityKey, SessionKey,
};
use crate::{ErrorCode, QfError, QfResult};
use std::collections::BTreeMap;
use std::sync::Arc;

pub(super) fn error(code: ErrorCode, message: &str) -> QfError {
    QfError::new(code, "accounting", message)
}
fn range() -> QfError {
    error(ErrorCode::NumericRangeUnsupported, "账户数值超出受支持范围")
}
pub(super) fn amount(price: Price, quantity: Quantity) -> QfResult<Money> {
    price.get().checked_mul(D::from_integer(quantity.get()))
}
pub(super) fn cost(value: Money) -> QfResult<Money> {
    value.round(crate::types::numeric::COST_SCALE, RoundingPolicy::HalfEven)
}

#[derive(Debug, Clone, Copy)]
pub struct AccountLimits {
    pub max_positions: usize,
    pub max_active_orders: usize,
    pub max_lots: usize,
    pub max_actions: usize,
    pub max_tax_attachments: usize,
}
impl Default for AccountLimits {
    fn default() -> Self {
        Self {
            max_positions: 10_000,
            max_active_orders: 10_000,
            max_lots: 100_000,
            max_actions: 10_000,
            max_tax_attachments: 100_000,
        }
    }
}
impl AccountLimits {
    fn validate(self) -> QfResult<()> {
        if self.max_positions == 0
            || self.max_positions > 10_000
            || self.max_active_orders == 0
            || self.max_active_orders > 10_000
            || self.max_lots == 0
            || self.max_lots > 1_000_000
            || self.max_actions == 0
            || self.max_actions > 100_000
            || self.max_tax_attachments == 0
            || self.max_tax_attachments > 1_000_000
        {
            return Err(error(ErrorCode::ResourceLimit, "账户内存预算无效"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountTotals {
    /// Trading realized P&L plus earned dividends less dividend tax.
    pub realized_pnl: Money,
    pub trading_fees: Money,
    pub dividend_income: Money,
    pub dividend_tax: Money,
}
impl Default for AccountTotals {
    fn default() -> Self {
        Self {
            realized_pnl: Money::ZERO,
            trading_fees: Money::ZERO,
            dividend_income: Money::ZERO,
            dividend_tax: Money::ZERO,
        }
    }
}
#[derive(Debug, Clone)]
pub struct ReservationRequest {
    /// D06 owns globally unique order identity/lifecycle. Never reuse an ID.
    pub order_id: String,
    pub security: SecurityKey,
    pub side: Side,
    pub quantity: Quantity,
    pub estimate_price: Price,
    pub limit_price: Option<Price>,
    pub tif: TimeInForce,
    pub effective_session: SessionKey,
    pub submitted_ns: Nanoseconds,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReservationView {
    pub order_id: String,
    pub security: SecurityKey,
    pub side: Side,
    pub remaining: Quantity,
    pub frozen_cash: Money,
    pub fee_paid: Money,
    pub cumulative_notional: Money,
}
#[derive(Debug, Clone)]
pub(super) struct Reservation {
    pub request: ReservationRequest,
    pub remaining: Quantity,
    pub cash: Money,
    pub fees: OrderFeeAccumulator,
    pub scope: FeeScope,
    pub effective_index: usize,
    pub last_fill: Option<(Nanoseconds, String)>,
}
#[derive(Debug, Clone)]
pub(super) struct TaxAttachment {
    pub action_id: String,
    pub quantity: Quantity,
    pub gross: Money,
    pub initial_tax: Money,
}
#[derive(Debug, Clone)]
pub(super) struct Lot {
    pub acquired: crate::rules::date::RuleDate,
    pub quantity: Quantity,
    pub sellable_from: usize,
    pub taxes: Vec<TaxAttachment>,
}
#[derive(Debug, Clone)]
pub(super) struct Position {
    pub quantity: Quantity,
    pub frozen: Quantity,
    pub total_cost: Money,
    pub lots: Vec<Lot>,
}
impl Default for Position {
    fn default() -> Self {
        Self {
            quantity: Quantity::ZERO,
            frozen: Quantity::ZERO,
            total_cost: Money::ZERO,
            lots: vec![],
        }
    }
}
impl Position {
    pub fn eligible(&self, index: usize) -> QfResult<Quantity> {
        self.lots
            .iter()
            .filter(|l| l.sellable_from <= index)
            .try_fold(Quantity::ZERO, |n, l| n.checked_add(l.quantity))
    }
    pub fn sellable(&self, index: usize) -> QfResult<Quantity> {
        self.eligible(index)?.checked_sub(self.frozen)
    }
    pub fn attachments(&self) -> usize {
        self.lots.iter().map(|l| l.taxes.len()).sum()
    }
}
#[derive(Debug, Clone)]
pub(super) struct State {
    pub cash: Money,
    pub order_frozen: Money,
    pub blocked_cash: Money,
    pub cash_locks: BTreeMap<usize, Money>,
    pub receivables: Money,
    pub positions: BTreeMap<SecurityKey, Position>,
    pub reservations: BTreeMap<String, Reservation>,
    pub terms: BTreeMap<SecurityKey, AccountTerms>,
    pub agreements: BTreeMap<SecurityKey, super::terms::AcceptedFees>,
    pub investor: Option<crate::rules::fees::InvestorKind>,
    pub marks: BTreeMap<SecurityKey, ValuationMark>,
    pub actions: BTreeMap<String, ActionState>,
    pub security_actions: BTreeMap<SecurityKey, Vec<String>>,
    pub next_payment: Option<(Nanoseconds, String)>,
    pub totals: AccountTotals,
    pub current: usize,
    pub settled: Option<usize>,
    pub lots: usize,
    pub attachments: usize,
    pub next_order: u64,
    pub last_action_boundary: Option<Nanoseconds>,
    pub as_of: Nanoseconds,
}
/// No public Clone/serde snapshot or externally mutable portfolio.
#[derive(Debug)]
pub struct Account {
    pub(super) state: State,
    pub(super) sessions: Arc<Vec<CalendarSession>>,
    session_index: Arc<BTreeMap<SessionKey, usize>>,
    pub(super) usage: RuleUse,
    pub(super) limits: AccountLimits,
    owner: Arc<()>,
    version: u64,
}
/// A short-lived candidate for atomic D06 target replacement/session transitions.
/// Private state cannot be persisted or applied to a different account/revision.
pub struct PreparedAccount {
    state: State,
    owner: Arc<()>,
    version: u64,
}
pub(super) struct FillPlan {
    pub reservation: Reservation,
    pub position: Position,
    pub cash: Money,
    pub frozen: Money,
    pub blocked: Money,
    pub cash_lock: Option<(usize, Money)>,
    pub totals: AccountTotals,
    pub actions: BTreeMap<String, ActionState>,
    pub charge: FeeCharge,
}

impl Account {
    pub fn new(
        initial_cash: Money,
        sessions: Vec<CalendarSession>,
        usage: RuleUse,
        limits: AccountLimits,
    ) -> QfResult<Self> {
        limits.validate()?;
        if initial_cash <= Money::ZERO {
            return Err(error(
                ErrorCode::InvalidRunConfig,
                "初始资金必须明确为正CNY金额",
            ));
        }
        if sessions.is_empty() || sessions.len() > crate::clock::calendar::MAX_CALENDAR_SESSIONS {
            return Err(error(ErrorCode::ResourceLimit, "账户日历缺失或超限"));
        }
        let mut session_index = BTreeMap::new();
        for (i, row) in sessions.iter().enumerate() {
            row.validate()?;
            if i > 0
                && (sessions[i - 1].after_close_ns >= row.before_open_ns
                    || sessions[i - 1].date >= row.date)
                || session_index.insert(row.session.key.clone(), i).is_some()
            {
                return Err(error(ErrorCode::RuleUnavailable, "账户日历反序或会话重复"));
            }
        }
        let as_of = sessions[0].before_open_ns;
        Ok(Self {
            state: State {
                cash: initial_cash,
                order_frozen: Money::ZERO,
                blocked_cash: Money::ZERO,
                cash_locks: BTreeMap::new(),
                receivables: Money::ZERO,
                positions: BTreeMap::new(),
                reservations: BTreeMap::new(),
                terms: BTreeMap::new(),
                agreements: BTreeMap::new(),
                investor: None,
                marks: BTreeMap::new(),
                actions: BTreeMap::new(),
                security_actions: BTreeMap::new(),
                next_payment: None,
                totals: AccountTotals::default(),
                current: 0,
                settled: None,
                lots: 0,
                attachments: 0,
                next_order: 1,
                last_action_boundary: None,
                as_of,
            },
            sessions: Arc::new(sessions),
            session_index: Arc::new(session_index),
            usage,
            limits,
            owner: Arc::new(()),
            version: 0,
        })
    }
    pub(super) fn index(&self, key: &SessionKey) -> QfResult<usize> {
        self.session_index
            .get(key)
            .copied()
            .ok_or_else(|| error(ErrorCode::RuleUnavailable, "缺少权威会话/结算日期映射"))
    }
    pub(super) fn next_index(&self) -> QfResult<usize> {
        let next = self.state.current.checked_add(1).ok_or_else(range)?;
        if next >= self.sessions.len() {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "下一结算会话未知，不推算自然日",
            ));
        }
        Ok(next)
    }
    pub(super) fn bump(&mut self) -> QfResult<()> {
        self.version = self.version.checked_add(1).ok_or_else(range)?;
        Ok(())
    }
    pub fn totals(&self) -> &AccountTotals {
        &self.state.totals
    }
    pub fn available_cash(&self) -> QfResult<Money> {
        self.state.cash.checked_sub(self.frozen_cash()?)
    }
    pub fn frozen_cash(&self) -> QfResult<Money> {
        self.state.order_frozen.checked_add(self.state.blocked_cash)
    }
    pub fn reservation(&self, id: &str) -> Option<ReservationView> {
        self.state.reservations.get(id).map(|r| ReservationView {
            order_id: id.into(),
            security: r.request.security.clone(),
            side: r.request.side,
            remaining: r.remaining,
            frozen_cash: r.cash,
            fee_paid: r.fees.total_paid(),
            cumulative_notional: r.fees.cumulative_notional(),
        })
    }
    pub fn active_reservation_count(&self) -> usize {
        self.state.reservations.len()
    }
    pub fn held_quantity(&self, security: &SecurityKey) -> Quantity {
        self.state
            .positions
            .get(security)
            .map_or(Quantity::ZERO, |p| p.quantity)
    }
    pub fn sellable_quantity(&self, security: &SecurityKey) -> QfResult<Quantity> {
        self.state
            .positions
            .get(security)
            .map_or(Ok(Quantity::ZERO), |p| p.sellable(self.state.current))
    }
    pub fn install_terms(&mut self, terms: AccountTerms) -> QfResult<()> {
        let row = &self.sessions[self.state.current];
        if terms.facts.session != row.session.key
            || terms.facts.date != row.date
            || terms.usage() != &self.usage
        {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "账户规则不是当前会话/日期/模型",
            ));
        }
        if terms.holding_bands.len() > self.limits.max_lots {
            return Err(error(ErrorCode::ResourceLimit, "持有期事实超过预算"));
        }
        if self
            .state
            .agreements
            .get(&terms.instrument.security)
            .is_some_and(|accepted| accepted != &terms.agreement())
            || self
                .state
                .investor
                .is_some_and(|investor| investor != terms.buy_fees.scope().investor)
        {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "已受理账户佣金/投资者配置不能在运行中改变",
            ));
        }
        if !self.state.terms.contains_key(&terms.instrument.security)
            && self.state.terms.len() >= self.limits.max_positions
        {
            return Err(error(ErrorCode::ResourceLimit, "当日账户事实超过预算"));
        }
        self.bump()?;
        self.state
            .terms
            .insert(terms.instrument.security.clone(), terms);
        Ok(())
    }
    pub(super) fn terms(&self, security: &SecurityKey) -> QfResult<&AccountTerms> {
        self.state
            .terms
            .get(security)
            .filter(|t| t.facts.session == self.sessions[self.state.current].session.key)
            .ok_or_else(|| {
                let mut e = error(ErrorCode::RuleUnavailable, "当日账户规则/费用事实未提供");
                e.scope.insert("security".into(), security.as_str().into());
                e
            })
    }
    /// Pure candidate work; D06 stages its order changes separately and commits
    /// both only after every fallible validation has succeeded.
    pub fn preview<T>(
        &self,
        work: impl FnOnce(&mut Self) -> QfResult<T>,
    ) -> QfResult<(T, PreparedAccount)> {
        let mut candidate = Self {
            state: self.state.clone(),
            sessions: Arc::clone(&self.sessions),
            session_index: Arc::clone(&self.session_index),
            usage: self.usage.clone(),
            limits: self.limits,
            owner: Arc::clone(&self.owner),
            version: self.version,
        };
        let result = work(&mut candidate)?;
        Ok((
            result,
            PreparedAccount {
                state: candidate.state,
                owner: Arc::clone(&self.owner),
                version: self.version,
            },
        ))
    }
    pub fn commit(&mut self, prepared: PreparedAccount) -> QfResult<()> {
        if !Arc::ptr_eq(&prepared.owner, &self.owner) || prepared.version != self.version {
            return Err(error(
                ErrorCode::InvalidContract,
                "账户候选已过期或属于另一资金池",
            ));
        }
        self.bump()?;
        self.state = prepared.state;
        Ok(())
    }
    pub fn transact<T>(&mut self, work: impl FnOnce(&mut Self) -> QfResult<T>) -> QfResult<T> {
        let (result, prepared) = self.preview(work)?;
        self.commit(prepared)?;
        Ok(result)
    }
    pub fn reserve_order(&mut self, request: ReservationRequest) -> QfResult<ReservationView> {
        crate::types::keys::label(&request.order_id, 128)?;
        let security = request.security.clone();
        let id = request.order_id.clone();
        self.reserve_inner(request).map_err(|mut e| {
            e.scope.insert("security".into(), security.as_str().into());
            e.scope.insert("order_id".into(), id);
            e.scope.insert(
                "date".into(),
                self.sessions[self.state.current].date.to_string(),
            );
            e
        })
    }
    fn reserve_inner(&mut self, request: ReservationRequest) -> QfResult<ReservationView> {
        crate::types::keys::label(&request.order_id, 128)?;
        self.ensure_payments_ready(request.submitted_ns)?;
        if request.submitted_ns < self.state.as_of {
            return Err(error(ErrorCode::InvalidOrder, "委托时间早于当前账户状态"));
        }
        if request.submitted_ns > self.sessions[self.state.current].after_close_ns
            || self.state.settled.is_none()
                && request.submitted_ns != self.sessions[0].before_open_ns
        {
            return Err(error(ErrorCode::InvalidOrder, "委托越过当前已结算会话边界"));
        }
        if self.state.reservations.contains_key(&request.order_id) {
            return Err(error(ErrorCode::InvalidOrder, "订单已有预留"));
        }
        if self.state.reservations.len() >= self.limits.max_active_orders {
            return Err(error(ErrorCode::ResourceLimit, "活动账户预留超过预算"));
        }
        let terms = self.terms(&request.security)?;
        if self
            .state
            .investor
            .is_some_and(|investor| investor != terms.buy_fees.scope().investor)
        {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "交易投资者与本运行账户不一致",
            ));
        }
        let agreement = terms.agreement();
        if terms.facts.status != TradingStatus::Trading {
            return Err(error(ErrorCode::InvalidOrder, "标的当前不可交易"));
        }
        let effective_index = self.index(&request.effective_session)?;
        if effective_index < self.state.current || effective_index > self.state.current + 1 {
            return Err(error(
                ErrorCode::InvalidOrder,
                "订单有效会话不是当前/下一会话",
            ));
        }
        let available = self.sellable_quantity(&request.security)?;
        terms.validate_quantity(
            request.side,
            request.quantity,
            available,
            request.limit_price.is_none(),
        )?;
        terms.validate_price(request.estimate_price)?;
        if let Some(limit) = request.limit_price {
            terms.validate_price(limit)?;
        }
        let fees = OrderFeeAccumulator::new(&request.order_id, terms.fees(request.side).clone())?;
        let scope = terms.fees(request.side).scope().clone();
        let notional = amount(request.estimate_price, request.quantity)?;
        let (charge, _) = fees.preview_fill(&scope, &terms.facts.date, notional)?;
        let available_cash = self.available_cash()?;
        let mut position = self
            .state
            .positions
            .get(&request.security)
            .cloned()
            .unwrap_or_default();
        let cash = if request.side == Side::Buy {
            // Accept underfunded long orders only if at least one legal order
            // unit can be paid now. D06/D07 use max_affordable at actual price.
            let minimum = terms.resolved()?.rule().minimum_buy;
            let min_notional = amount(request.estimate_price, minimum)?;
            let (min_fee, _) = fees.preview_fill(&scope, &terms.facts.date, min_notional)?;
            if min_notional.checked_add(min_fee.total)? > available_cash {
                return Err(error(
                    ErrorCode::InsufficientCash,
                    "没有可支付的合法买入数量",
                ));
            }
            notional.checked_add(charge.total)?.min(available_cash)
        } else {
            let (_, tax, _) = self.dispose(&position, request.quantity, terms)?;
            let needed = charge
                .total
                .checked_add(tax)?
                .checked_sub(notional)?
                .max(Money::ZERO);
            if needed > available_cash {
                return Err(error(
                    ErrorCode::InsufficientCash,
                    "卖出收入不足以支付费用/税款",
                ));
            }
            position.frozen = position.frozen.checked_add(request.quantity)?;
            needed
        };
        let frozen = self.state.order_frozen.checked_add(cash)?;
        let reservation = Reservation {
            remaining: request.quantity,
            cash,
            fees,
            scope,
            effective_index,
            request,
            last_fill: None,
        };
        let view = ReservationView {
            order_id: reservation.request.order_id.clone(),
            security: reservation.request.security.clone(),
            side: reservation.request.side,
            remaining: reservation.remaining,
            frozen_cash: cash,
            fee_paid: Money::ZERO,
            cumulative_notional: Money::ZERO,
        };
        self.bump()?;
        self.state.order_frozen = frozen;
        self.state.as_of = reservation.request.submitted_ns;
        self.state.investor = Some(reservation.scope.investor);
        self.state
            .agreements
            .entry(reservation.request.security.clone())
            .or_insert(agreement);
        if reservation.request.side == Side::Sell {
            self.state
                .positions
                .insert(reservation.request.security.clone(), position);
        }
        self.state
            .reservations
            .insert(reservation.request.order_id.clone(), reservation);
        Ok(view)
    }
    pub fn recheck_order(&self, id: &str) -> QfResult<()> {
        let r = self
            .state
            .reservations
            .get(id)
            .ok_or_else(|| error(ErrorCode::InvalidOrder, "订单预留不存在"))?;
        let terms = self.terms(&r.request.security)?;
        if terms.facts.status != TradingStatus::Trading {
            return Err(error(ErrorCode::InvalidOrder, "跨日订单当日不可交易"));
        }
        terms
            .fees(r.request.side)
            .validate_date(&terms.facts.date)?;
        if let Some(limit) = r.request.limit_price {
            terms.validate_price(limit)?;
        }
        // Preview the *original* accumulator: mandatory dated rates must be
        // covered; changing today's commission must not reset an open order.
        r.fees.preview_fill(
            &r.scope,
            &terms.facts.date,
            amount(r.request.estimate_price, r.remaining)?,
        )?;
        Ok(())
    }
    /// No rounded ratio/binary-float search. Monotone fee liabilities are
    /// previewed on the same accumulator. This never advances fees or orders.
    pub fn max_affordable(
        &self,
        id: &str,
        requested: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<Quantity> {
        let r = self
            .state
            .reservations
            .get(id)
            .ok_or_else(|| error(ErrorCode::InvalidOrder, "订单预留不存在"))?;
        if r.request.side != Side::Buy {
            return Err(error(ErrorCode::InvalidOrder, "可支付量只用于买入"));
        }
        let terms = self.terms(&r.request.security)?;
        terms.validate_price(price)?;
        let budget = self.available_cash()?.checked_add(r.cash)?;
        let mut low = 0_i64;
        let mut high = requested.min(r.remaining).get() / step.get();
        while low < high {
            let middle = low + (high - low) / 2 + (high - low) % 2;
            let quantity = Quantity::new(middle.checked_mul(step.get()).ok_or_else(range)?)?;
            let value = amount(price, quantity)?;
            let (fee, _) = r.fees.preview_fill(&r.scope, &terms.facts.date, value)?;
            if value.checked_add(fee.total)? <= budget {
                low = middle;
            } else {
                high = middle - 1;
            }
        }
        Quantity::new(low.checked_mul(step.get()).ok_or_else(range)?)
    }
    fn fill_plan(&self, fill: &Fill) -> QfResult<FillPlan> {
        crate::types::keys::label(&fill.trade_id, 128)?;
        let r = self
            .state
            .reservations
            .get(&fill.order_id)
            .ok_or_else(|| error(ErrorCode::InvalidContract, "成交没有活动账户预留"))?;
        let terms = self.terms(&fill.security)?;
        let row = &self.sessions[self.state.current];
        if self.state.settled != Some(self.state.current) {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "当前会话尚未结算，不能应用成交",
            ));
        }
        if fill.security != r.request.security
            || fill.side != r.request.side
            || fill.quantity == Quantity::ZERO
            || fill.quantity > r.remaining
            || fill.execution_time_ns < row.session.open_ns
            || fill.execution_time_ns > row.session.close_ns
            || fill.execution_time_ns < r.request.submitted_ns
            || r.effective_index > self.state.current
            || fill.execution_time_ns < self.state.as_of
            || r.last_fill
                .as_ref()
                .is_some_and(|(time, id)| fill.execution_time_ns < *time || &fill.trade_id == id)
        {
            return Err(error(
                ErrorCode::InvalidContract,
                "成交身份、方向、时间、数量或重复调用无效",
            ));
        }
        if terms.facts.status != TradingStatus::Trading {
            return Err(error(ErrorCode::InvalidOrder, "已知不可交易状态不能成交"));
        }
        if let Some(limit) = r.request.limit_price
            && (fill.side == Side::Buy && fill.price > limit
                || fill.side == Side::Sell && fill.price < limit)
        {
            return Err(error(ErrorCode::InvalidOrder, "成交价格违反限价"));
        }
        self.ensure_actions_ready(&fill.security, fill.execution_time_ns)?;
        terms.resolved()?.validate_execution(fill.price)?;
        let notional = amount(fill.price, fill.quantity)?;
        let (charge, next_fees) = r.fees.preview_fill(&r.scope, &terms.facts.date, notional)?;
        let mut next = r.clone();
        next.fees = next_fees;
        next.remaining = r.remaining.checked_sub(fill.quantity)?;
        next.last_fill = Some((fill.execution_time_ns, fill.trade_id.clone()));
        let original = self
            .state
            .positions
            .get(&fill.security)
            .cloned()
            .unwrap_or_default();
        let mut position = original.clone();
        let mut totals = self.state.totals.clone();
        totals.trading_fees = totals.trading_fees.checked_add(charge.total)?;
        let other_frozen = self.frozen_cash()?.checked_sub(r.cash)?;
        let mut actions = BTreeMap::new();
        let mut blocked = self.state.blocked_cash;
        let mut cash_lock = None;
        let cash;
        if fill.side == Side::Buy {
            let spent = notional.checked_add(charge.total)?;
            if spent > self.state.cash.checked_sub(other_frozen)? {
                return Err(error(
                    ErrorCode::InsufficientCash,
                    "跳空后成交超过真实可支付资金",
                ));
            }
            cash = self.state.cash.checked_sub(spent)?;
            position.quantity = position.quantity.checked_add(fill.quantity)?;
            position.total_cost = cost(position.total_cost.checked_add(spent)?)?;
            let sellable_from = match terms.sell_availability()? {
                SellAvailability::SameSession => self.state.current,
                SellAvailability::NextSession => self.next_index()?,
                SellAvailability::CrossBorderUnderlying => unreachable!("D02 resolves underlying"),
            };
            // Same-date holdings coalesce until registration attaches claims.
            if let Some(lot) = position.lots.last_mut().filter(|l| {
                l.acquired == terms.facts.date
                    && l.sellable_from == sellable_from
                    && l.taxes.is_empty()
            }) {
                lot.quantity = lot.quantity.checked_add(fill.quantity)?;
            } else {
                position.lots.push(Lot {
                    acquired: terms.facts.date.clone(),
                    quantity: fill.quantity,
                    sellable_from,
                    taxes: vec![],
                });
            }
            next.cash = if next.remaining == Quantity::ZERO {
                Money::ZERO
            } else {
                let rest = amount(r.request.estimate_price, next.remaining)?;
                let (fee, _) = next.fees.preview_fill(&r.scope, &terms.facts.date, rest)?;
                rest.checked_add(fee.total)?
                    .min(cash.checked_sub(other_frozen)?)
            };
        } else {
            if fill.quantity > position.frozen {
                return Err(error(
                    ErrorCode::InsufficientSellable,
                    "卖出成交超过本账户冻结数量",
                ));
            }
            let (disposed, tax, tax_changes) = self.dispose(&position, fill.quantity, terms)?;
            position = disposed;
            actions = tax_changes;
            position.frozen = position.frozen.checked_sub(fill.quantity)?;
            let (released, remaining) =
                D::release_cost(position.total_cost, original.quantity, fill.quantity)?;
            position.quantity = position.quantity.checked_sub(fill.quantity)?;
            position.total_cost = remaining;
            let proceeds = notional.checked_sub(charge.total)?.checked_sub(tax)?;
            cash = self.state.cash.checked_add(proceeds)?;
            if cash < other_frozen {
                return Err(error(
                    ErrorCode::InsufficientCash,
                    "费用/分红税会侵占其他订单冻结现金",
                ));
            }
            totals.realized_pnl = totals
                .realized_pnl
                .checked_add(proceeds.checked_sub(released)?)?;
            totals.dividend_tax = totals.dividend_tax.checked_add(tax)?;
            if terms.proceeds.timing == SaleProceedsTiming::NextSession && proceeds > Money::ZERO {
                let due = self.next_index()?;
                let lock = self
                    .state
                    .cash_locks
                    .get(&due)
                    .copied()
                    .unwrap_or(Money::ZERO)
                    .checked_add(proceeds)?;
                blocked = blocked.checked_add(proceeds)?;
                cash_lock = Some((due, lock));
            }
            // Do not re-charge a minimum on cancellation/remaining portions.
            next.cash = if next.remaining == Quantity::ZERO {
                Money::ZERO
            } else {
                r.cash.min(cash.checked_sub(other_frozen)?)
            };
        }
        let frozen = self
            .state
            .order_frozen
            .checked_sub(r.cash)?
            .checked_add(next.cash)?;
        if frozen.checked_add(blocked)? > cash {
            return Err(error(ErrorCode::InsufficientCash, "成交后冻结现金超过余额"));
        }
        self.check_position_limits(&fill.security, &position)?;
        Ok(FillPlan {
            reservation: next,
            position,
            cash,
            frozen,
            blocked,
            cash_lock,
            totals,
            actions,
            charge,
        })
    }
    pub fn quote_fill(&self, fill: &Fill) -> QfResult<Fill> {
        crate::types::keys::label(&fill.order_id, 128)?;
        let plan = self.fill_plan(fill).map_err(|e| self.fill_error(fill, e))?;
        let mut assessed = fill.clone();
        assessed.fee = plan.charge.total;
        Ok(assessed)
    }
    fn fill_error(&self, fill: &Fill, mut e: QfError) -> QfError {
        e.scope
            .insert("security".into(), fill.security.as_str().into());
        e.scope.insert("order_id".into(), fill.order_id.clone());
        e.scope.insert(
            "date".into(),
            self.sessions[self.state.current].date.to_string(),
        );
        e
    }
    pub(super) fn check_position_limits(
        &self,
        key: &SecurityKey,
        position: &Position,
    ) -> QfResult<()> {
        let old = self.state.positions.get(key);
        let lots = self.state.lots - old.map_or(0, |p| p.lots.len()) + position.lots.len();
        let attachments =
            self.state.attachments - old.map_or(0, Position::attachments) + position.attachments();
        if lots > self.limits.max_lots
            || attachments > self.limits.max_tax_attachments
            || position.quantity != Quantity::ZERO
                && old.is_none()
                && self.state.positions.len() >= self.limits.max_positions
        {
            return Err(error(ErrorCode::ResourceLimit, "持仓或税务份额超过预算"));
        }
        Ok(())
    }
    pub(super) fn put_position(&mut self, key: SecurityKey, position: Position) {
        let old = self.state.positions.get(&key);
        self.state.lots = self.state.lots - old.map_or(0, |p| p.lots.len()) + position.lots.len();
        self.state.attachments =
            self.state.attachments - old.map_or(0, Position::attachments) + position.attachments();
        if position.quantity == Quantity::ZERO {
            self.state.positions.remove(&key);
        } else {
            self.state.positions.insert(key, position);
        }
    }
    pub(super) fn ensure_actions_ready(
        &self,
        security: &SecurityKey,
        now: Nanoseconds,
    ) -> QfResult<()> {
        self.ensure_payments_ready(now)?;
        for id in self
            .state
            .security_actions
            .get(security)
            .into_iter()
            .flatten()
        {
            let a = &self.state.actions[id];
            let record = self.action_time(&a.action.record)?;
            let ex = self.action_time(&a.action.ex)?;
            if !a.registered && record < now || !a.ex_applied && ex <= now {
                return Err(error(
                    ErrorCode::RuleUnavailable,
                    "公司行动边界未处理，拒绝继续成交",
                ));
            }
        }
        Ok(())
    }
    pub fn add_corporate_action(
        &mut self,
        action: CorporateAction,
        now: Nanoseconds,
    ) -> QfResult<()> {
        self.validate_action(&action, now)?;
        if self.state.actions.contains_key(&action.action_id) {
            return Err(error(ErrorCode::InvalidContract, "公司行动身份重复"));
        }
        if self.state.actions.len() >= self.limits.max_actions {
            return Err(error(ErrorCode::ResourceLimit, "公司行动超过预算"));
        }
        let payment = match &action.kind {
            super::CorporateActionKind::CashDividend { payment, .. } => {
                Some((self.action_time(payment)?, action.action_id.clone()))
            }
            _ => None,
        };
        self.bump()?;
        if let Some(payment) = payment
            && self
                .state
                .next_payment
                .as_ref()
                .is_none_or(|old| &payment < old)
        {
            self.state.next_payment = Some(payment);
        }
        self.state
            .security_actions
            .entry(action.security.clone())
            .or_default()
            .push(action.action_id.clone());
        self.state
            .actions
            .insert(action.action_id.clone(), ActionState::new(action));
        Ok(())
    }
}

impl AccountPort for Account {
    fn validate_and_reserve(&mut self, intent: &OrderIntent) -> QfResult<OrderResult> {
        intent.validate()?;
        let IntentValue::Quantity(quantity) = intent.value else {
            return Err(error(
                ErrorCode::InvalidContract,
                "D06须先原子解析目标/金额指令为合法数量",
            ));
        };
        if quantity == Quantity::ZERO {
            return Ok(OrderResult {
                accepted: true,
                order_id: None,
                reason_code: None,
                message: String::new(),
                unchanged: true,
                requested_quantity: Some(quantity),
                effective_quantity: Some(quantity),
            });
        }
        let limit_price = match intent.style {
            OrderStyle::Market => None,
            OrderStyle::Limit { price } => Some(price),
        };
        let estimate = limit_price
            .or_else(|| self.state.marks.get(&intent.security).map(|m| m.price))
            .ok_or_else(|| error(ErrorCode::RuleUnavailable, "没有有效原价用于资金预留"))?;
        let next_order = self.state.next_order.checked_add(1).ok_or_else(range)?;
        let id = format!("account-order-{}", self.state.next_order);
        let result = self.reserve_order(ReservationRequest {
            order_id: id.clone(),
            security: intent.security.clone(),
            side: intent.side,
            quantity,
            estimate_price: estimate,
            limit_price,
            tif: intent.tif,
            effective_session: self.sessions[self.state.current].session.key.clone(),
            submitted_ns: intent.submitted_at.time_ns,
        });
        match result {
            Ok(_) => {
                self.state.next_order = next_order;
                Ok(OrderResult {
                    accepted: true,
                    order_id: Some(id),
                    reason_code: None,
                    message: String::new(),
                    unchanged: false,
                    requested_quantity: Some(quantity),
                    effective_quantity: Some(quantity),
                })
            }
            Err(e)
                if matches!(
                    e.code,
                    ErrorCode::InsufficientCash
                        | ErrorCode::InsufficientSellable
                        | ErrorCode::InvalidOrder
                        | ErrorCode::RuleUnavailable
                ) =>
            {
                Ok(OrderResult {
                    accepted: false,
                    order_id: None,
                    reason_code: Some(e.code),
                    message: e.message,
                    unchanged: true,
                    requested_quantity: Some(quantity),
                    effective_quantity: None,
                })
            }
            Err(e) => Err(e),
        }
    }
    fn assess_fill(&self, fill: &Fill) -> QfResult<Fill> {
        self.quote_fill(fill)
    }
    fn apply_fill(&mut self, fill: &Fill) -> QfResult<()> {
        crate::types::keys::label(&fill.order_id, 128)?;
        let plan = self.fill_plan(fill).map_err(|e| self.fill_error(fill, e))?;
        if fill.fee != plan.charge.total {
            return Err(error(
                ErrorCode::InvalidContract,
                "成交费用不是账户累计器核定值",
            ));
        }
        self.bump()?;
        self.state.cash = plan.cash;
        self.state.as_of = fill.execution_time_ns;
        self.state.order_frozen = plan.frozen;
        self.state.blocked_cash = plan.blocked;
        if let Some((due, value)) = plan.cash_lock {
            self.state.cash_locks.insert(due, value);
        }
        self.state.totals = plan.totals;
        self.state.actions.extend(plan.actions);
        self.put_position(fill.security.clone(), plan.position);
        if plan.reservation.remaining == Quantity::ZERO {
            self.state.reservations.remove(&fill.order_id);
        } else {
            self.state
                .reservations
                .insert(fill.order_id.clone(), plan.reservation);
        }
        Ok(())
    }
    fn release(&mut self, order_id: &str) -> QfResult<()> {
        let Some(r) = self.state.reservations.get(order_id) else {
            return Ok(());
        };
        let frozen = self.state.order_frozen.checked_sub(r.cash)?;
        let security = r.request.security.clone();
        let position = if r.request.side == Side::Sell {
            let mut p = self
                .state
                .positions
                .get(&security)
                .cloned()
                .ok_or_else(|| error(ErrorCode::InvalidContract, "冻结卖单没有持仓"))?;
            p.frozen = p.frozen.checked_sub(r.remaining)?;
            Some(p)
        } else {
            None
        };
        self.bump()?;
        self.state.order_frozen = frozen;
        if let Some(p) = position {
            self.put_position(security, p);
        }
        self.state.reservations.remove(order_id);
        Ok(())
    }
    fn settle(&mut self, session: &SessionKey) -> QfResult<()> {
        let index = self.index(session)?;
        if self.state.settled == Some(index) {
            return Ok(());
        }
        if self
            .state
            .settled
            .map_or(index != 0, |last| index != last + 1)
        {
            return Err(error(ErrorCode::RuleUnavailable, "结算会话被跳过或倒退"));
        }
        if self.sessions[index].before_open_ns < self.state.as_of {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "结算边界早于当前账户状态",
            ));
        }
        let released = self
            .state
            .cash_locks
            .range(..=index)
            .try_fold(Money::ZERO, |n, (_, v)| n.checked_add(*v))?;
        let blocked = self.state.blocked_cash.checked_sub(released)?;
        self.bump()?;
        self.state.current = index;
        self.state.as_of = self.sessions[index].before_open_ns;
        self.state.settled = Some(index);
        self.state.blocked_cash = blocked;
        self.state.cash_locks.retain(|due, _| *due > index);
        // Sellability derives from each immutable lot's accepted release
        // session. Order reservations/fee accumulators survive this boundary.
        Ok(())
    }
    fn value(&self, now: Nanoseconds) -> QfResult<AccountView> {
        Ok(self.valuation(now)?.account)
    }
    fn check_session_end(&self, session: &SessionKey) -> QfResult<()> {
        self.check_closing_actions(session)
    }
}
