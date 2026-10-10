//! One small immutable fee configuration per order and cumulative assessment.
//! No account CRUD, Python fee calculator, network lookup or execution ledger.
use super::date::RuleDate;
use super::market::{Instrument, RuleOrigin, RuleUse};
use super::{CostOverrides, FeeComponentKind, FeeConfig, FeeFact};
use crate::orders::Side;
use crate::types::{ExactDecimal, Money, RoundingPolicy};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const REQUIRED_COMPONENTS: [FeeComponentKind; 4] = [
    FeeComponentKind::StampDuty,
    FeeComponentKind::TransferFee,
    FeeComponentKind::RegulatoryFee,
    FeeComponentKind::HandlingFee,
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InvestorKind {
    ResidentIndividual,
    ResidentEnterprise,
    Other,
    Unknown,
}

/// Adapted from the selected saved account's commission agreement. Inclusion
/// is explicit even when empty; no assumption of net/all-inclusive commission.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommissionConfig {
    pub commission_rate: ExactDecimal,
    pub minimum_commission: Money,
    pub currency: crate::types::market::Currency,
    pub settlement_scale: u32,
    pub rounding: RoundingPolicy,
    pub included_components: Vec<FeeComponentKind>,
    pub basis: String,
}
impl CommissionConfig {
    pub fn validate(&self) -> QfResult<()> {
        let bad = || {
            QfError::new(
                ErrorCode::RuleUnavailable,
                "commission_config",
                "账户佣金缺失、重复或结算约定无效",
            )
        };
        if self.commission_rate.is_negative()
            || self.commission_rate > ExactDecimal::ONE
            || self.minimum_commission.is_negative()
            || self.settlement_scale > 28
            || self.included_components.len() > REQUIRED_COMPONENTS.len()
            || self
                .included_components
                .iter()
                .enumerate()
                .any(|(i, k)| self.included_components[..i].contains(k))
            || crate::types::keys::label(&self.basis, 256).is_err()
        {
            return Err(bad());
        }
        if self
            .minimum_commission
            .round(self.settlement_scale, self.rounding)?
            != self.minimum_commission
        {
            return Err(bad());
        }
        Ok(())
    }
    pub fn with_overrides(&self, overrides: &CostOverrides) -> QfResult<Self> {
        self.validate()?;
        overrides.validate()?;
        let mut result = self.clone();
        if let Some(rate) = overrides.commission_rate {
            result.commission_rate = rate;
        }
        if let Some(minimum) = overrides.minimum_commission {
            result.minimum_commission = minimum;
        }
        result.validate()?;
        Ok(result)
    }
}

/// FeeConfig has no symbol/side fields in D01. Bind it here so it cannot leak
/// across securities, markets, investor classes or directions at execution.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeeScope {
    pub instrument: Instrument,
    pub side: Side,
    pub investor: InvestorKind,
    pub origin: RuleOrigin,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopedFeeConfig {
    scope: FeeScope,
    config: FeeConfig,
}
impl ScopedFeeConfig {
    pub fn new(scope: FeeScope, config: FeeConfig, usage: &RuleUse) -> QfResult<Self> {
        scope.instrument.validate()?;
        scope.origin.validate()?;
        config.validate()?;
        let name = match &scope.origin {
            RuleOrigin::Official(_) => None,
            RuleOrigin::Synthetic(name) => Some(name),
        };
        if !scope.origin.permits(usage)
            || config.synthetic_model.as_ref() != name
            || matches!(scope.investor, InvestorKind::Other | InvestorKind::Unknown)
            || config
                .minimum_commission
                .round(config.settlement_scale, config.rounding)?
                != config.minimum_commission
        {
            return Err(fee_error(
                &scope,
                None,
                "scope",
                "费用来源、投资者或结算约定不适用",
            ));
        }
        Ok(Self { scope, config })
    }
    pub fn config(&self) -> &FeeConfig {
        &self.config
    }
    pub fn scope(&self) -> &FeeScope {
        &self.scope
    }
    /// Every component must explicitly apply or be inapplicable on this date,
    /// even if a user's commission includes it. A missing rate is never zero.
    pub fn validate_date(&self, date: &RuleDate) -> QfResult<()> {
        for kind in REQUIRED_COMPONENTS {
            let count = self
                .config
                .components
                .iter()
                .filter(|c| {
                    c.kind == kind
                        && c.effective_from.as_str() <= date.as_str()
                        && date.as_str() <= c.effective_through.as_str()
                })
                .count();
            let represented = self.config.components.iter().any(|c| c.kind == kind);
            if count != 1 && (self.config.synthetic_model.is_none() || represented) {
                return Err(fee_error(
                    &self.scope,
                    Some(date),
                    &format!("{kind:?}"),
                    "当日必需费用事实缺失或冲突",
                ));
            }
        }
        Ok(())
    }
}

