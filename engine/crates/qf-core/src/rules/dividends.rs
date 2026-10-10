//! Investor/date-specific tax definition, not a dividend ledger. D09 owns FIFO
//! lots, entitlement/payment events and deduction at actual disposal settlement.
use super::catalog::VERIFIED_THROUGH;
use super::date::RuleDate;
use super::fees::InvestorKind;
use super::market::{Exchange, Instrument};
use crate::types::{ExactDecimal, Money, RoundingPolicy};
use crate::{ErrorCode, QfError, QfResult};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum HoldingBand {
    UpToOneMonth,
    OverMonthUpToYear,
    OverOneYear,
    Unknown,
}

/// Acquisition through the day before transfer settlement, measured in natural
/// months/years (财税2012 85). Calendar-month-end cases with no corresponding
/// anniversary require the provider's accepted holding band; no day-count guess.
pub fn holding_band(acquired: &RuleDate, transfer_settlement: &RuleDate) -> QfResult<HoldingBand> {
    if transfer_settlement < acquired {
        return Err(unavailable("holding_period", "卖出交收日期早于取得日期"));
    }
    // An ambiguous one-month anniversary must not reject an unambiguous
    // holding of more than a full year (e.g. acquisition on 31 January).
    if acquired
        .anniversary(12)
        .is_ok_and(|year| transfer_settlement > &year)
    {
        return Ok(HoldingBand::OverOneYear);
    }
    if transfer_settlement <= &acquired.anniversary(1)? {
        return Ok(HoldingBand::UpToOneMonth);
    }
    if transfer_settlement <= &acquired.anniversary(12)? {
        return Ok(HoldingBand::OverMonthUpToYear);
    }
    Ok(HoldingBand::OverOneYear)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DividendTaxTiming {
    /// 2013 model: 5% initial withholding, with an adjustment on disposal.
    PaymentWithDisposalAdjustment,
    /// 2015 model: <=one-year holdings are paid gross; determine final tax at
    /// disposal. >one-year holdings are explicitly exempt, not an unknown rate.
    DeferredUntilDisposal,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DividendTaxModel {
    pub basis: String,
    pub timing: DividendTaxTiming,
    pub long_holding_rate: ExactDecimal,
    pub initial_withholding_rate: ExactDecimal,
    pub fifo_required: bool,
}
impl DividendTaxModel {
    pub fn rate(&self, holding: HoldingBand) -> QfResult<ExactDecimal> {
        match holding {
            HoldingBand::UpToOneMonth => "0.20".parse(),
            HoldingBand::OverMonthUpToYear => "0.10".parse(),
            HoldingBand::OverOneYear => Ok(self.long_holding_rate),
            HoldingBand::Unknown => Err(unavailable(
                "holding_period",
                "分红税持有期未知，不可用零税率替代",
            )),
        }
    }
    /// Final tax liability only; this must not be deducted on payment and then
    /// again on disposal. Settlement precision is an explicit simulation choice.
    pub fn final_liability(
        &self,
        cash_dividend: Money,
        holding: HoldingBand,
        scale: u32,
        rounding: RoundingPolicy,
    ) -> QfResult<Money> {
        if cash_dividend.is_negative() {
            return Err(unavailable("cash_dividend", "现金分红金额不可为负"));
        }
        cash_dividend.mul_rounded(self.rate(holding)?, scale, rounding)
    }
}

pub fn dividend_tax_model(
    instrument: &Instrument,
    investor: InvestorKind,
    record_date: &RuleDate,
    restricted_stock: bool,
) -> QfResult<DividendTaxModel> {
    instrument.validate()?;
    // Neither ETF-holder distributions nor Beijing shares, corporate, foreign
    // or restricted-stock models are inferred from the public SH/SZ share rule.
    if investor != InvestorKind::ResidentIndividual
        || restricted_stock
        || !instrument.product.is_stock()
        || !matches!(instrument.exchange, Exchange::Shanghai | Exchange::Shenzhen)
        || record_date.as_str() <= "2013-01-01"
        || record_date.as_str() > VERIFIED_THROUGH
        || record_date.as_str() == "2015-09-08"
    {
        let mut error = instrument.error(
            Some(record_date),
            ErrorCode::RuleUnavailable,
            "dividend_tax",
            "投资者、股票范围或登记日分红税适用依据未核验",
        );
        error.operation = "dividend_tax".into();
        return Err(error);
    }
    // Both originals say registration date AFTER the effective date. The exact
    // 2015-09-08 registration boundary remains unavailable, never backfilled.
    if record_date.as_str() > "2015-09-08" {
        Ok(DividendTaxModel {
            basis: "dividend-tax-2015-101;dividend-tax-2012-85".into(),
            timing: DividendTaxTiming::DeferredUntilDisposal,
            long_holding_rate: ExactDecimal::ZERO,
            initial_withholding_rate: ExactDecimal::ZERO,
            fifo_required: true,
        })
    } else {
        Ok(DividendTaxModel {
            basis: "dividend-tax-2012-85".into(),
            timing: DividendTaxTiming::PaymentWithDisposalAdjustment,
            long_holding_rate: "0.05".parse()?,
            initial_withholding_rate: "0.05".parse()?,
            fifo_required: true,
        })
    }
}
fn unavailable(field: &str, message: &str) -> QfError {
    let mut error = QfError::new(ErrorCode::RuleUnavailable, "dividend_tax", message);
    error.scope.insert("field".into(), field.into());
    error
}
