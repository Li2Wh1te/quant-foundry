use crate::types::{ExactDecimal, Money};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};

pub mod catalog;
pub mod date;
pub mod dividends;
pub mod fees;
pub mod market;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct CostOverrides {
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "crate::types::numeric::optional_nonnegative"
    )]
    pub commission_rate: Option<ExactDecimal>,
    #[serde(
        default,
        skip_serializing_if = "Option::is_none",
        deserialize_with = "crate::types::numeric::optional_nonnegative"
    )]
    pub minimum_commission: Option<Money>,
}
impl CostOverrides {
    pub fn validate(&self) -> QfResult<()> {
        if self
            .commission_rate
            .is_some_and(|v| v.is_negative() || v > ExactDecimal::ONE)
            || self
                .minimum_commission
                .is_some_and(ExactDecimal::is_negative)
        {
            return Err(QfError::new(
                ErrorCode::InvalidRunConfig,
                "cost_overrides",
                "佣金覆盖参数范围无效",
            ));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FeeComponentKind {
    StampDuty,
    TransferFee,
    RegulatoryFee,
    HandlingFee,
}
/// D02 supplies dated applicability/evidence. Explicit zero is allowed; missing
/// facts have no Default implementation and cannot silently become zero.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "applicability", rename_all = "snake_case", deny_unknown_fields)]
pub enum FeeFact {
    Applicable { rate: ExactDecimal, basis: String },
    Inapplicable { basis: String },
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DatedFeeComponent {
    pub kind: FeeComponentKind,
    pub effective_from: String,
    pub effective_through: String,
    pub included_in_commission: bool,
    pub fact: FeeFact,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeeConfig {
    pub commission_rate: ExactDecimal,
    pub minimum_commission: Money,
    pub currency: crate::types::market::Currency,
    pub settlement_scale: u32,
    pub rounding: crate::types::RoundingPolicy,
    pub components: Vec<DatedFeeComponent>,
    /// Named fixture models are distinguished from applicable dated fee facts.
    pub synthetic_model: Option<String>,
}
impl FeeConfig {
    pub fn validate(&self) -> QfResult<()> {
        let bad = || {
            QfError::new(
                ErrorCode::RuleUnavailable,
                "fee_config",
                "费用事实缺失、重复或适用范围无效",
            )
        };
        if self.commission_rate.is_negative()
            || self.commission_rate > ExactDecimal::ONE
            || self.minimum_commission.is_negative()
            || self.settlement_scale > 28
            || self.components.len() > 256
        {
            return Err(bad());
        }
        if let Some(name) = &self.synthetic_model {
            crate::types::keys::label(name, 128).map_err(|_| bad())?;
        }
        if self.components.is_empty() && self.synthetic_model.is_none() {
            return Err(bad());
        }
        for (index, component) in self.components.iter().enumerate() {
            if !crate::run::valid_date(&component.effective_from)
                || !crate::run::valid_date(&component.effective_through)
                || component.effective_from > component.effective_through
            {
                return Err(bad());
            }
            // The same component may change at a dated boundary. Overlapping
            // facts would allow double charging or ambiguous applicability.
            if self.components[..index].iter().any(|previous| {
                previous.kind == component.kind
                    && previous.effective_from <= component.effective_through
                    && component.effective_from <= previous.effective_through
            }) {
                return Err(bad());
            }
            let basis = match &component.fact {
                FeeFact::Applicable { rate, basis } => {
                    if rate.is_negative() || *rate > ExactDecimal::ONE {
                        return Err(bad());
                    }
                    basis
                }
                FeeFact::Inapplicable { basis } => basis,
            };
            crate::types::keys::label(basis, 256).map_err(|_| bad())?;
        }
        Ok(())
    }
}
