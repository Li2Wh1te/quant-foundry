//! Registration, ex, payment and sellability are separate accepted boundaries.
use super::ledger::{Lot, Position, TaxAttachment, error};
use super::{Account, AccountPort, PriceBasis, ValuationMark};
use crate::rules::date::RuleDate;
use crate::rules::dividends::{DividendTaxModel, HoldingBand, dividend_tax_model, holding_band};
use crate::rules::fees::InvestorKind;
use crate::rules::market::RuleOrigin;
use crate::types::{
    ExactDecimal as D, Money, Nanoseconds, Price, Quantity, RoundingPolicy, SecurityKey, SessionKey,
};
use crate::{ErrorCode, QfError, QfResult};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActionPhase {
    BeforeOpen,
    SessionClose,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActionBoundary {
    pub session: SessionKey,
    pub phase: ActionPhase,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntitlementRule {
    AllHeld,
    SettledOnly,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CashSettlement {
    pub scale: u32,
    pub rounding: RoundingPolicy,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CashDividendTax {
    /// Resolve D02's actual investor/product/registration-date policy.
    DatedStock {
        investor: InvestorKind,
        restricted_stock: bool,
    },
    /// Available only within a named synthetic RuleUse namespace.
    SyntheticFlat { rate: D },
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ShareRatio {
    pub numerator: u32,
    pub denominator: u32,
}
impl ShareRatio {
    fn validate(self) -> QfResult<()> {
        if self.numerator == 0 || self.denominator == 0 {
            return Err(error(ErrorCode::RuleUnavailable, "股份比例条款无效"));
        }
        Ok(())
    }
    fn apply(self, quantity: Quantity) -> QfResult<Quantity> {
        self.validate()?;
        let product = (quantity.get() as u128)
            .checked_mul(u128::from(self.numerator))
            .ok_or_else(|| error(ErrorCode::NumericRangeUnsupported, "股份比例溢出"))?;
        if product % u128::from(self.denominator) != 0 {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "存在零碎股份，缺少分配/补偿事实，不能无声舍去",
            ));
        }
        let shares = product / u128::from(self.denominator);
        Quantity::new(
            i64::try_from(shares)
                .map_err(|_| error(ErrorCode::NumericRangeUnsupported, "公司行动数量溢出"))?,
        )
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShareAcquisitionDate {
    InheritRecordLots,
    ExDate,
}
/// Investor-scoped accepted cash-tax fact for share changes. Missing is not
/// zero. D02's cash-dividend rule is not reused as an invented bonus-share tax.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShareTaxFact {
    pub investor: InvestorKind,
    pub per_entitled_share: Money,
    pub settlement: CashSettlement,
    pub basis: String,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CorporateActionKind {
    CashDividend {
        per_share: Money,
        payment: ActionBoundary,
        tax: CashDividendTax,
        settlement: CashSettlement,
    },
    ShareDistribution {
        additional_ratio: ShareRatio,
        sellable_session: SessionKey,
        acquisition: ShareAcquisitionDate,
        tax: Option<ShareTaxFact>,
    },
    Split {
        ratio: ShareRatio,
        sellable_session: SessionKey,
        tax: Option<ShareTaxFact>,
    },
    RightsIssue {
        offered_ratio: ShareRatio,
        subscription_price: Price,
        tradable_rights: Option<bool>,
        lapse_compensation: Option<Money>,
    },
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CorporateAction {
    pub action_id: String,
    pub security: SecurityKey,
    pub public_ns: Nanoseconds,
    pub record: ActionBoundary,
    pub ex: ActionBoundary,
    /// Accepted order for multiple same-boundary actions, not file iteration.
    pub sequence: u64,
    pub entitlement: EntitlementRule,
    pub kind: CorporateActionKind,
    pub ex_raw_mark: Option<ValuationMark>,
    pub origin: RuleOrigin,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CorporateEffectKind {
    Registered,
    ExDividend,
    PaidDividend,
    SharesChanged,
    RightsNotParticipated,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CorporateEffect {
    pub action_id: String,
    pub security: SecurityKey,
    pub kind: CorporateEffectKind,
    pub quantity: Quantity,
    pub cash_delta: Money,
    pub receivable_delta: Money,
    pub tax: Money,
    pub policy: String,
}
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CorporateActionReport {
    /// D06 stages cancellation transitions/notifications with this account
    /// transaction; no order is silently removed from its lifecycle store.
    pub cancelled_order_ids: Vec<String>,
    pub effects: Vec<CorporateEffect>,
}
#[derive(Debug, Clone)]
pub(super) struct RecordedLot {
    acquired: RuleDate,
    quantity: Quantity,
}
#[derive(Debug, Clone)]
pub(super) enum TaxModel {
    Dated(DividendTaxModel),
    Flat(D),
}
impl TaxModel {
    fn initial_rate(&self) -> D {
        match self {
            Self::Dated(m) => m.initial_withholding_rate,
            Self::Flat(r) => *r,
        }
    }
    fn liability(
        &self,
        gross: Money,
        band: HoldingBand,
        settlement: CashSettlement,
    ) -> QfResult<Money> {
        match self {
            Self::Dated(m) => m.final_liability(gross, band, settlement.scale, settlement.rounding),
            Self::Flat(r) => gross.mul_rounded(*r, settlement.scale, settlement.rounding),
        }
    }
}
#[derive(Debug, Clone)]
pub(super) struct ActionState {
    pub action: CorporateAction,
    pub registered: bool,
    pub ex_applied: bool,
    pub paid: bool,
    quantity: Quantity,
    recorded_lots: Vec<RecordedLot>,
    gross: Money,
    initial_tax: Money,
    net: Money,
    tax_model: Option<TaxModel>,
    gross_disposed: [Money; 3],
    initial_disposed: Money,
    additional_paid: Money,
}
impl ActionState {
    pub fn new(action: CorporateAction) -> Self {
        Self {
            action,
            registered: false,
            ex_applied: false,
            paid: false,
            quantity: Quantity::ZERO,
            recorded_lots: vec![],
            gross: Money::ZERO,
            initial_tax: Money::ZERO,
            net: Money::ZERO,
            tax_model: None,
            gross_disposed: [Money::ZERO; 3],
            initial_disposed: Money::ZERO,
            additional_paid: Money::ZERO,
        }
    }
    fn effect(
        &self,
        kind: CorporateEffectKind,
        cash: Money,
        receivable: Money,
        tax: Money,
        policy: &str,
    ) -> CorporateEffect {
        CorporateEffect {
            action_id: self.action.action_id.clone(),
            security: self.action.security.clone(),
            kind,
            quantity: self.quantity,
            cash_delta: cash,
            receivable_delta: receivable,
            tax,
            policy: policy.into(),
        }
    }
}
fn settlement_valid(settlement: CashSettlement) -> QfResult<()> {
    if settlement.scale > 28 {
        return Err(error(
            ErrorCode::RuleUnavailable,
            "公司行动现金精度未提供或超限",
        ));
    }
    Ok(())
}
fn action_error(action: &CorporateAction, mut e: QfError) -> QfError {
    e.operation = "corporate_action".into();
    e.scope
        .insert("security".into(), action.security.as_str().into());
    e.scope.insert("action_id".into(), action.action_id.clone());
    e
}
fn validate_share_tax(tax: &Option<ShareTaxFact>) -> QfResult<()> {
    let t = tax.as_ref().ok_or_else(|| {
        error(
            ErrorCode::RuleUnavailable,
            "股份行动税费事实缺失，不默认免税",
        )
    })?;
    settlement_valid(t.settlement)?;
    crate::types::keys::label(&t.basis, 256)?;
    if t.per_entitled_share.is_negative()
        || matches!(t.investor, InvestorKind::Other | InvestorKind::Unknown)
    {
        return Err(error(
            ErrorCode::RuleUnavailable,
            "股份行动税费金额/投资者不适用",
        ));
    }
    Ok(())
}
impl Account {
    pub(super) fn action_time(&self, boundary: &ActionBoundary) -> QfResult<Nanoseconds> {
        let row = &self.sessions[self.index(&boundary.session)?];
        Ok(match boundary.phase {
            ActionPhase::BeforeOpen => row.before_open_ns,
            ActionPhase::SessionClose => row.session.close_ns,
        })
    }
    pub(super) fn validate_action(&self, a: &CorporateAction, now: Nanoseconds) -> QfResult<()> {
        crate::types::keys::label(&a.action_id, 128)?;
        if now < self.state.as_of {
            return Err(error(
                ErrorCode::InvalidContract,
                "公司行动准入时间早于账户状态",
            ));
        }
        let record = self.action_time(&a.record)?;
        let ex = self.action_time(&a.ex)?;
        if a.public_ns > now {
            return Err(error(ErrorCode::LookaheadForbidden, "公司行动尚未公开"));
        }
        if !a.origin.permits(&self.usage) {
            return Err(error(ErrorCode::RuleUnavailable, "公司行动模型来源不适用"));
        }
        a.origin.validate()?;
        if a.public_ns > record
            || record < now
            || a.record.phase != ActionPhase::SessionClose
            || a.ex.phase != ActionPhase::BeforeOpen
            || ex <= record
        {
            return Err(error(
                ErrorCode::RuleUnavailable,
                "公司行动登记/除权边界缺失、错序或已错过",
            ));
        }
        if self.state.actions.values().any(|old| {
            old.action.security == a.security
                && old.action.ex == a.ex
                && old.action.sequence == a.sequence
        }) {
            return Err(error(
                ErrorCode::InvalidContract,
                "同一除权边界的公司行动序号冲突",
            ));
        }
        if let Some(m) = &a.ex_raw_mark {
            m.origin.validate()?;
            if m.basis != PriceBasis::Raw
                || m.price_time_ns != ex
                || m.price_time_ns > m.known_ns
                || m.known_ns > ex
                || m.session != a.ex.session
                || !m.origin.permits(&self.usage)
            {
                return Err(error(
                    ErrorCode::RuleUnavailable,
                    "除权原价依据不是有效当期raw价格",
                ));
            }
            crate::types::keys::label(&m.source, 256)?;
        }
        match &a.kind {
            CorporateActionKind::CashDividend {
                per_share,
                payment,
                tax,
                settlement,
            } => {
                settlement_valid(*settlement)?;
                if per_share.is_negative() || self.action_time(payment)? < ex {
                    return Err(error(ErrorCode::RuleUnavailable, "分红金额/支付边界无效"));
                }
                if let CashDividendTax::SyntheticFlat { rate } = tax
                    && (rate.is_negative()
                        || *rate > D::ONE
                        || !matches!(&a.origin, RuleOrigin::Synthetic(_)))
                {
                    return Err(error(
                        ErrorCode::RuleUnavailable,
                        "合成分红税不得作为正式税费事实",
                    ));
                }
            }
            CorporateActionKind::ShareDistribution {
                additional_ratio,
                sellable_session,
                tax,
                ..
            }
            | CorporateActionKind::Split {
                ratio: additional_ratio,
                sellable_session,
                tax,
            } => {
                additional_ratio.validate()?;
                validate_share_tax(tax)?;
                if self.index(sellable_session)? < self.index(&a.ex.session)? {
                    return Err(error(
                        ErrorCode::RuleUnavailable,
                        "新增/变更股份可卖日早于除权日",
                    ));
                }
            }
            CorporateActionKind::RightsIssue {
                offered_ratio,
                tradable_rights,
                lapse_compensation,
                ..
            } => {
                offered_ratio.validate()?;
                if *tradable_rights != Some(false) || *lapse_compensation != Some(Money::ZERO) {
                    return Err(error(
                        ErrorCode::RuleUnavailable,
                        "配股可交易权利/放弃补偿条款未知或本模型不支持",
                    ));
                }
            }
        }
        Ok(())
    }
    /// Called at exact D03 before_open/session_end boundaries. A batch previews
    /// all effects first; overflow or a missing fact leaves every stage, cash,
    /// position, fee accumulator and affected reservation unchanged.
    pub fn advance_corporate_actions(
        &mut self,
        boundary: &ActionBoundary,
    ) -> QfResult<CorporateActionReport> {
        self.transact(|candidate| candidate.advance_actions_inner(boundary))
    }
    fn advance_actions_inner(
        &mut self,
        boundary: &ActionBoundary,
    ) -> QfResult<CorporateActionReport> {
        if self.index(&boundary.session)? != self.state.current {
            return Err(error(
                ErrorCode::InvalidContract,
                "公司行动不是当前会话边界",
            ));
        }
        let now = self.action_time(boundary)?;
        if self
            .state
            .last_action_boundary
            .is_some_and(|last| now < last)
        {
            return Err(error(ErrorCode::InvalidContract, "公司行动时钟不能倒退"));
        }
        if now < self.state.as_of {
            return Err(error(
                ErrorCode::InvalidContract,
                "公司行动边界早于账户状态",
            ));
        }
        let mut report = CorporateActionReport::default();
        let mut ordered: Vec<_> = self
            .state
            .actions
            .values()
            .map(|a| (a.action.sequence, a.action.action_id.clone()))
            .collect();
        ordered.sort();
        for (_, id) in ordered {
            let mut a = self.state.actions[&id].clone();
            let record_time = self.action_time(&a.action.record)?;
            let ex_time = self.action_time(&a.action.ex)?;
            if !a.registered && record_time < now {
                return Err(error(
                    ErrorCode::RuleUnavailable,
                    "登记日边界漏处理，不能事后推算持仓",
                ));
            }
            if !a.ex_applied && ex_time < now {
                return Err(error(
                    ErrorCode::RuleUnavailable,
                    "除权边界漏处理，不能给完整收益",
                ));
            }
            if !a.registered && record_time == now {
                self.register_action(&mut a)
                    .map_err(|e| action_error(&a.action, e))?;
                report.effects.push(a.effect(
                    CorporateEffectKind::Registered,
                    Money::ZERO,
                    Money::ZERO,
                    Money::ZERO,
                    "record-date holdings frozen once",
                ));
            }
            if !a.ex_applied && ex_time == now {
                if !a.registered {
                    return Err(error(ErrorCode::RuleUnavailable, "除权前未登记权益"));
                }
                // Both GTC and next-session DAY estimates can be invalidated.
                // Cancel conservatively rather than fabricating adjusted queue
                // priority, prices or quantities for any affected remainder.
                let ids: Vec<_> = self
                    .state
                    .reservations
                    .values()
                    .filter(|r| r.request.security == a.action.security)
                    .map(|r| r.request.order_id.clone())
                    .collect();
                for id in ids {
                    self.release(&id)?;
                    report.cancelled_order_ids.push(id);
                }
                self.state.marks.remove(&a.action.security);
                self.ex_action(&mut a, &mut report)
                    .map_err(|e| action_error(&a.action, e))?;
                a.ex_applied = true;
                if let Some(mark) = &a.action.ex_raw_mark {
                    self.set_raw_mark(a.action.security.clone(), mark.clone(), now)?;
                }
            }
            if let CorporateActionKind::CashDividend { payment, .. } = &a.action.kind {
                let pay_time = self.action_time(payment)?;
                if !a.paid && pay_time < now {
                    return Err(error(
                        ErrorCode::RuleUnavailable,
                        "支付边界漏处理，拒绝跳过事件",
                    ));
                }
                if !a.paid && pay_time == now {
                    if !a.ex_applied {
                        return Err(error(ErrorCode::RuleUnavailable, "支付前尚未确认除权应收"));
                    }
                    let cash = self.state.cash.checked_add(a.net)?;
                    let receivables = self.state.receivables.checked_sub(a.net)?;
                    report.effects.push(a.effect(
                        CorporateEffectKind::PaidDividend,
                        a.net,
                        Money::ZERO.checked_sub(a.net)?,
                        Money::ZERO,
                        "receivable to cash; no second income",
                    ));
                    self.state.cash = cash;
                    self.state.receivables = receivables;
                    a.paid = true;
                }
            }
            self.state.actions.insert(id, a);
        }
        self.state.last_action_boundary = Some(now);
        self.state.as_of = now;
        Ok(report)
    }
    fn register_action(&mut self, a: &mut ActionState) -> QfResult<()> {
        let original = self
            .state
            .positions
            .get(&a.action.security)
            .cloned()
            .unwrap_or_default();
        let index = self.index(&a.action.record.session)?;
        a.recorded_lots = original
            .lots
            .iter()
            .filter(|l| {
                a.action.entitlement == EntitlementRule::AllHeld || l.sellable_from <= index
            })
            .map(|l| RecordedLot {
                acquired: l.acquired.clone(),
                quantity: l.quantity,
            })
            .collect();
        let stored_lots: usize = self
            .state
            .actions
            .values()
            .map(|old| old.recorded_lots.len())
            .sum();
        if a.recorded_lots.len() > self.limits.max_tax_attachments.saturating_sub(stored_lots) {
            return Err(error(ErrorCode::ResourceLimit, "登记权益份额超过预算"));
        }
        a.quantity = a
            .recorded_lots
            .iter()
            .try_fold(Quantity::ZERO, |n, l| n.checked_add(l.quantity))?;
        if let CorporateActionKind::CashDividend {
            per_share,
            tax,
            settlement,
            ..
        } = &a.action.kind
        {
            let terms = self.terms(&a.action.security)?;
            let model = match tax {
                CashDividendTax::DatedStock {
                    investor,
                    restricted_stock,
                } => {
                    if *investor != terms.buy_fees.scope().investor {
                        return Err(error(
                            ErrorCode::RuleUnavailable,
                            "分红税投资者与账户不一致",
                        ));
                    }
                    TaxModel::Dated(dividend_tax_model(
                        &terms.instrument,
                        *investor,
                        &self.sessions[index].date,
                        *restricted_stock,
                    )?)
                }
                CashDividendTax::SyntheticFlat { rate } => TaxModel::Flat(*rate),
            };
            a.gross = per_share.mul_rounded(
                D::from_integer(a.quantity.get()),
                settlement.scale,
                settlement.rounding,
            )?;
            a.initial_tax =
                a.gross
                    .mul_rounded(model.initial_rate(), settlement.scale, settlement.rounding)?;
            a.net = a.gross.checked_sub(a.initial_tax)?;
            a.tax_model = Some(model);
            if a.quantity != Quantity::ZERO {
                let mut position = original;
                let mut remaining = a.quantity;
                let mut gross = a.gross;
                let mut initial = a.initial_tax;
                for lot in position.lots.iter_mut().filter(|l| {
                    a.action.entitlement == EntitlementRule::AllHeld || l.sellable_from <= index
                }) {
                    let (lot_gross, rest_gross) = D::release_cost(gross, remaining, lot.quantity)?;
                    let (lot_initial, rest_initial) =
                        D::release_cost(initial, remaining, lot.quantity)?;
                    lot.taxes.push(TaxAttachment {
                        action_id: a.action.action_id.clone(),
                        quantity: lot.quantity,
                        gross: lot_gross,
                        initial_tax: lot_initial,
                    });
                    remaining = remaining.checked_sub(lot.quantity)?;
                    gross = rest_gross;
                    initial = rest_initial;
                }
                self.check_position_limits(&a.action.security, &position)?;
                self.put_position(a.action.security.clone(), position);
            }
            a.recorded_lots.clear();
        }
        a.registered = true;
        Ok(())
    }
    fn ex_action(
        &mut self,
        a: &mut ActionState,
        report: &mut CorporateActionReport,
    ) -> QfResult<()> {
        match &a.action.kind {
            CorporateActionKind::CashDividend { .. } => {
                self.state.receivables = self.state.receivables.checked_add(a.net)?;
                self.state.totals.dividend_income =
                    self.state.totals.dividend_income.checked_add(a.gross)?;
                self.state.totals.dividend_tax =
                    self.state.totals.dividend_tax.checked_add(a.initial_tax)?;
                self.state.totals.realized_pnl =
                    self.state.totals.realized_pnl.checked_add(a.net)?;
                report.effects.push(a.effect(
                    CorporateEffectKind::ExDividend,
                    Money::ZERO,
                    a.net,
                    a.initial_tax,
                    "earned net receivable; raw price only",
                ));
            }
            CorporateActionKind::ShareDistribution {
                additional_ratio,
                sellable_session,
                acquisition,
                tax,
            } => {
                let terms = self.terms(&a.action.security)?;
                let tax = tax.as_ref().expect("validated tax fact");
                if tax.investor != terms.buy_fees.scope().investor {
                    return Err(error(
                        ErrorCode::RuleUnavailable,
                        "股份分配税投资者与账户不一致",
                    ));
                }
                let tax_amount = tax.per_entitled_share.mul_rounded(
                    D::from_integer(a.quantity.get()),
                    tax.settlement.scale,
                    tax.settlement.rounding,
                )?;
                self.debit_share_tax(tax_amount)?;
                let mut position = self
                    .state
                    .positions
                    .get(&a.action.security)
                    .cloned()
                    .unwrap_or_default();
                let available = self.index(sellable_session)?;
                let mut added = Quantity::ZERO;
                for lot in &a.recorded_lots {
                    let shares = additional_ratio.apply(lot.quantity)?;
                    if shares == Quantity::ZERO {
                        continue;
                    }
                    added = added.checked_add(shares)?;
                    position.lots.push(Lot {
                        acquired: match acquisition {
                            ShareAcquisitionDate::InheritRecordLots => lot.acquired.clone(),
                            ShareAcquisitionDate::ExDate => {
                                self.sessions[self.state.current].date.clone()
                            }
                        },
                        quantity: shares,
                        sellable_from: available,
                        taxes: vec![],
                    });
                }
                position.quantity = position.quantity.checked_add(added)?;
                position.lots.sort_by(|a, b| a.acquired.cmp(&b.acquired));
                // Total moving cost is unchanged: granted shares never create
                // external cash or a cost recomputed from rounded average.
                self.check_position_limits(&a.action.security, &position)?;
                self.put_position(a.action.security.clone(), position);
                let mut effect = a.effect(
                    CorporateEffectKind::SharesChanged,
                    Money::ZERO.checked_sub(tax_amount)?,
                    Money::ZERO,
                    tax_amount,
                    "record entitlement; separately scheduled sellability",
                );
                effect.quantity = added;
                report.effects.push(effect);
                a.recorded_lots.clear();
            }
            CorporateActionKind::Split {
                ratio,
                sellable_session,
                tax,
            } => {
                let terms = self.terms(&a.action.security)?;
                let tax = tax.as_ref().expect("validated tax fact");
                if tax.investor != terms.buy_fees.scope().investor {
                    return Err(error(
                        ErrorCode::RuleUnavailable,
                        "拆并股份税投资者与账户不一致",
                    ));
                }
                let tax_amount = tax.per_entitled_share.mul_rounded(
                    D::from_integer(a.quantity.get()),
                    tax.settlement.scale,
                    tax.settlement.rounding,
                )?;
                self.debit_share_tax(tax_amount)?;
                let mut position = self
                    .state
                    .positions
                    .get(&a.action.security)
                    .cloned()
                    .unwrap_or_default();
                let available = self.index(sellable_session)?;
                let new_quantity = ratio.apply(position.quantity)?;
                for lot in &mut position.lots {
                    lot.quantity = ratio.apply(lot.quantity)?;
                    lot.sellable_from = lot.sellable_from.max(available);
                    // Previously registered cash entitlement remains the same
                    // total after a split; only its share denominator changes.
                    for claim in &mut lot.taxes {
                        claim.quantity = ratio.apply(claim.quantity)?;
                    }
                }
                position.quantity = new_quantity;
                self.check_position_limits(&a.action.security, &position)?;
                self.put_position(a.action.security.clone(), position);
                let mut effect = a.effect(
                    CorporateEffectKind::SharesChanged,
                    Money::ZERO.checked_sub(tax_amount)?,
                    Money::ZERO,
                    tax_amount,
                    "cost total preserved; exact integral split/consolidation",
                );
                effect.quantity = new_quantity;
                report.effects.push(effect);
                a.recorded_lots.clear();
            }
            CorporateActionKind::RightsIssue { .. } => {
                report.effects.push(a.effect(CorporateEffectKind::RightsNotParticipated, Money::ZERO, Money::ZERO, Money::ZERO, "do not subscribe; no external funds; accepted nontradable rights lapse without compensation"));
                a.recorded_lots.clear();
            }
        }
        Ok(())
    }
    fn debit_share_tax(&mut self, tax: Money) -> QfResult<()> {
        if tax > self.available_cash()? {
            return Err(error(
                ErrorCode::InsufficientCash,
                "股份行动税款不足，不追加外部资金",
            ));
        }
        self.state.cash = self.state.cash.checked_sub(tax)?;
        self.state.totals.dividend_tax = self.state.totals.dividend_tax.checked_add(tax)?;
        self.state.totals.realized_pnl = self.state.totals.realized_pnl.checked_sub(tax)?;
        Ok(())
    }
    /// Pure FIFO disposal preview, including retained registration entitlement
    /// after the holder sold before ex/payment. No cost is read from averages.
    pub(super) fn dispose(
        &self,
        original: &Position,
        quantity: Quantity,
        terms: &super::AccountTerms,
    ) -> QfResult<(Position, Money, BTreeMap<String, ActionState>)> {
        if quantity > original.eligible(self.state.current)? {
            return Err(error(
                ErrorCode::InsufficientSellable,
                "成交超过已结算/已上市可卖份额",
            ));
        }
        let mut position = original.clone();
        let mut remaining = quantity;
        let mut changes: BTreeMap<String, ActionState> = BTreeMap::new();
        for lot in position
            .lots
            .iter_mut()
            .filter(|l| l.sellable_from <= self.state.current)
        {
            if remaining == Quantity::ZERO {
                break;
            }
            let taken = lot.quantity.min(remaining);
            for claim in &mut lot.taxes {
                if claim.quantity != lot.quantity {
                    return Err(error(
                        ErrorCode::InvalidContract,
                        "税务权益份额与FIFO份额不一致",
                    ));
                }
                let (gross, gross_left) = D::release_cost(claim.gross, claim.quantity, taken)?;
                let (initial, initial_left) =
                    D::release_cost(claim.initial_tax, claim.quantity, taken)?;
                let action = if let Some(a) = changes.get_mut(&claim.action_id) {
                    a
                } else {
                    let a = self
                        .state
                        .actions
                        .get(&claim.action_id)
                        .ok_or_else(|| error(ErrorCode::InvalidContract, "税务权益缺失公司行动"))?
                        .clone();
                    changes.entry(claim.action_id.clone()).or_insert(a)
                };
                let model = action
                    .tax_model
                    .as_ref()
                    .ok_or_else(|| error(ErrorCode::InvalidContract, "现金权益缺少税务模型"))?;
                let band = if matches!(model, TaxModel::Dated(_)) {
                    let disposal_date =
                        terms.disposal_settlement_date.as_ref().ok_or_else(|| {
                            error(ErrorCode::RuleUnavailable, "分红税缺少实际转让交收日期")
                        })?;
                    if disposal_date < &terms.facts.date {
                        return Err(error(ErrorCode::RuleUnavailable, "转让交收日期早于成交日"));
                    }
                    match holding_band(&lot.acquired, disposal_date) {
                        Ok(band) => {
                            if terms
                                .holding_bands
                                .get(&lot.acquired)
                                .is_some_and(|provided| *provided != band)
                            {
                                return Err(error(
                                    ErrorCode::RuleUnavailable,
                                    "持有期事实与D02自然月规则冲突",
                                ));
                            }
                            band
                        }
                        Err(e) => terms
                            .holding_bands
                            .get(&lot.acquired)
                            .copied()
                            .filter(|b| *b != HoldingBand::Unknown)
                            .ok_or(e)?,
                    }
                } else {
                    HoldingBand::UpToOneMonth
                };
                let index = match band {
                    HoldingBand::UpToOneMonth => 0,
                    HoldingBand::OverMonthUpToYear => 1,
                    HoldingBand::OverOneYear => 2,
                    HoldingBand::Unknown => {
                        return Err(error(ErrorCode::RuleUnavailable, "分红税持有期未知"));
                    }
                };
                action.gross_disposed[index] = action.gross_disposed[index].checked_add(gross)?;
                action.initial_disposed = action.initial_disposed.checked_add(initial)?;
                claim.quantity = claim.quantity.checked_sub(taken)?;
                claim.gross = gross_left;
                claim.initial_tax = initial_left;
            }
            lot.quantity = lot.quantity.checked_sub(taken)?;
            lot.taxes.retain(|c| c.quantity != Quantity::ZERO);
            remaining = remaining.checked_sub(taken)?;
        }
        position.lots.retain(|l| l.quantity != Quantity::ZERO);
        let mut adjustment = Money::ZERO;
        for (id, action) in &mut changes {
            let CorporateActionKind::CashDividend { settlement, .. } = action.action.kind else {
                return Err(error(ErrorCode::InvalidContract, "非现金分红产生税务扣缴"));
            };
            let model = action.tax_model.as_ref().expect("registered model");
            let mut liability = Money::ZERO;
            for (gross, band) in action.gross_disposed.iter().zip([
                HoldingBand::UpToOneMonth,
                HoldingBand::OverMonthUpToYear,
                HoldingBand::OverOneYear,
            ]) {
                liability = liability.checked_add(model.liability(*gross, band, settlement)?)?;
            }
            let due = liability.checked_sub(action.initial_disposed)?;
            let old = self
                .state
                .actions
                .get(id)
                .expect("existing claim")
                .additional_paid;
            adjustment = adjustment.checked_add(due.checked_sub(old)?)?;
            action.additional_paid = due;
        }
        Ok((position, adjustment, changes))
    }
}
