use super::manager::*;
use super::*;
use crate::accounting::{AccountPort, AccountView, ActionBoundary, ActionPhase};
use crate::clock::CalendarSession;
use crate::engine::{ExecutionPort, OrderActivation};
use crate::matching::{Fill, FillAllowance, FillBudget, MatchOutcome};
use crate::types::{MarketEvent, Price, QuantityStep};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

impl<F: OrderFacts> OrderManager<F> {
    pub fn claim_match(&self, event: &MarketEvent, orders: &[Order]) -> QfResult<MatchClaim> {
        event.validate()?;
        let key = event.key();
        if self.last_event.as_ref().is_some_and(|last| key <= *last)
            || orders.len() > self.limits.max_active
        {
            return Err(failure(
                ErrorCode::StaleClaim,
                "市场事件已完成或候选订单过量",
            ));
        }
        let session = match event {
            MarketEvent::Bar(e) => &e.session,
            MarketEvent::TradeTick(e) => &e.session,
            MarketEvent::QuoteTick(e) => &e.session,
        };
        if session != &self.sessions[self.current].session.key || self.closed {
            return Err(failure(
                ErrorCode::InvalidContract,
                "撮合不是当前未关闭的交易会话",
            ));
        }
        let mut ids = BTreeSet::new();
        let mut priority = 0;
        for order in orders {
            let entry = self
                .entries
                .get(&order.order_id)
                .ok_or_else(|| failure(ErrorCode::StaleClaim, "未知订单不能取得成交 claim"))?;
            let effective = self
                .session_indices
                .get(&order.effective_session)
                .is_some_and(|index| *index <= self.current);
            let eligible = effective
                && (order.tif == TimeInForce::Gtc || order.effective_session == *session)
                && order.submitted_ns <= key.time_ns
                && match event {
                    MarketEvent::Bar(bar) => {
                        order
                            .eligible_interval_start
                            .is_some_and(|start| start <= bar.interval_start_ns)
                            && order.submitted_ns <= bar.interval_start_ns
                    }
                    _ => order
                        .eligible_after_event
                        .as_ref()
                        .is_none_or(|last| key > *last),
                };
            if !ids.insert(&order.order_id)
                || !order.status.is_active()
                || entry.order != *order
                || order.security != key.security
                || !eligible
                || entry.sequence <= priority
            {
                return Err(failure(
                    ErrorCode::StaleClaim,
                    "撮合订单快照过期、未生效或委托优先序错误",
                ));
            }
            priority = entry.sequence;
        }
        Ok(MatchClaim {
            owner: Arc::clone(&self.owner),
            revision: self.revision,
            event: event.clone(),
            orders: orders.to_vec(),
        })
    }
    pub fn commit_match(
        &mut self,
        claim: MatchClaim,
        mut outcome: MatchOutcome,
    ) -> QfResult<MatchOutcome> {
        if !Arc::ptr_eq(&self.owner, &claim.owner)
            || claim.revision != self.revision
            || self
                .last_event
                .as_ref()
                .is_some_and(|last| claim.event.key() <= *last)
        {
            return Err(failure(
                ErrorCode::StaleClaim,
                "未知、旧或其他运行的成交 claim 被拒绝",
            ));
        }
        if outcome.fills.len().saturating_add(outcome.orders.len()) > self.limits.max_changes {
            return Err(failure(ErrorCode::ResourceLimit, "撮合输出超过边界预算"));
        }
        let key = claim.event.key();
        let by_id: BTreeMap<_, _> = claim
            .orders
            .iter()
            .map(|o| (o.order_id.as_str(), o))
            .collect();
        let mut quantities: BTreeMap<&str, Quantity> = BTreeMap::new();
        let mut next_trade = self.next_trade;
        let mut priority = 0;
        for fill in &outcome.fills {
            let order = by_id
                .get(fill.order_id.as_str())
                .ok_or_else(|| failure(ErrorCode::StaleClaim, "成交引用未知或未生效订单"))?;
            let sequence = self.entries[&fill.order_id].sequence;
            let quantity = quantities
                .get(fill.order_id.as_str())
                .copied()
                .unwrap_or(Quantity::ZERO)
                .checked_add(fill.quantity)?;
            if fill.trade_id != trade_id(next_trade)
                || sequence < priority
                || fill.security != order.security
                || fill.side != order.side
                || fill.execution_time_ns != key.time_ns
                || fill.quantity == Quantity::ZERO
                || quantity > order.remaining()?
            {
                return Err(failure(
                    ErrorCode::InvalidContract,
                    "成交身份、份额、时间或稳定委托序不符合 claim",
                ));
            }
            self.account
                .terms(&fill.security)?
                .resolved()?
                .validate_execution(fill.price)?;
            quantities.insert(&fill.order_id, quantity);
            priority = sequence;
            next_trade = next(next_trade)?;
        }
        let mut changed = BTreeSet::new();
        let mut entries = Vec::new();
        for order in &mut outcome.orders {
            let previous = by_id
                .get(order.order_id.as_str())
                .ok_or_else(|| failure(ErrorCode::StaleClaim, "状态更新引用未生效订单"))?;
            let filled = quantities
                .get(order.order_id.as_str())
                .copied()
                .unwrap_or(Quantity::ZERO);
            let expected_filled = previous.filled_quantity.checked_add(filled)?;
            let status_valid = match order.status {
                OrderStatus::Filled => expected_filled == previous.quantity,
                OrderStatus::PartiallyFilled => {
                    expected_filled != Quantity::ZERO
                        && expected_filled < previous.quantity
                        && filled != Quantity::ZERO
                }
                OrderStatus::Open => {
                    previous.status == OrderStatus::Accepted && expected_filled == Quantity::ZERO
                }
                OrderStatus::Cancelled | OrderStatus::Expired | OrderStatus::Rejected => {
                    expected_filled < previous.quantity
                        && order.reason_code.is_some()
                        && !order.message.is_empty()
                }
                OrderStatus::Accepted => false,
            };
            let mut expected = (*previous).clone();
            expected.filled_quantity = expected_filled;
            expected.status = order.status;
            expected.reason_code = order.reason_code;
            expected.message = order.message.clone();
            expected.updated_at = order.updated_at.clone();
            if !changed.insert(order.order_id.clone()) || expected != *order || !status_valid {
                return Err(failure(
                    ErrorCode::InvalidContract,
                    "订单状态过渡或不可变字段不合法",
                ));
            }
            order.updated_at = Some(Box::new(key.clone()));
            if order.status == OrderStatus::Filled {
                order.reason_code = None;
            }
            let mut entry = self.entries[&order.order_id].clone();
            entry.order = order.clone();
            entries.push(entry);
        }
        if quantities.keys().any(|id| !changed.contains(*id)) {
            return Err(failure(
                ErrorCode::InvalidContract,
                "成交缺少唯一订单状态更新",
            ));
        }
        entries.sort_by_key(|entry| entry.sequence);
        let revision = next(self.revision)?;
        if outcome.fills.is_empty() && entries.is_empty() {
            // The normal unfilled Tick has no account snapshot, status record
            // or durable success entry. Keep only the current replay cursor.
            self.revision = revision;
            self.last_event = Some(key);
            return Ok(outcome);
        }
        let mut session_buys = self.session_buys.clone();
        for fill in &outcome.fills {
            if fill.side == Side::Buy {
                let bought = session_buys
                    .get(&fill.security)
                    .copied()
                    .unwrap_or(Quantity::ZERO)
                    .checked_add(fill.quantity)?;
                session_buys.insert(fill.security.clone(), bought);
            }
        }
        let ((fills, shortfalls), prepared) = self.account.preview(|account| {
            let mut assessed = Vec::with_capacity(outcome.fills.len());
            for fill in &outcome.fills {
                let quoted = account.assess_fill(fill)?;
                let mut expected = fill.clone();
                expected.fee = quoted.fee;
                if expected != quoted || quoted.fee.is_negative() {
                    return Err(failure(
                        ErrorCode::InvalidContract,
                        "账户核费改变了成交身份",
                    ));
                }
                account.apply_fill(&quoted)?;
                assessed.push(quoted);
            }
            for entry in &entries {
                if !entry.order.status.is_active() {
                    account.release(&entry.order.order_id)?;
                }
            }
            let mut shortfalls = BTreeMap::new();
            for entry in &entries {
                if entry.order.side == Side::Buy
                    && entry.order.status == OrderStatus::PartiallyFilled
                {
                    let price = assessed
                        .iter()
                        .rev()
                        .find(|fill| fill.order_id == entry.order.order_id)
                        .expect("partial fill has trade")
                        .price;
                    let remaining = entry.order.remaining()?;
                    let affordable = account.max_affordable(
                        &entry.order.order_id,
                        remaining,
                        price,
                        QuantityStep::new(1)?,
                    )?;
                    if affordable < remaining {
                        shortfalls.insert(entry.order.order_id.clone(), affordable);
                    }
                }
            }
            Ok((assessed, shortfalls))
        })?;
        for entry in &mut entries {
            if let Some(affordable) = shortfalls.get(&entry.order.order_id) {
                entry.order.reason_code = Some(ErrorCode::InsufficientCash);
                entry.order.message = format!(
                    "本次价格与费用下剩余 {} 份仅可支付 {} 份；余单保持活动",
                    entry.order.remaining()?.get(),
                    affordable.get()
                );
            }
        }
        outcome.orders = entries.iter().map(|entry| entry.order.clone()).collect();
        self.account.commit(prepared)?;
        for entry in entries {
            self.install(entry);
        }
        self.next_trade = next_trade;
        self.session_buys = session_buys;
        self.revision = revision;
        self.last_event = Some(key);
        outcome.fills = fills;
        Ok(outcome)
    }
    fn boundary(&mut self, row: &CalendarSession, phase: ActionPhase) -> QfResult<Vec<Order>> {
        if row != &self.sessions[self.current] {
            return Err(failure(
                ErrorCode::InvalidContract,
                "订单边界与唯一账户日历不一致",
            ));
        }
        let terms = if phase == ActionPhase::BeforeOpen {
            self.facts.session_terms(row)?
        } else {
            vec![]
        };
        if terms.len() > self.active.len() {
            return Err(failure(
                ErrorCode::ResourceLimit,
                "会话账户事实超过授权预算",
            ));
        }
        let mut securities = BTreeSet::new();
        for terms in &terms {
            if !self.active.contains_key(&terms.instrument.security)
                || !securities.insert(&terms.instrument.security)
            {
                return Err(failure(
                    ErrorCode::InvalidContract,
                    "会话事实重复或越过授权集合",
                ));
            }
        }
        let at = if phase == ActionPhase::BeforeOpen {
            row.before_open_ns
        } else {
            row.session.close_ns
        };
        let revision = next(self.revision)?;
        let ((report, rechecks), prepared) = self.account.preview(|account| {
            for terms in terms {
                account.install_terms(terms)?;
            }
            let report = account.advance_corporate_actions(&ActionBoundary {
                session: row.session.key.clone(),
                phase,
            })?;
            let mut rechecks = Vec::new();
            if phase == ActionPhase::BeforeOpen {
                for order in self.open_orders() {
                    if report.cancelled_order_ids.contains(&order.order_id) {
                        continue;
                    }
                    if let Err(error) = account.recheck_order(&order.order_id) {
                        if !matches!(
                            error.code,
                            ErrorCode::InvalidOrder
                                | ErrorCode::RuleUnavailable
                                | ErrorCode::InsufficientCash
                                | ErrorCode::InsufficientSellable
                        ) {
                            return Err(error);
                        }
                        account.release(&order.order_id)?;
                        rechecks.push((order.order_id, error));
                    }
                }
            }
            Ok((report, rechecks))
        })?;
        if report.effects.len().saturating_add(self.effects.len()) > self.limits.max_changes {
            return Err(failure(
                ErrorCode::ResultBudgetExceeded,
                "公司行动 effects 尚未消费或超过预算",
            ));
        }
        let mut cancellations = Vec::new();
        for id in &report.cancelled_order_ids {
            cancellations.push((
                id.clone(),
                failure(
                    ErrorCode::Cancelled,
                    "公司行动保守取消余单，不修改价格或保留虚构优先级",
                ),
            ));
        }
        cancellations.extend(rechecks);
        if cancellations.len() > self.limits.max_changes {
            return Err(failure(ErrorCode::ResourceLimit, "会话取消通知超过预算"));
        }
        let mut entries = Vec::new();
        let mut ids = BTreeSet::new();
        for (id, reason) in cancellations {
            let mut entry = self
                .entries
                .get(&id)
                .filter(|e| e.order.status.is_active())
                .cloned()
                .ok_or_else(|| failure(ErrorCode::InvalidContract, "账户公司行动取消了未知订单"))?;
            if !ids.insert(id) {
                return Err(failure(ErrorCode::InvalidContract, "会话取消订单重复"));
            }
            entry.order.status = OrderStatus::Cancelled;
            entry.order.updated_at = Some(Box::new(boundary_key(
                row,
                at,
                entry.order.security.clone(),
            )?));
            entry.order.reason_code = Some(reason.code);
            entry.order.message = reason.message;
            entries.push(entry);
        }
        entries.sort_by_key(|entry| entry.sequence);
        self.account.commit(prepared)?;
        self.revision = revision;
        self.closed = phase == ActionPhase::SessionClose;
        let orders = entries.iter().map(|entry| entry.order.clone()).collect();
        for entry in entries {
            self.install(entry);
        }
        self.effects
            .extend(report.effects.into_iter().map(|effect| {
                crate::results::CorporateActionEvent {
                    time_ns: at,
                    session: row.session.key.clone(),
                    effect,
                }
            }));
        Ok(orders)
    }
}
impl<F: OrderFacts> AccountPort for OrderManager<F> {
    fn validate_and_reserve(&mut self, intent: &OrderIntent) -> QfResult<OrderResult> {
        let activation = self.activation_for(intent.submitted_at.time_ns)?;
        self.submit(intent, &activation)
    }
    fn assess_fill(&self, fill: &Fill) -> QfResult<Fill> {
        self.account.assess_fill(fill)
    }
    fn apply_fill(&mut self, _fill: &Fill) -> QfResult<()> {
        Err(failure(
            ErrorCode::InvalidContract,
            "成交必须通过单次 claim 原子提交订单与唯一账户",
        ))
    }
    fn release(&mut self, _id: &str) -> QfResult<()> {
        Err(failure(
            ErrorCode::InvalidContract,
            "订单预留释放必须伴随真实撤单状态",
        ))
    }
    fn settle(&mut self, session: &SessionKey) -> QfResult<()> {
        let index = self
            .session_indices
            .get(session)
            .copied()
            .ok_or_else(|| failure(ErrorCode::RuleUnavailable, "结算会话未知"))?;
        let revision = next(self.revision)?;
        self.account.settle(session)?;
        if index != self.current {
            self.closed = false;
            self.session_buys.clear();
        }
        self.current = index;
        self.revision = revision;
        Ok(())
    }
    fn check_session_end(&self, session: &SessionKey) -> QfResult<()> {
        self.account.check_session_end(session)
    }
    fn value(&self, now: Nanoseconds) -> QfResult<AccountView> {
        self.account.value(now)
    }
}
impl<F: OrderFacts> ExecutionPort for OrderManager<F> {
    fn submit_order(
        &mut self,
        intent: &OrderIntent,
        activation: &OrderActivation,
    ) -> QfResult<OrderResult> {
        self.submit(intent, activation)
    }
    fn cancel_order(&mut self, id: &str) -> QfResult<OrderResult> {
        if let Some(result) = self.evicted_terminal_cancel(id) {
            return Ok(result);
        }
        let order = self.get_order(id)?;
        if !order.status.is_active() {
            return Ok(result(
                order.quantity,
                order.quantity,
                None,
                true,
                format!("订单已终态：{:?}", order.status),
            ));
        }
        Err(failure(
            ErrorCode::InvalidContract,
            "活动订单撤销须提供完整命令事件键",
        ))
    }
    fn cancel_order_at(&mut self, id: &str, at: &EventKey) -> QfResult<OrderResult> {
        self.cancel_at(id, at)
    }
    fn command_changes(&mut self) -> Option<Vec<Order>> {
        Some(self.take_order_changes())
    }
    fn corporate_effects(&mut self) -> Vec<crate::results::CorporateActionEvent> {
        self.take_corporate_effects()
    }
    fn order(&self, id: &str) -> QfResult<Order> {
        self.get_order(id)
    }
    fn active_orders(&self, security: &SecurityKey, limit: usize) -> QfResult<Vec<Order>> {
        let Some(index) = self.active.get(security) else {
            return Err(failure(ErrorCode::InvalidOrder, "标的不在授权集合内"));
        };
        if index.len() > limit {
            return Err(failure(ErrorCode::ResourceLimit, "活动订单超过调用预算"));
        }
        Ok(index
            .values()
            .map(|id| self.entries[id].order.clone())
            .collect())
    }
    fn active_order_count(&self) -> usize {
        self.count()
    }
    fn transition(&mut self, order: &Order) -> QfResult<()> {
        if self
            .entries
            .get(&order.order_id)
            .is_some_and(|entry| entry.order == *order)
        {
            return Ok(());
        }
        Err(failure(
            ErrorCode::InvalidContract,
            "订单状态必须通过成交 claim 或原子命令更新",
        ))
    }
    fn fill_allowance(
        &self,
        id: &str,
        requested: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        self.allowance(id, requested, price, step)
    }
    fn next_trade_id(&self, offset: usize) -> QfResult<String> {
        FillBudget::trade_id(self, offset)
    }
    fn fill_allowance_after(
        &self,
        prior: &[Fill],
        id: &str,
        requested: Quantity,
        price: Price,
        step: QuantityStep,
    ) -> QfResult<FillAllowance> {
        self.allowance_after(prior, id, requested, price, step)
    }
    fn apply_match_outcome(
        &mut self,
        event: &MarketEvent,
        eligible: &[Order],
        outcome: MatchOutcome,
    ) -> QfResult<MatchOutcome> {
        let claim = self.claim_match(event, eligible)?;
        self.commit_match(claim, outcome)
    }
    fn mark(&mut self, event: &MarketEvent) -> QfResult<()> {
        if self.last_event.as_ref() != Some(&event.key()) {
            return Err(failure(
                ErrorCode::InvalidContract,
                "账户价格更新必须在对应市场 claim 提交后",
            ));
        }
        let revision = next(self.revision)?;
        self.account.mark(event)?;
        self.revision = revision;
        Ok(())
    }
    fn expire_day(&mut self, session: &SessionKey) -> QfResult<Vec<Order>> {
        if session != &self.sessions[self.current].session.key || !self.closed {
            return Err(failure(
                ErrorCode::InvalidContract,
                "DAY 到期必须在会话关闭处理之后",
            ));
        }
        let mut entries = Vec::new();
        let row = &self.sessions[self.current];
        for order in self
            .open_orders()
            .into_iter()
            .filter(|o| o.tif == TimeInForce::Day && &o.effective_session == session)
        {
            let mut entry = self.entries[&order.order_id].clone();
            entry.order.status = OrderStatus::Expired;
            entry.order.updated_at = Some(Box::new(boundary_key(
                row,
                row.session.close_ns,
                entry.order.security.clone(),
            )?));
            entry.order.reason_code = Some(ErrorCode::Cancelled);
            entry.order.message = "DAY 有效会话结束，未成交余量到期".into();
            entries.push(entry);
        }
        let revision = next(self.revision)?;
        let (_, prepared) = self.account.preview(|account| {
            for entry in &entries {
                account.release(&entry.order.order_id)?;
            }
            Ok(())
        })?;
        self.account.commit(prepared)?;
        self.revision = revision;
        let orders = entries.iter().map(|e| e.order.clone()).collect();
        for entry in entries {
            self.install(entry);
        }
        Ok(orders)
    }
    fn session_start(&mut self, row: &CalendarSession) -> QfResult<Vec<Order>> {
        self.boundary(row, ActionPhase::BeforeOpen)
    }
    fn session_end(&mut self, row: &CalendarSession) -> QfResult<Vec<Order>> {
        self.boundary(row, ActionPhase::SessionClose)
    }
}
