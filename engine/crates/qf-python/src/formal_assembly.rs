//! Compose existing D02/D04/D09 contracts; no producer, inferred fact or rates.
use crate::runtime::spec::Fact;
use qf_core::accounting::{AccountTerms, SaleProceedsRule, SaleProceedsTiming};
use qf_core::orders::Side;
use qf_core::rules::{
    CostOverrides,
    catalog::{verified_fee_catalog, verified_market_rules},
    date::{EffectiveRange, RuleDate},
    dividends::HoldingBand,
    fees::{CommissionConfig, FeeScope, InvestorKind},
    market::{Instrument, RiskState, RuleUse, TradeDayFacts, TradePermissions},
};
use qf_core::types::Price;
use qf_core::{ErrorCode, QfError, QfResult};
use std::collections::BTreeMap;

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    instrument: Instrument,
    day: TradeDayFacts,
    permissions: TradePermissions,
    submission_reference: Option<Price>,
    investor: InvestorKind,
    sale_proceeds: String,
    sale_proceeds_basis: String,
    disposal_settlement_date: Option<RuleDate>,
    holding_bands: BTreeMap<RuleDate, HoldingBand>,
    buy_commission: CommissionConfig,
    sell_commission: CommissionConfig,
    #[serde(default)]
    cost_overrides: CostOverrides,
}

pub fn compose(input: &str) -> QfResult<String> {
    let input: Input = qf_core::data::views::bounded_json(input)?;
    let book = verified_market_rules()?;
    let usage = RuleUse::Market;
    let resolved = book.resolve(&input.instrument, &input.day, &usage)?;
    let missing = input.permissions.buy.is_none()
        || input.permissions.sell.is_none()
        || input.permissions.product_buy.is_none()
        || input.day.risk != RiskState::Normal && input.permissions.risk_disclosure.is_none()
        || input.day.risk == RiskState::Delisting && input.permissions.delisting_buy.is_none()
        || input.day.risk == RiskState::RiskWarning
            && resolved.rule().risk_buy_cap.is_some()
            && input.permissions.risk_bought_or_open.is_none();
    if missing {
        return Err(missing_fact(&input, "permissions", "适用交易权限事实未知"));
    }
    // A known false permission stays false; orders enforce it in the original
    // RuleBook. Initial cash or account metadata cannot confer permission.
    let origin = resolved.rule().origin.clone();
    let range = EffectiveRange {
        from: input.day.date.clone(),
        through: input.day.date.clone(),
    };
    let catalog = verified_fee_catalog()?;
    let fees = |side, commission: &CommissionConfig| {
        catalog.compose(
            FeeScope {
                instrument: input.instrument.clone(),
                side,
                investor: input.investor,
                origin: origin.clone(),
            },
            &range,
            commission,
            &input.cost_overrides,
            &usage,
        )
    };
    let buy = fees(Side::Buy, &input.buy_commission)?;
    let sell = fees(Side::Sell, &input.sell_commission)?;
    let timing = match input.sale_proceeds.as_str() {
        "immediate" => SaleProceedsTiming::Immediate,
        "next_session" => SaleProceedsTiming::NextSession,
        _ => {
            return Err(missing_fact(&input, "sale_proceeds", "资金可用时点未知"));
        }
    };
    AccountTerms::resolve(
        input.instrument.clone(),
        &book,
        input.day.clone(),
        buy.clone(),
        sell.clone(),
        SaleProceedsRule {
            timing,
            basis: input.sale_proceeds_basis.clone(),
            origin: origin.clone(),
        },
        usage,
    )?;
    let result = Fact {
        instrument: input.instrument,
        day: input.day,
        permissions: input.permissions,
        submission_reference: input.submission_reference,
        buy_fees: buy.config().clone(),
        sell_fees: sell.config().clone(),
        investor: input.investor,
        origin,
        sale_proceeds: input.sale_proceeds,
        sale_proceeds_basis: input.sale_proceeds_basis,
        disposal_settlement_date: input.disposal_settlement_date,
        holding_bands: input.holding_bands,
    };
    let output = serde_json::to_string(&result).map_err(|_| {
        QfError::new(
            ErrorCode::InvalidContract,
            "formal_assembly",
            "会话事实序列化失败",
        )
    })?;
    if output.len() > qf_core::run::MAX_CONTROL_BYTES {
        return Err(QfError::new(
            ErrorCode::ResourceLimit,
            "formal_assembly",
            "会话事实超过控制消息预算",
        ));
    }
    Ok(output)
}

fn missing_fact(input: &Input, field: &str, message: &str) -> QfError {
    let mut error = QfError::new(ErrorCode::RuleUnavailable, "formal_assembly", message);
    error
        .scope
        .insert("security".into(), input.instrument.security.as_str().into());
    error
        .scope
        .insert("date".into(), input.day.date.to_string());
    error.scope.insert("field".into(), field.into());
    error
}
