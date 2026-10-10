//! Raw valuation inputs only. Research adjustment never mutates the account.
use super::ledger::{amount, error};
use super::{Account, AccountPort, AccountView, PositionView};
use crate::rules::market::{RuleOrigin, RuleUse, TradingStatus};
use crate::types::{
    MarketEvent, Money, Nanoseconds, Price, RoundingPolicy, SecurityKey, SessionKey,
};
use crate::{ErrorCode, QfResult};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PriceBasis {
    Raw,
    ResearchAdjusted,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValuationMark {
    pub price: Price,
    pub price_time_ns: Nanoseconds,
    pub known_ns: Nanoseconds,
    pub session: SessionKey,
    pub basis: PriceBasis,
    pub source: String,
    pub origin: RuleOrigin,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValuationReason {
    CurrentRaw,
    PriorCloseBeforeOpen,
    KnownHaltLastRaw,
    UnknownMissingPrice,
    CorporateActionBoundaryMissing,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PositionValuation {
    pub market_value: Option<Money>,
    pub price_time_ns: Option<Nanoseconds>,
    pub is_stale: bool,
    pub reason: ValuationReason,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValuedAccount {
    /// Existing D01 DTO remains unchanged for D05/D10/SDK consumers.
    pub account: AccountView,
    pub valuation: BTreeMap<SecurityKey, PositionValuation>,
}
impl Account {
    pub fn set_raw_mark(
        &mut self,
        security: SecurityKey,
        mark: ValuationMark,
        now: Nanoseconds,
    ) -> QfResult<()> {
        if mark.basis != PriceBasis::Raw {
            return Err(error(
                ErrorCode::InvalidContract,
                "研究复权价格不能作为账户原价估值",
            ));
        }
        if mark.price_time_ns > mark.known_ns || mark.known_ns > now {
            return Err(error(ErrorCode::LookaheadForbidden, "账户估值价格尚未公开"));
        }
        if !mark.origin.permits(&self.usage) {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "合成价格不得进入正式账户模型",
            ));
        }
        mark.origin.validate()?;
        if now < self.state.as_of {
            return Err(error(ErrorCode::LookaheadForbidden, "原价更新早于账户状态"));
        }
        if let Ok(index) = self.index(&mark.session) {
            let row = &self.sessions[index];
            if index > self.state.current
                || mark.price_time_ns < row.before_open_ns
                || mark.price_time_ns > row.session.close_ns
            {
                return Err(error(
                    ErrorCode::InvalidContract,
                    "原价时间与来源会话不一致",
                ));
            }
        } else if mark.price_time_ns >= self.sessions[0].before_open_ns {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "当前原价缺少已接受的会话映射",
            ));
        }
        crate::types::keys::label(&mark.source, 256)?;
        // A pre-ex raw observation cannot restore a mark invalidated by ex.
        for id in self
            .state
            .security_actions
            .get(&security)
            .into_iter()
            .flatten()
        {
            let action = &self.state.actions[id];
            if !action.ex_applied {
                continue;
            }
            if mark.price_time_ns < self.action_time(&action.action.ex)? {
                return Err(error(
                    ErrorCode::InvalidContract,
                    "除权前原价不能恢复除权后估值",
                ));
            }
        }
        if self
            .state
            .marks
            .get(&security)
            .is_some_and(|old| mark.price_time_ns < old.price_time_ns)
        {
            return Err(error(ErrorCode::InvalidContract, "原价观察时间不能倒退"));
        }
        if !self.state.marks.contains_key(&security)
            && self.state.marks.len() >= self.limits.max_positions
        {
            return Err(error(ErrorCode::ResourceLimit, "原价估值窗口超过预算"));
        }
        self.bump()?;
        self.state.as_of = now;
        self.state.marks.insert(security, mark);
        Ok(())
    }
    /// D03 ExecutionPort::mark forwards completed raw MarketEvents here.
    /// QuoteTick has no last trade: neither midpoint nor either side is invented.
    pub fn mark(&mut self, event: &MarketEvent) -> QfResult<()> {
        event.validate()?;
        let (security, session, time, price) = match event {
            MarketEvent::Bar(b) => (&b.security, &b.session, b.interval_end_ns, Some(b.close)),
            MarketEvent::TradeTick(t) => (&t.security, &t.session, t.time_ns, Some(t.price)),
            MarketEvent::QuoteTick(q) => (&q.security, &q.session, q.time_ns, None),
        };
        if session != &self.sessions[self.state.current].session.key {
            return Err(error(ErrorCode::InvalidContract, "原价行情不是当前会话"));
        }
        let Some(price) = price else {
            return Ok(());
        };
        let origin = match &self.usage {
            RuleUse::Market => RuleOrigin::Official("D04-completed-raw-market-event".into()),
            RuleUse::Synthetic(name) => RuleOrigin::Synthetic(name.clone()),
        };
        self.set_raw_mark(
            security.clone(),
            ValuationMark {
                price,
                price_time_ns: time,
                known_ns: time,
                session: session.clone(),
                basis: PriceBasis::Raw,
                source: "completed raw MarketEvent; no research adjustment".into(),
                origin,
            },
            time,
        )
    }
    pub fn valuation(&self, now: Nanoseconds) -> QfResult<ValuedAccount> {
        if now < self.state.as_of {
            return Err(error(
                ErrorCode::LookaheadForbidden,
                "账户只提供当前有界视图，不提供历史状态回放",
            ));
        }
        let row = &self.sessions[self.state.current];
        if now > row.after_close_ns {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "账户视图越过当前会话，必须先结算",
            ));
        }
        let mut positions = BTreeMap::new();
        let mut valuation = BTreeMap::new();
        let mut total = Some(self.state.cash.checked_add(self.state.receivables)?);
        for (security, p) in &self.state.positions {
            let boundary_missing = self
                .state
                .security_actions
                .get(security)
                .into_iter()
                .flatten()
                .try_fold(false, |missing, id| {
                    let a = &self.state.actions[id];
                    Ok::<_, crate::QfError>(
                        missing
                            || !a.ex_applied && self.action_time(&a.action.ex)? <= now
                            || !a.registered && self.action_time(&a.action.record)? < now,
                    )
                })?;
            let mark = self.state.marks.get(security);
            if mark.is_some_and(|m| m.price_time_ns > now || m.known_ns > now) {
                return Err(error(
                    ErrorCode::LookaheadForbidden,
                    "请求账户视图早于已知价格边界",
                ));
            }
            let halted = self.state.terms.get(security).is_some_and(|t| {
                t.facts.session == row.session.key && t.facts.status == TradingStatus::Halted
            });
            let reason = if boundary_missing {
                ValuationReason::CorporateActionBoundaryMissing
            } else if mark.is_some_and(|m| m.session == row.session.key) {
                ValuationReason::CurrentRaw
            } else if mark.is_some() && now <= row.session.open_ns {
                ValuationReason::PriorCloseBeforeOpen
            } else if mark.is_some() && halted {
                ValuationReason::KnownHaltLastRaw
            } else {
                ValuationReason::UnknownMissingPrice
            };
            let usable = matches!(
                reason,
                ValuationReason::CurrentRaw
                    | ValuationReason::PriorCloseBeforeOpen
                    | ValuationReason::KnownHaltLastRaw
            );
            let value = if usable {
                Some(amount(mark.expect("usable mark").price, p.quantity)?)
            } else {
                None
            };
            total = match (total, value) {
                (Some(a), Some(b)) => Some(a.checked_add(b)?),
                _ => None,
            };
            let average = p.total_cost.div_rounded(
                Money::from_integer(p.quantity.get()),
                crate::types::numeric::COST_SCALE,
                RoundingPolicy::HalfEven,
            )?;
            positions.insert(
                security.clone(),
                PositionView {
                    security: security.clone(),
                    quantity: p.quantity,
                    sellable: p.sellable(self.state.current)?,
                    frozen: p.frozen,
                    cost_basis_total: p.total_cost,
                    average_cost: average,
                    market_value: value,
                },
            );
            valuation.insert(
                security.clone(),
                PositionValuation {
                    market_value: value,
                    price_time_ns: mark.map(|m| m.price_time_ns),
                    is_stale: usable && mark.is_some_and(|m| m.price_time_ns < now),
                    reason,
                },
            );
        }
        Ok(ValuedAccount {
            account: AccountView {
                cash: self.state.cash,
                available_cash: self.available_cash()?,
                frozen_cash: self.frozen_cash()?,
                receivables: self.state.receivables,
                total_value: total,
                positions,
            },
            valuation,
        })
    }
    /// Bounded day-end view. Does not liquidate terminal holdings or retain a
    /// history of snapshots; callers stream selected curves to ResultSink.
    pub fn close_view(&self, session: &SessionKey) -> QfResult<ValuedAccount> {
        let row = &self.sessions[self.index(session)?];
        if row.session.key != self.sessions[self.state.current].session.key {
            return Err(error(ErrorCode::InvalidContract, "日终视图不是当前会话"));
        }
        self.valuation(row.session.close_ns)
    }
    pub fn terminal_view(&self, now: Nanoseconds) -> QfResult<AccountView> {
        self.value(now)
    }
}
