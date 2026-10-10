//! D06 owns order state; D09 owns every cash/position/fee mutation.
use super::*;
use crate::accounting::{Account, AccountPort, AccountTerms, CorporateAction, ValuationMark};
use crate::clock::CalendarSession;
use crate::engine::OrderActivation;
use crate::matching::{FillAllowance, FillBudget};
use crate::rules::market::{AuctionPhase, TradePermissions};
use crate::types::{EventIdentity, EventPhase, QuantityStep, Sequence};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;

/// Actual accepted dated facts, supplied by D02/D04. Missing facts propagate;
/// this interface never reads a supplier, infers a fee or installs a fallback.
pub trait OrderFacts {
    fn session_terms(&mut self, session: &CalendarSession) -> QfResult<Vec<AccountTerms>>;
    fn admission(
        &self,
        security: &SecurityKey,
        side: Side,
        session: &CalendarSession,
        now: Nanoseconds,
    ) -> QfResult<AdmissionFacts>;
    /// D13 may supply an already persisted terminal order after cache eviction.
    fn terminal_order(&self, _id: &str) -> QfResult<Order> {
        Err(failure(
            ErrorCode::CapabilityUnavailable,
            "终态订单已离开有界缓存，请使用结果分页读取",
        ))
    }
}
pub struct AdmissionFacts {
    pub instrument: crate::rules::market::Instrument,
    pub permissions: TradePermissions,
    /// Exchange-defined public cage/auction reference, never guessed from a mark.
    pub submission_reference: Option<Price>,
}
#[derive(Debug, Clone, Copy)]
pub struct OrderLimits {
    pub max_active: usize,
    pub terminal_cache: usize,
    pub max_changes: usize,
}
impl Default for OrderLimits {
    fn default() -> Self {
        Self {
            max_active: 10_000,
            terminal_cache: 1_000,
            max_changes: 10_000,
        }
    }
}
#[derive(Debug, Clone)]
pub(super) struct Entry {
    pub order: Order,
    pub sequence: u64,
    pub target: bool,
}
/// Run-local single-use market claim. Its private owner/revision/order snapshot
/// prevents unknown, cross-run and pre-command claims from committing fills.
pub struct MatchClaim {
    pub(super) owner: Arc<()>,
    pub(super) revision: u64,
    pub(super) event: crate::types::MarketEvent,
    pub(super) orders: Vec<Order>,
}
pub struct OrderManager<F> {
    pub(super) account: Account,
    pub(super) facts: F,
    pub(super) sessions: Vec<CalendarSession>,
    pub(super) session_indices: BTreeMap<SessionKey, usize>,
    pub(super) current: usize,
    pub(super) closed: bool,
    pub(super) entries: BTreeMap<String, Entry>,
    pub(super) active: BTreeMap<SecurityKey, BTreeMap<u64, String>>,
    active_count: usize,
    terminal: VecDeque<String>,
    pub(super) limits: OrderLimits,
    pub(super) next_order: u64,
    pub(super) next_trade: u64,
    /// D02's daily buy-admission counter, including buys later sold. This is
    /// order-rule state, never another holdings/cash ledger or event history.
    pub(super) session_buys: BTreeMap<SecurityKey, Quantity>,
    pub(super) revision: u64,
    pub(super) owner: Arc<()>,
    pub(super) last_event: Option<EventKey>,
    pub(super) last_command: Option<EventKey>,
    pub(super) changes: Vec<Order>,
    pub(super) effects: Vec<crate::results::CorporateActionEvent>,
}
pub(super) fn failure(code: ErrorCode, message: &str) -> QfError {
    QfError::new(code, "orders", message)
}
pub(super) fn next(value: u64) -> QfResult<u64> {
    value
        .checked_add(1)
        .ok_or_else(|| failure(ErrorCode::NumericRangeUnsupported, "订单或成交序号溢出"))
}
pub(super) fn command_after(at: &EventKey, previous: &EventKey) -> bool {
    at.time_ns >= previous.time_ns
        && at.identity.stable_input_sequence > previous.identity.stable_input_sequence
}
pub(super) fn trade_id(sequence: u64) -> String {
    format!("qf-trade-{sequence:020}")
}
pub(super) fn result(
    requested: Quantity,
    effective: Quantity,
    id: Option<String>,
    unchanged: bool,
    message: String,
) -> OrderResult {
    OrderResult {
        accepted: true,
        order_id: id,
        reason_code: None,
        message,
        unchanged,
        requested_quantity: Some(requested),
        effective_quantity: Some(effective),
    }
}
fn rejected(
    error: QfError,
    requested: Option<Quantity>,
    effective: Option<Quantity>,
) -> OrderResult {
    OrderResult {
        accepted: false,
        order_id: None,
        reason_code: Some(error.code),
        message: error.message,
        unchanged: false,
        requested_quantity: requested,
        effective_quantity: effective,
    }
}
pub(super) fn boundary_key(
    row: &CalendarSession,
    now: Nanoseconds,
    security: SecurityKey,
) -> QfResult<EventKey> {
    Ok(EventKey {
        time_ns: now,
        phase: EventPhase::Settlement,
        security,
        identity: EventIdentity {
            source_session: row.session.key.clone(),
            channel: crate::types::keys::ChannelKey::new("order_lifecycle")?,
            sequence: None,
            stable_input_sequence: Sequence::new(0),
        },
    })
}
impl<F: OrderFacts> OrderManager<F> {
    pub fn new(
        account: Account,
        facts: F,
        universe: Vec<SecurityKey>,
        limits: OrderLimits,
    ) -> QfResult<Self> {
        if universe.is_empty()
            || universe.len() > 10_000
            || universe.iter().collect::<BTreeSet<_>>().len() != universe.len()
            || [limits.max_active, limits.max_changes]
                .iter()
                .any(|n| *n == 0 || *n > 10_000)
            || limits.terminal_cache > 10_000
            || account.active_reservation_count() != 0
        {
            return Err(failure(
                ErrorCode::InvalidContract,
                "订单授权集合、预算或初始预留无效",
            ));
        }
        // Empty per-security indices also represent the fixed authorized universe.
        let active = universe.into_iter().map(|s| (s, BTreeMap::new())).collect();
        let session_indices = account
            .sessions()
            .iter()
            .enumerate()
            .map(|(i, row)| (row.session.key.clone(), i))
            .collect();
        Ok(Self {
            sessions: account.sessions().to_vec(),
            session_indices,
            account,
            facts,
            active,
            active_count: 0,
            current: 0,
            closed: false,
            entries: BTreeMap::new(),
            terminal: VecDeque::new(),
            limits,
            next_order: 1,
            next_trade: 1,
            session_buys: BTreeMap::new(),
            revision: 0,
            owner: Arc::new(()),
            last_event: None,
            last_command: None,
            changes: Vec::new(),
            effects: Vec::new(),
        })
    }
    pub fn account(&self) -> &Account {
        &self.account
    }
    pub fn add_corporate_action(
        &mut self,
        action: CorporateAction,
        now: Nanoseconds,
    ) -> QfResult<()> {
        let revision = next(self.revision)?;
        self.account.add_corporate_action(action, now)?;
        self.revision = revision;
        Ok(())
    }
    pub fn set_raw_mark(
        &mut self,
        security: SecurityKey,
        mark: ValuationMark,
        now: Nanoseconds,
    ) -> QfResult<()> {
        let revision = next(self.revision)?;
        self.account.set_raw_mark(security, mark, now)?;
        self.revision = revision;
        Ok(())
    }
    pub fn get_order(&self, id: &str) -> QfResult<Order> {
        crate::types::keys::label(id, 128)?;
        if let Some(entry) = self.entries.get(id) {
            return Ok(entry.order.clone());
        }
        if !self.issued(id) {
            return Err(failure(ErrorCode::InvalidOrder, "订单 ID 未在本次运行受理"));
        }
        let order = self.facts.terminal_order(id)?;
        if order.order_id != id
            || order.status.is_active()
            || order.filled_quantity > order.quantity
        {
            return Err(failure(
                ErrorCode::InvalidContract,
                "持久层返回了错误的终态订单",
            ));
        }
        Ok(order)
    }
    fn issued(&self, id: &str) -> bool {
        let Some(sequence) = id
            .strip_prefix("qf-order-")
            .and_then(|value| value.parse::<u64>().ok())
        else {
            return false;
        };
        sequence != 0 && sequence < self.next_order && id == format!("qf-order-{sequence:020}")
    }
    pub(super) fn evicted_terminal_cancel(&self, id: &str) -> Option<OrderResult> {
        if self.entries.contains_key(id) || !self.issued(id) {
            return None;
        }
        // Accepted IDs are contiguous and never reused; every active order is
        // indexed. A missing issued ID is terminal. No per-order tombstone or
        // account mutation is needed to make cancellation idempotent forever.
        Some(OrderResult {
            accepted: true,
            order_id: None,
            reason_code: None,
            message: "订单已终态，状态详情从结果分页读取".into(),
            unchanged: true,
            requested_quantity: None,
            effective_quantity: None,
        })
    }
    pub fn open_orders(&self) -> Vec<Order> {
        let mut entries: Vec<_> = self
            .active
            .values()
            .flat_map(|ids| ids.values())
            .map(|id| &self.entries[id])
            .collect();
        entries.sort_by_key(|entry| entry.sequence);
        entries
            .into_iter()
            .map(|entry| entry.order.clone())
            .collect()
    }
    pub(super) fn count(&self) -> usize {
        self.active_count
    }
    pub(super) fn install(&mut self, entry: Entry) {
        let id = entry.order.order_id.clone();
        let was_active = self
            .entries
            .get(&id)
            .is_some_and(|entry| entry.order.status.is_active());
        if entry.order.status.is_active() && !was_active {
            self.active_count += 1;
        } else if !entry.order.status.is_active() && was_active {
            self.active_count -= 1;
        }
        let index = self
            .active
            .get_mut(&entry.order.security)
            .expect("authorized order");
        if entry.order.status.is_active() {
            index.insert(entry.sequence, id.clone());
        } else {
            index.remove(&entry.sequence);
            self.terminal.push_back(id.clone());
        }
        self.entries.insert(id, entry);
        while self.terminal.len() > self.limits.terminal_cache {
            if let Some(id) = self.terminal.pop_front() {
                self.entries.remove(&id);
            }
        }
    }
    pub(super) fn queue_capacity(&self, extra: usize) -> QfResult<()> {
        if self.changes.len().saturating_add(extra) > self.limits.max_changes {
            return Err(failure(
                ErrorCode::ResourceLimit,
                "未消费的订单状态通知超过预算",
            ));
        }
        Ok(())
    }
    pub fn take_order_changes(&mut self) -> Vec<Order> {
        std::mem::take(&mut self.changes)
    }
    pub fn take_corporate_effects(&mut self) -> Vec<crate::results::CorporateActionEvent> {
        std::mem::take(&mut self.effects)
    }
    pub(super) fn activation_for(&self, now: Nanoseconds) -> QfResult<OrderActivation> {
        let row = &self.sessions[self.current];
        let session = if self.closed || now > row.session.close_ns {
            self.sessions
                .get(self.current + 1)
                .ok_or_else(|| failure(ErrorCode::RuleUnavailable, "下一可生效交易会话未知"))?
        } else {
            row
        };
        Ok(OrderActivation {
            submitted_ns: now,
            effective_session: session.session.key.clone(),
            eligible_interval_start: now,
            eligible_after_event: self.last_event.clone(),
        })
    }
    fn check_activation(&self, intent: &OrderIntent, activation: &OrderActivation) -> QfResult<()> {
        let row = &self.sessions[self.current];
        if intent.submitted_at.phase != EventPhase::Callback
            || intent.submitted_at.security != intent.security
            || intent.submitted_at.identity.source_session != row.session.key
            || intent.submitted_at.identity.sequence
                != Some(intent.submitted_at.identity.stable_input_sequence)
            || intent.submitted_at.identity.stable_input_sequence.get() == 0
            || intent.submitted_at.time_ns != activation.submitted_ns
            || activation.submitted_ns < row.before_open_ns
            || activation.submitted_ns > row.after_close_ns
            || self
                .last_command
                .as_ref()
                .is_some_and(|last| !command_after(&intent.submitted_at, last))
            || activation.eligible_interval_start != activation.submitted_ns
            || activation.eligible_after_event != self.last_event
            || activation.effective_session
                != self
                    .activation_for(activation.submitted_ns)?
                    .effective_session
        {
            return Err(failure(
                ErrorCode::InvalidContract,
                "委托生效边界或完整命令事件键不符合当前时钟",
            ));
        }
        Ok(())
    }
    pub fn submit(
        &mut self,
        intent: &OrderIntent,
        activation: &OrderActivation,
    ) -> QfResult<OrderResult> {
        if let Err(error) = intent.validate() {
            return Ok(rejected(error, None, None));
        }
        self.check_activation(intent, activation)?;
        if !self.active.contains_key(&intent.security) {
            return Ok(rejected(
                failure(ErrorCode::InvalidOrder, "标的不在运行授权集合内"),
                None,
                None,
            ));
        }
        let mut requested = None;
        let mut reachable = None;
        let outcome = self.prepare_submit(intent, activation, &mut requested, &mut reachable);
        // Semantic refusals do not end the engine; resource/contract faults do.
        match outcome {
            Ok(result) => {
                self.last_command = Some(intent.submitted_at.clone());
                Ok(result)
            }
            Err(error)
                if matches!(
                    error.code,
                    ErrorCode::InvalidOrder
                        | ErrorCode::InsufficientCash
                        | ErrorCode::InsufficientSellable
                        | ErrorCode::RuleUnavailable
                        | ErrorCode::NumericRangeUnsupported
                ) =>
            {
                self.last_command = Some(intent.submitted_at.clone());
                Ok(rejected(error, requested, reachable))
            }
            Err(error) => Err(error),
        }
    }
    fn prepare_submit(
        &mut self,
        intent: &OrderIntent,
        activation: &OrderActivation,
        requested_out: &mut Option<Quantity>,
        reachable: &mut Option<Quantity>,
    ) -> QfResult<OrderResult> {
        let target = matches!(
            intent.value,
            IntentValue::TargetQuantity(_)
                | IntentValue::TargetValue(_)
                | IntentValue::TargetPercent(_)
        );
        let monetary = matches!(
            intent.value,
            IntentValue::Value(_) | IntentValue::TargetValue(_) | IntentValue::TargetPercent(_)
        );
        let literal_zero = match intent.value {
            IntentValue::Quantity(q) | IntentValue::TargetQuantity(q) => q == Quantity::ZERO,
            IntentValue::Value(v) | IntentValue::TargetValue(v) | IntentValue::TargetPercent(v) => {
                v.is_zero()
            }
        };
        if literal_zero && !target {
            return Ok(result(
                Quantity::ZERO,
                Quantity::ZERO,
                None,
                true,
                "零数量，无动作".into(),
            ));
        }
        let limit = match intent.style {
            OrderStyle::Limit { price } => Some(price),
            OrderStyle::Market => None,
        };
        // A zero target needs no price to compute its target quantity. Any
        // resulting sale still needs a valid estimate for fees/reservation.
        let estimate = if literal_zero {
            None
        } else if monetary {
            Some(
                self.account
                    .order_estimate(&intent.security, activation.submitted_ns)?
                    .price,
            )
        } else {
            Some(limit.map_or_else(
                || {
                    self.account
                        .order_estimate(&intent.security, activation.submitted_ns)
                        .map(|mark| mark.price)
                },
                Ok,
            )?)
        };
        let raw_requested = match intent.value {
            IntentValue::Quantity(q) | IntentValue::TargetQuantity(q) => q,
            IntentValue::Value(v) | IntentValue::TargetValue(v) if v.is_zero() => Quantity::ZERO,
            IntentValue::Value(v) | IntentValue::TargetValue(v) => ExactDecimal::legal_quantity(
                Quantity::new(1)?,
                v,
                estimate.expect("nonzero estimate").get(),
                QuantityStep::new(1)?,
            )?,
            IntentValue::TargetPercent(v) if v.is_zero() => Quantity::ZERO,
            IntentValue::TargetPercent(v) => {
                let equity = self
                    .account
                    .value(activation.submitted_ns)?
                    .total_value
                    .ok_or_else(|| {
                        failure(
                            ErrorCode::RuleUnavailable,
                            "组合权益不可计算，无法确定目标比例",
                        )
                    })?;
                // floor(equity * percent / price), with a single integer rational.
                // mul_rounded at scale 28 would introduce an avoidable boundary.
                ExactDecimal::legal_value_quantity(
                    equity,
                    v,
                    estimate.expect("nonzero estimate").get(),
                    QuantityStep::new(1)?,
                )?
            }
        };
        *requested_out = Some(raw_requested);
        let ids = &self.active[&intent.security];
        let existing: Vec<_> = ids.values().map(|id| &self.entries[id]).collect();
        let old: Vec<_> = if target {
            existing
                .iter()
                .filter(|entry| entry.target)
                .map(|e| (*e).clone())
                .collect()
        } else {
            vec![]
        };
        let mut base = i128::from(self.account.held_quantity(&intent.security).get());
        if target {
            for entry in existing.iter().filter(|entry| !entry.target) {
                let signed = i128::from(entry.order.remaining()?.get());
                base += if entry.order.side == Side::Buy {
                    signed
                } else {
                    -signed
                };
            }
        }
        let delta = if target {
            i128::from(raw_requested.get()) - base
        } else {
            i128::from(raw_requested.get()) * if intent.side == Side::Buy { 1 } else { -1 }
        };
        let side = if delta < 0 { Side::Sell } else { Side::Buy };
        let raw_delta =
            Quantity::new(i64::try_from(delta.abs()).map_err(|_| {
                failure(ErrorCode::NumericRangeUnsupported, "目标差量超出整数范围")
            })?)?;
        let revision = next(self.revision)?;
        let order_sequence = self.next_order;
        let next_order = next(order_sequence)?;
        let id = format!("qf-order-{order_sequence:020}");
        let mut admission = if raw_delta == Quantity::ZERO {
            None
        } else {
            Some(self.facts.admission(
                &intent.security,
                side,
                &self.sessions[self.current],
                activation.submitted_ns,
            )?)
        };
        if let Some(admission) = &mut admission {
            if admission.instrument.security != intent.security {
                return Err(failure(ErrorCode::InvalidContract, "申报身份与标的不一致"));
            }
            admission.instrument.validate()?;
            let mut bought_or_open = self
                .session_buys
                .get(&intent.security)
                .copied()
                .unwrap_or(Quantity::ZERO);
            for entry in existing
                .iter()
                .filter(|entry| entry.order.side == Side::Buy && !(target && entry.target))
            {
                bought_or_open = bought_or_open.checked_add(entry.order.remaining()?)?;
            }
            admission.permissions.risk_bought_or_open = Some(bought_or_open);
        }
        let (plan, account_candidate) = self.account.preview(|account| {
            for entry in &old { account.release(&entry.order.order_id)?; }
            let available = account.sellable_quantity(&intent.security)?;
            let quantity = if raw_delta == Quantity::ZERO { raw_delta } else {
                let terms = account.terms(&intent.security)?;
                let resolved = terms.resolved()?;
                if monetary {
                    if side == Side::Buy { resolved.quantize_buy(raw_delta) } else { quantize_sell(raw_delta, available, &resolved)? }
                } else { raw_delta }
            };
            let effective = if target { quantity_from_projected(base, side, quantity)? } else { quantity };
            // A partially filled legal declaration keeps its existing remainder:
            // the remainder is not a new order subject to declaration minimums.
            // Preserve it only while it stays within the raw goal and a newly
            // quantized declaration would not cover more of the requested delta.
            if target && old.len() == 1 && old[0].order.side == side
                && old[0].order.limit_price == limit && old[0].order.tif == intent.tif {
                let remaining = old[0].order.remaining()?;
                if remaining == quantity || monetary && remaining <= raw_delta && quantity <= remaining {
                    return Ok((remaining, quantity_from_projected(base, side, remaining)?, true));
                }
            }
            if quantity != Quantity::ZERO {
                let terms = account.terms(&intent.security)?;
                let resolved = terms.resolved()?;
                resolved.validate_quantity(side, quantity, available, limit.is_none())?;
                let admission = admission.as_ref().expect("nonzero delta facts");
                resolved.validate_permissions(side, quantity, limit.is_none(), &admission.permissions)?;
                let row = &self.sessions[self.current];
                // Orders outside an auction phase are staged for the next
                // eligible event. Current intraday submissions obey D02 phases.
                let phase = if !self.closed && activation.submitted_ns <= row.session.close_ns && row.market_windows.iter().any(|w| w.contains(activation.submitted_ns)) {
                    row.scheduled_phase_at(resolved.rule().session_template, activation.submitted_ns)?
                } else { AuctionPhase::Continuous };
                if let Some(price) = limit { resolved.validate_limit_submission(side, price, phase, admission.submission_reference)?; }
                else { resolved.validate_market_phase(phase)?; }
                let price = limit.or(estimate).or_else(|| account.order_estimate(&intent.security, activation.submitted_ns).ok().map(|mark| mark.price)).ok_or_else(|| failure(ErrorCode::RuleUnavailable, "清仓订单仍需有效原价用于费用预留"))?;
                let buy_step = resolved.rule().buy_step;
                let minimum_buy = resolved.rule().minimum_buy;
                let reserve = account.reserve_order(crate::accounting::ReservationRequest {
                    order_id: id.clone(), security: intent.security.clone(), side, quantity, estimate_price: price, limit_price: limit,
                    tif: intent.tif, effective_session: activation.effective_session.clone(), submitted_ns: activation.submitted_ns,
                });
                if let Err(error) = reserve {
                    if side == Side::Buy && error.code == ErrorCode::InsufficientCash {
                        *reachable = Some(if target { quantity_from_projected(base, side, Quantity::ZERO)? } else { Quantity::ZERO });
                    }
                    return Err(error);
                }
                if side == Side::Buy {
                    let affordable = account.max_affordable(&id, quantity, price, buy_step)?;
                    let affordable = if affordable < minimum_buy { Quantity::ZERO } else { affordable };
                    if affordable < quantity {
                        *reachable = Some(if target { quantity_from_projected(base, side, affordable)? } else { affordable });
                        return Err(failure(ErrorCode::InsufficientCash, "估算原价与费用下无法达到请求数量；effective_quantity 为可达数量，原目标单保留"));
                    }
                }
            }
            Ok((quantity, effective, false))
        })?;
        let (quantity, effective, unchanged) = plan;
        if unchanged || quantity == Quantity::ZERO && old.is_empty() {
            return Ok(result(
                raw_requested,
                effective,
                None,
                true,
                "目标已由持仓和活动委托覆盖，无新增订单".into(),
            ));
        }
        let changed_count = old.len() + usize::from(quantity != Quantity::ZERO);
        self.queue_capacity(changed_count)?;
        if self.count().saturating_sub(old.len()) + usize::from(quantity != Quantity::ZERO)
            > self.limits.max_active
        {
            return Err(failure(ErrorCode::ResourceLimit, "活动订单超过预算"));
        }
        let mut changes = Vec::with_capacity(changed_count);
        for mut entry in old {
            entry.order.status = OrderStatus::Cancelled;
            entry.order.updated_at = Some(Box::new(intent.submitted_at.clone()));
            entry.order.reason_code = Some(ErrorCode::Cancelled);
            entry.order.message = "由新目标原子替换未成交余量".into();
            changes.push(entry);
        }
        if quantity != Quantity::ZERO {
            changes.push(Entry {
                sequence: order_sequence,
                target,
                order: Order {
                    order_id: id.clone(),
                    security: intent.security.clone(),
                    side,
                    quantity,
                    filled_quantity: Quantity::ZERO,
                    status: OrderStatus::Open,
                    submitted_ns: activation.submitted_ns,
                    updated_at: Some(Box::new(intent.submitted_at.clone())),
                    limit_price: limit,
                    tif: intent.tif,
                    effective_session: activation.effective_session.clone(),
                    eligible_interval_start: Some(activation.eligible_interval_start),
                    eligible_after_event: activation.eligible_after_event.clone(),
                    reason_code: None,
                    message: "已受理；在后续符合条件的市场事件撮合".into(),
                },
            });
        }
        // Every fallible account/order/budget computation precedes commit.
        self.account.commit(account_candidate)?;
        self.revision = revision;
        if quantity != Quantity::ZERO {
            self.next_order = next_order;
        }
        for entry in changes {
            self.changes.push(entry.order.clone());
            self.install(entry);
        }
        Ok(result(
            raw_requested,
            effective,
            (quantity != Quantity::ZERO).then_some(id),
            false,
            if effective != raw_requested {
                "已按交易单位向下量化；effective_quantity 为有效目标/数量".into()
            } else {
                "已受理".into()
            },
        ))
    }
    pub fn cancel_at(&mut self, id: &str, at: &EventKey) -> QfResult<OrderResult> {
        if let Some(result) = self.evicted_terminal_cancel(id) {
            return Ok(result);
        }
        let current = self.get_order(id)?;
        if !current.status.is_active() {
            return Ok(result(
                current.quantity,
                current.quantity,
                None,
                true,
                format!("订单已终态：{:?}", current.status),
            ));
        }
        if at.phase != EventPhase::Callback
            || at.security != current.security
            || at.time_ns < current.submitted_ns
            || self
                .last_event
                .as_ref()
                .is_some_and(|event| at.time_ns < event.time_ns)
            || at.identity.source_session != self.sessions[self.current].session.key
            || at.time_ns < self.sessions[self.current].before_open_ns
            || at.time_ns > self.sessions[self.current].after_close_ns
            || at.identity.sequence != Some(at.identity.stable_input_sequence)
            || at.identity.stable_input_sequence.get() == 0
            || self
                .last_command
                .as_ref()
                .is_some_and(|last| !command_after(at, last))
        {
            return Err(failure(
                ErrorCode::InvalidContract,
                "撤单命令事件键早于当前状态",
            ));
        }
        self.queue_capacity(1)?;
        let revision = next(self.revision)?;
        let mut entry = self.entries[id].clone();
        entry.order.status = OrderStatus::Cancelled;
        entry.order.updated_at = Some(Box::new(at.clone()));
        entry.order.reason_code = Some(ErrorCode::Cancelled);
        entry.order.message = "仅取消未成交余量，已成交部分保持".into();
        let (_, prepared) = self.account.preview(|account| account.release(id))?;
        self.account.commit(prepared)?;
        self.revision = revision;
        self.last_command = Some(at.clone());
        self.changes.push(entry.order.clone());
        self.install(entry);
        Ok(result(
            current.quantity,
            current.filled_quantity,
            Some(id.into()),
            false,
            "撤单完成".into(),
        ))
    }
}
pub(super) fn quantity_from_projected(
    base: i128,
    side: Side,
    quantity: Quantity,
) -> QfResult<Quantity> {
    let value = base + i128::from(quantity.get()) * if side == Side::Buy { 1 } else { -1 };
    Quantity::new(
        i64::try_from(value)
            .map_err(|_| failure(ErrorCode::NumericRangeUnsupported, "预计净持仓超出整数范围"))?,
    )
}
fn quantize_sell(
    requested: Quantity,
    available: Quantity,
    rule: &crate::rules::market::ResolvedTradingRule<'_>,
) -> QfResult<Quantity> {
    if requested > available {
        return Err(failure(
            ErrorCode::InsufficientSellable,
            "目标差量超过当前可卖量；未成交买单不是可卖持仓",
        ));
    }
    if requested == available {
        return Ok(requested);
    }
    let step = rule.rule().sell_step.get();
    let odd = available.get() % step;
    let floor = requested.get() / step * step;
    let value = if odd != 0 && requested.get() - floor >= odd {
        floor + odd
    } else {
        floor
    };
    let quantity = Quantity::new(value)?;
    if quantity == Quantity::ZERO {
        return Ok(quantity);
    }
    if quantity < rule.rule().minimum_sell && quantity.get() != odd {
        return Ok(Quantity::ZERO);
    }
    rule.validate_quantity(Side::Sell, quantity, available, false)?;
    Ok(quantity)
}
impl<F: OrderFacts> FillBudget for OrderManager<F> {
    fn allowance(
        &self,
        id: &str,
        requested: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        let entry = self
            .entries
            .get(id)
            .filter(|e| e.order.status.is_active())
            .ok_or_else(|| failure(ErrorCode::InvalidOrder, "活动订单不存在"))?;
        if requested > entry.order.remaining()? {
            return Err(failure(ErrorCode::InvalidOrder, "候选成交超过余量"));
        }
        let effective = if entry.order.side == Side::Buy {
            self.account.max_affordable(id, requested, price, step)?
        } else {
            requested
        };
        Ok(FillAllowance {
            requested_quantity: requested,
            effective_quantity: effective,
            reason_code: (effective < requested).then_some(ErrorCode::InsufficientCash),
            message: if effective < requested {
                "候选价格与费用下仅可支付 effective_quantity，余量保持活动".into()
            } else {
                String::new()
            },
        })
    }
    fn trade_id(&self, offset: usize) -> QfResult<String> {
        let offset = u64::try_from(offset)
            .map_err(|_| failure(ErrorCode::NumericRangeUnsupported, "成交序号溢出"))?;
        Ok(trade_id(self.next_trade.checked_add(offset).ok_or_else(
            || failure(ErrorCode::NumericRangeUnsupported, "成交序号溢出"),
        )?))
    }
    fn allowance_after(
        &self,
        prior: &[crate::matching::Fill],
        id: &str,
        requested: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        if prior.is_empty() {
            return self.allowance(id, requested, price, step);
        }
        if prior.len() > self.limits.max_changes {
            return Err(failure(ErrorCode::ResourceLimit, "累计候选成交超过预算"));
        }
        let entry = self
            .entries
            .get(id)
            .filter(|entry| entry.order.status.is_active())
            .ok_or_else(|| failure(ErrorCode::InvalidOrder, "候选活动订单不存在"))?;
        let (allowance, _) = self.account.preview(|account| {
            for (offset, fill) in prior.iter().enumerate() {
                if fill.trade_id != self.trade_id(offset)? {
                    return Err(failure(
                        ErrorCode::StaleClaim,
                        "累计候选成交 ID 不符合本运行序列",
                    ));
                }
                let assessed = account.assess_fill(fill)?;
                account.apply_fill(&assessed)?;
            }
            let reservation = account
                .reservation(id)
                .ok_or_else(|| failure(ErrorCode::InvalidOrder, "候选订单余量已用完"))?;
            if requested > reservation.remaining {
                return Err(failure(
                    ErrorCode::InvalidOrder,
                    "候选成交超过累计预览后的余量",
                ));
            }
            let effective = if entry.order.side == Side::Buy {
                account.max_affordable(id, requested, price, step)?
            } else {
                requested
            };
            Ok(FillAllowance {
                requested_quantity: requested,
                effective_quantity: effective,
                reason_code: (effective < requested).then_some(ErrorCode::InsufficientCash),
                message: if effective < requested {
                    "累计此前候选成交及费用后仅可支付 effective_quantity".into()
                } else {
                    String::new()
                },
            })
        })?;
        Ok(allowance)
    }
}