fn fee_error(scope: &FeeScope, date: Option<&RuleDate>, field: &str, message: &str) -> QfError {
    let mut error = scope
        .instrument
        .error(date, ErrorCode::RuleUnavailable, field, message);
    error.operation = "fees".into();
    error.scope.insert(
        "side".into(),
        match scope.side {
            Side::Buy => "buy",
            Side::Sell => "sell",
        }
        .into(),
    );
    error
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeeComponentCharge {
    pub kind: FeeComponentKind,
    /// Total sourced component liability rounded under the explicit simulation
    /// settlement policy. Included items remain visible but are not added again.
    pub cumulative_liability: Money,
    pub charged: Money,
    pub included_in_commission: bool,
    pub basis: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeeCharge {
    pub commission: Money,
    pub components: Vec<FeeComponentCharge>,
    pub total: Money,
    pub cumulative_total: Money,
    pub settlement_scale: u32,
    pub rounding: RoundingPolicy,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ComponentAccrual {
    raw: Money,
    paid: Money,
    included: bool,
}

/// The accumulator belongs to one order. GTC fills retain it across dates and
/// apply each fill's dated rate, rather than re-rating all earlier notional.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderFeeAccumulator {
    order_id: String,
    fees: ScopedFeeConfig,
    last_date: Option<RuleDate>,
    notional: Money,
    commission_paid: Money,
    components: BTreeMap<FeeComponentKind, ComponentAccrual>,
    total_paid: Money,
}
impl OrderFeeAccumulator {
    pub fn new(order_id: &str, fees: ScopedFeeConfig) -> QfResult<Self> {
        crate::types::keys::label(order_id, 128)?;
        // Inclusion cannot change within an order. Changing the account config
        // creates a new accepted run/order, never an implicit mid-order reset.
        for c in &fees.config.components {
            if fees.config.components.iter().any(|other| {
                other.kind == c.kind && other.included_in_commission != c.included_in_commission
            }) {
                return Err(fee_error(
                    &fees.scope,
                    None,
                    "included_in_commission",
                    "同一订单费用包含项随日期冲突",
                ));
            }
        }
        Ok(Self {
            order_id: order_id.into(),
            fees,
            last_date: None,
            notional: Money::ZERO,
            commission_paid: Money::ZERO,
            components: BTreeMap::new(),
            total_paid: Money::ZERO,
        })
    }
    pub fn total_paid(&self) -> Money {
        self.total_paid
    }
    pub fn order_id(&self) -> &str {
        &self.order_id
    }
    pub fn cumulative_notional(&self) -> Money {
        self.notional
    }
    /// D09 first previews on a copy, validates cash and applies the fill, then
    /// adopts the returned next state. Errors leave the original untouched.
    pub fn preview_fill(
        &self,
        scope: &FeeScope,
        date: &RuleDate,
        notional: Money,
    ) -> QfResult<(FeeCharge, Self)> {
        let mut next = self.clone();
        let charge = next.accrue(scope, date, notional).map_err(|mut error| {
            error.scope.insert("order_id".into(), self.order_id.clone());
            error.scope.insert(
                "security".into(),
                self.fees.scope.instrument.security.as_str().into(),
            );
            error.scope.insert("date".into(), date.to_string());
            error
        })?;
        Ok((charge, next))
    }
    pub fn apply_fill(
        &mut self,
        scope: &FeeScope,
        date: &RuleDate,
        notional: Money,
    ) -> QfResult<FeeCharge> {
        let (charge, next) = self.preview_fill(scope, date, notional)?;
        *self = next;
        Ok(charge)
    }
    fn accrue(
        &mut self,
        scope: &FeeScope,
        date: &RuleDate,
        notional: Money,
    ) -> QfResult<FeeCharge> {
        if scope != &self.fees.scope
            || notional <= Money::ZERO
            || self.last_date.as_ref().is_some_and(|last| date < last)
        {
            return Err(fee_error(
                &self.fees.scope,
                Some(date),
                "fill",
                "成交标的、方向、日期或金额与订单费用范围冲突",
            ));
        }
        self.fees.validate_date(date)?;
        let config = &self.fees.config;
        self.notional = self.notional.checked_add(notional)?;
        let commission_due = self
            .notional
            .checked_mul(config.commission_rate)?
            .max(config.minimum_commission)
            .round(config.settlement_scale, config.rounding)?;
        let commission = commission_due.checked_sub(self.commission_paid)?;
        let mut total = commission;
        let mut included_due = Money::ZERO;
        let mut charges = Vec::new();
        for c in config.components.iter().filter(|c| {
            c.effective_from.as_str() <= date.as_str()
                && date.as_str() <= c.effective_through.as_str()
        }) {
            let (raw, basis) = match &c.fact {
                FeeFact::Applicable { rate, basis } => (notional.checked_mul(*rate)?, basis),
                FeeFact::Inapplicable { basis } => (Money::ZERO, basis),
            };
            let state = self.components.entry(c.kind).or_insert(ComponentAccrual {
                raw: Money::ZERO,
                paid: Money::ZERO,
                included: c.included_in_commission,
            });
            state.raw = state.raw.checked_add(raw)?;
            let due = state.raw.round(config.settlement_scale, config.rounding)?;
            let charged = if state.included {
                included_due = included_due.checked_add(due)?;
                Money::ZERO
            } else {
                due.checked_sub(state.paid)?
            };
            state.paid = due;
            total = total.checked_add(charged)?;
            charges.push(FeeComponentCharge {
                kind: c.kind,
                cumulative_liability: due,
                charged,
                included_in_commission: state.included,
                basis: basis.clone(),
            });
        }
        if included_due > commission_due {
            return Err(fee_error(
                &self.fees.scope,
                Some(date),
                "commission_rate",
                "账户佣金不足以覆盖声明已包含的法定费用",
            ));
        }
        self.commission_paid = commission_due;
        self.total_paid = self.total_paid.checked_add(total)?;
        self.last_date = Some(date.clone());
        Ok(FeeCharge {
            commission,
            components: charges,
            total,
            cumulative_total: self.total_paid,
            settlement_scale: config.settlement_scale,
            rounding: config.rounding,
        })
    }
}
