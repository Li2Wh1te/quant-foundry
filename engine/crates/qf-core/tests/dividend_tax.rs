use qf_core::ErrorCode;
use qf_core::rules::date::RuleDate;
use qf_core::rules::dividends::*;
use qf_core::rules::fees::InvestorKind;
use qf_core::rules::market::{Exchange, Instrument, Product};
use qf_core::types::{ExactDecimal as D, RoundingPolicy, SecurityKey};
fn date(value: &str) -> RuleDate {
    value.parse().unwrap()
}
fn d(value: &str) -> D {
    value.parse().unwrap()
}
fn sh_stock() -> Instrument {
    Instrument {
        security: SecurityKey::new("dividend-fixture").unwrap(),
        exchange: Exchange::Shanghai,
        product: Product::MainBoardStock,
    }
}

#[test]
fn investor_record_date_and_holding_band_select_original_2013_and_2015_models() {
    let inst = sh_stock();
    let old = dividend_tax_model(
        &inst,
        InvestorKind::ResidentIndividual,
        &date("2015-09-07"),
        false,
    )
    .unwrap();
    let new = dividend_tax_model(
        &inst,
        InvestorKind::ResidentIndividual,
        &date("2015-09-09"),
        false,
    )
    .unwrap();
    assert_eq!(old.timing, DividendTaxTiming::PaymentWithDisposalAdjustment);
    assert_eq!(old.initial_withholding_rate, d("0.05"));
    assert!(old.fifo_required);
    assert_eq!(new.timing, DividendTaxTiming::DeferredUntilDisposal);
    assert_eq!(new.initial_withholding_rate, D::ZERO);
    assert!(new.fifo_required);
    for (band, before, after) in [
        (HoldingBand::UpToOneMonth, "20", "20"),
        (HoldingBand::OverMonthUpToYear, "10", "10"),
        (HoldingBand::OverOneYear, "5", "0"),
    ] {
        assert_eq!(
            old.final_liability(d("100"), band, 2, RoundingPolicy::HalfEven)
                .unwrap(),
            d(before)
        );
        assert_eq!(
            new.final_liability(d("100"), band, 2, RoundingPolicy::HalfEven)
                .unwrap(),
            d(after)
        );
    }
    assert_eq!(
        new.rate(HoldingBand::Unknown).unwrap_err().code,
        ErrorCode::RuleUnavailable
    );
    for value in ["2013-01-01", "2015-09-08", "2026-10-11"] {
        assert_eq!(
            dividend_tax_model(&inst, InvestorKind::ResidentIndividual, &date(value), false)
                .unwrap_err()
                .code,
            ErrorCode::RuleUnavailable
        );
    }
}

#[test]
fn natural_month_year_boundaries_use_transfer_settlement_not_trading_day_counts() {
    let acquired = date("2025-01-10");
    for (transfer, expected) in [
        ("2025-02-10", HoldingBand::UpToOneMonth),
        ("2025-02-11", HoldingBand::OverMonthUpToYear),
        ("2026-01-10", HoldingBand::OverMonthUpToYear),
        ("2026-01-11", HoldingBand::OverOneYear),
    ] {
        assert_eq!(holding_band(&acquired, &date(transfer)).unwrap(), expected);
    }
    assert_eq!(
        holding_band(&acquired, &date("2025-01-09"))
            .unwrap_err()
            .code,
        ErrorCode::RuleUnavailable
    );
    // The original supplies natural months; no made-up 30-day/month-end rule.
    assert_eq!(
        holding_band(&date("2025-01-31"), &date("2025-02-28"))
            .unwrap_err()
            .code,
        ErrorCode::RuleUnavailable
    );
    assert_eq!(
        holding_band(&date("2025-01-31"), &date("2026-02-01")).unwrap(),
        HoldingBand::OverOneYear
    );
}

#[test]
fn unsupported_investors_etf_beijing_and_restricted_shares_are_not_tax_free() {
    let inst = sh_stock();
    let current = date("2026-07-07");
    for investor in [
        InvestorKind::ResidentEnterprise,
        InvestorKind::Other,
        InvestorKind::Unknown,
    ] {
        assert_eq!(
            dividend_tax_model(&inst, investor, &current, false)
                .unwrap_err()
                .code,
            ErrorCode::RuleUnavailable
        );
    }
    assert_eq!(
        dividend_tax_model(&inst, InvestorKind::ResidentIndividual, &current, true)
            .unwrap_err()
            .code,
        ErrorCode::RuleUnavailable
    );
    for (exchange, product) in [
        (Exchange::Shanghai, Product::EquityEtf),
        (Exchange::Beijing, Product::BeijingStock),
    ] {
        let other = Instrument {
            exchange,
            product,
            ..inst.clone()
        };
        assert_eq!(
            dividend_tax_model(&other, InvestorKind::ResidentIndividual, &current, false)
                .unwrap_err()
                .code,
            ErrorCode::RuleUnavailable
        );
    }
}
