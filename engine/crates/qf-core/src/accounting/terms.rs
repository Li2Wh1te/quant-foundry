//! Current dated facts, selected by D02/D04. No inferred calendar, tax or fees.
use crate::orders::Side;
use crate::rules::FeeComponentKind;
use crate::rules::date::RuleDate;
use crate::rules::dividends::HoldingBand;
use crate::rules::fees::ScopedFeeConfig;
use crate::rules::market::{
    Instrument, ResolvedTradingRule, RuleBook, RuleOrigin, RuleUse, SellAvailability,
    TradeDayFacts, TradingRule,
};
use crate::types::{ExactDecimal, Money, Price, Quantity, RoundingPolicy};
use crate::{ErrorCode, QfError, QfResult};
use std::collections::BTreeMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct AcceptedCommission {
    rate: ExactDecimal,
    minimum: Money,
    scale: u32,
    rounding: RoundingPolicy,
    included: Vec<FeeComponentKind>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct AcceptedFees {
    investor: crate::rules::fees::InvestorKind,
    buy: AcceptedCommission,
    sell: AcceptedCommission,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SaleProceedsTiming {
    Immediate,
    NextSession,
}
/// D02 currently supplies security sellability, not sale-cash usability.
/// A provider must explicitly supply this fact; withdrawal is out of scope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SaleProceedsRule {
    pub timing: SaleProceedsTiming,
    pub basis: String,
    pub origin: RuleOrigin,
}

#[derive(Debug, Clone)]
pub struct AccountTerms {
    pub(crate) instrument: Instrument,
    pub(crate) facts: TradeDayFacts,
    book: RuleBook,
    usage: RuleUse,
    pub(crate) buy_fees: ScopedFeeConfig,
    pub(crate) sell_fees: ScopedFeeConfig,
    pub(crate) proceeds: SaleProceedsRule,
    /// Actual transfer settlement, not the fill date or a guessed next weekday.
    pub disposal_settlement_date: Option<RuleDate>,
    /// Accepted provider bands for ambiguous natural-month anniversaries only.
    pub holding_bands: BTreeMap<RuleDate, HoldingBand>,
}
impl AccountTerms {
    pub(super) fn agreement(&self) -> AcceptedFees {
        let commission = |fees: &ScopedFeeConfig| {
            let config = fees.config();
            let included: std::collections::BTreeSet<_> = config
                .components
                .iter()
                .filter(|c| c.included_in_commission)
                .map(|c| c.kind)
                .collect();
            AcceptedCommission {
                rate: config.commission_rate,
                minimum: config.minimum_commission,
                scale: config.settlement_scale,
                rounding: config.rounding,
                included: included.into_iter().collect(),
            }
        };
        AcceptedFees {
            investor: self.buy_fees.scope().investor,
            buy: commission(&self.buy_fees),
            sell: commission(&self.sell_fees),
        }
    }
    pub fn resolve(
        instrument: Instrument,
        book: &RuleBook,
        facts: TradeDayFacts,
        buy_fees: ScopedFeeConfig,
        sell_fees: ScopedFeeConfig,
        proceeds: SaleProceedsRule,
        usage: RuleUse,
    ) -> QfResult<Self> {
        let selected: TradingRule = book.resolve(&instrument, &facts, &usage)?.rule().clone();
        // Store one selected rule, rather than another full rule catalogue.
        let book = RuleBook::new(vec![selected])?;
        for (fees, side) in [(&buy_fees, Side::Buy), (&sell_fees, Side::Sell)] {
            let scope = fees.scope();
            if scope.instrument != instrument || scope.side != side || !scope.origin.permits(&usage)
            {
                return Err(instrument.error(
                    Some(&facts.date),
                    ErrorCode::RuleUnavailable,
                    "fee_scope",
                    "账户费用与标的、方向或模型不一致",
                ));
            }
            fees.validate_date(&facts.date)?;
        }
        if buy_fees.scope().investor != sell_fees.scope().investor
            || !proceeds.origin.permits(&usage)
            || crate::types::keys::label(&proceeds.basis, 256).is_err()
        {
            return Err(QfError::new(
                ErrorCode::RuleUnavailable,
                "account_terms",
                "资金可用或投资者事实缺失/冲突",
            ));
        }
        proceeds.origin.validate()?;
        Ok(Self {
            instrument,
            book,
            facts,
            usage,
            buy_fees,
            sell_fees,
            proceeds,
            disposal_settlement_date: None,
            holding_bands: BTreeMap::new(),
        })
    }
    pub(crate) fn resolved(&self) -> QfResult<ResolvedTradingRule<'_>> {
        self.book
            .resolve(&self.instrument, &self.facts, &self.usage)
    }
    pub(crate) fn fees(&self, side: Side) -> &ScopedFeeConfig {
        match side {
            Side::Buy => &self.buy_fees,
            Side::Sell => &self.sell_fees,
        }
    }
    pub(crate) fn sell_availability(&self) -> QfResult<SellAvailability> {
        self.resolved()?.sell_availability()
    }
    pub(crate) fn validate_quantity(
        &self,
        side: Side,
        quantity: Quantity,
        available: Quantity,
        market: bool,
    ) -> QfResult<()> {
        self.resolved()?
            .validate_quantity(side, quantity, available, market)
    }
    pub(crate) fn validate_price(&self, price: Price) -> QfResult<()> {
        if !self.resolved()?.rule().price_on_grid(price)? {
            return Err(self.instrument.error(
                Some(&self.facts.date),
                ErrorCode::InvalidOrder,
                "price",
                "价格不满足当日价格步长",
            ));
        }
        Ok(())
    }
    pub(crate) fn usage(&self) -> &RuleUse {
        &self.usage
    }
}
