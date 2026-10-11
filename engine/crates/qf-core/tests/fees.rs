use qf_core::orders::Side;
use qf_core::rules::catalog::{FeeCatalog, FeeRuleRecord, verified_fee_catalog};
use qf_core::rules::date::{EffectiveRange, RuleDate};
use qf_core::rules::fees::*;
use qf_core::rules::market::{Exchange, Instrument, Product, RuleOrigin, RuleUse};
use qf_core::rules::{CostOverrides, DatedFeeComponent, FeeComponentKind as K, FeeConfig, FeeFact};
use qf_core::types::{ExactDecimal as D, RoundingPolicy as R, SecurityKey};
use qf_core::{ErrorCode, QfResult};

fn d(v: &str) -> D {
    v.parse().unwrap()
}
fn date(v: &str) -> RuleDate {
    v.parse().unwrap()
}
fn range(from: &str, through: &str) -> EffectiveRange {
    EffectiveRange {
        from: date(from),
        through: date(through),
    }
}
fn scope(exchange: Exchange, product: Product, side: Side, origin: RuleOrigin) -> FeeScope {
    FeeScope {
        instrument: Instrument {
            security: SecurityKey::new("fee-fixture").unwrap(),
            exchange,
            product,
        },
        side,
        investor: InvestorKind::ResidentIndividual,
        origin,
    }
}
fn commission(rate: &str, minimum: &str) -> CommissionConfig {
    CommissionConfig {
        commission_rate: d(rate),
        minimum_commission: d(minimum),
        currency: qf_core::types::market::Currency::CNY,
        settlement_scale: 2,
        rounding: R::HalfEven,
        included_components: vec![],
        basis: "selected account commission agreement (test input)".into(),
    }
}
fn fixture(
    rate: &str,
    minimum: &str,
    components: Vec<DatedFeeComponent>,
    rounding: R,
) -> (FeeScope, ScopedFeeConfig) {
    let scope = scope(
        Exchange::Shanghai,
        Product::MainBoardStock,
        Side::Sell,
        RuleOrigin::Synthetic("fee-oracle".into()),
    );
    let config = FeeConfig {
        commission_rate: d(rate),
        minimum_commission: d(minimum),
        currency: qf_core::types::market::Currency::CNY,
        settlement_scale: 2,
        rounding,
        components,
        synthetic_model: Some("fee-oracle".into()),
    };
    let fees = ScopedFeeConfig::new(
        scope.clone(),
        config,
        &RuleUse::Synthetic("fee-oracle".into()),
    )
    .unwrap();
    (scope, fees)
}
fn component(kind: K, from: &str, through: &str, rate: &str, included: bool) -> DatedFeeComponent {
    DatedFeeComponent {
        kind,
        effective_from: from.into(),
        effective_through: through.into(),
        included_in_commission: included,
        fact: FeeFact::Applicable {
            rate: d(rate),
            basis: "synthetic fee oracle; not an official tariff".into(),
        },
    }
}
fn code<T: std::fmt::Debug>(result: QfResult<T>, expected: ErrorCode) {
    assert_eq!(result.unwrap_err().code, expected);
}
fn applicable_rate(fact: &FeeFact) -> D {
    match fact {
        FeeFact::Applicable { rate, .. } => *rate,
        FeeFact::Inapplicable { .. } => panic!("expected applicable tariff"),
    }
}

#[test]
fn official_tax_and_fee_changes_apply_to_direction_market_product_and_date() {
    let catalog = verified_fee_catalog().unwrap();
    for (exchange, product, old_transfer, old_handling, handling) in [
        (
            Exchange::Shanghai,
            Product::MainBoardStock,
            "0.00002",
            "0.0000487",
            "0.0000341",
        ),
        (
            Exchange::Shenzhen,
            Product::ChiNextStock,
            "0.00002",
            "0.0000487",
            "0.0000341",
        ),
        (
            Exchange::Beijing,
            Product::BeijingStock,
            "0.000025",
            "0.00025",
            "0.000125",
        ),
    ] {
        let mut s = scope(
            exchange,
            product,
            Side::Sell,
            RuleOrigin::Official("verified-fee-catalog".into()),
        );
        for (value, rate) in [("2022-04-28", old_transfer), ("2022-04-29", "0.00001")] {
            assert_eq!(
                applicable_rate(
                    &catalog
                        .fact(&s, K::TransferFee, &date(value), &RuleUse::Market)
                        .unwrap()
                        .fact
                ),
                d(rate)
            );
        }
        for (value, stamp, handling) in [
            ("2023-08-27", "0.001", old_handling),
            ("2023-08-28", "0.0005", handling),
        ] {
            assert_eq!(
                applicable_rate(
                    &catalog
                        .fact(&s, K::StampDuty, &date(value), &RuleUse::Market)
                        .unwrap()
                        .fact
                ),
                d(stamp)
            );
            assert_eq!(
                applicable_rate(
                    &catalog
                        .fact(&s, K::HandlingFee, &date(value), &RuleUse::Market)
                        .unwrap()
                        .fact
                ),
                d(handling)
            );
        }
        s.side = Side::Buy;
        assert!(matches!(
            catalog
                .fact(&s, K::StampDuty, &date("2023-08-28"), &RuleUse::Market)
                .unwrap()
                .fact,
            FeeFact::Inapplicable { .. }
        ));
        code(
            catalog.fact(
                &s,
                K::TransferFee,
                &date(if exchange == Exchange::Beijing {
                    "2022-04-27"
                } else {
                    "2015-07-31"
                }),
                &RuleUse::Market,
            ),
            ErrorCode::RuleUnavailable,
        );
    }
    for product in [
        Product::EquityEtf,
        Product::BondEtf,
        Product::MoneyEtf,
        Product::GoldEtf,
        Product::CommodityEtf,
        Product::CrossBorderEtf,
    ] {
        let s = scope(
            Exchange::Shanghai,
            product,
            Side::Sell,
            RuleOrigin::Official("verified-fee-catalog".into()),
        );
        assert!(matches!(
            catalog
                .fact(&s, K::StampDuty, &date("2023-08-28"), &RuleUse::Market)
                .unwrap()
                .fact,
            FeeFact::Inapplicable { .. }
        ));
        let rate = if matches!(product, Product::BondEtf | Product::MoneyEtf) {
            "0"
        } else {
            "0.00004"
        };
        // The July 19, 2021 adjustment and maintained exemptions establish
        // intervals. The current tariff inspection date is not commencement.
        for exchange in [Exchange::Shanghai, Exchange::Shenzhen] {
            let mut s = s.clone();
            s.instrument.exchange = exchange;
            for value in ["2021-07-19", "2023-04-10", "2026-07-07", "2026-10-10"] {
                assert_eq!(
                    applicable_rate(
                        &catalog
                            .fact(&s, K::HandlingFee, &date(value), &RuleUse::Market)
                            .unwrap()
                            .fact
                    ),
                    d(rate)
                );
            }
            let previous_rate = if matches!(product, Product::BondEtf | Product::MoneyEtf) {
                "0"
            } else if exchange == Exchange::Shanghai {
                "0.000045"
            } else {
                "0.0000487"
            };
            assert_eq!(
                applicable_rate(
                    &catalog
                        .fact(&s, K::HandlingFee, &date("2021-07-18"), &RuleUse::Market)
                        .unwrap()
                        .fact
                ),
                d(previous_rate)
            );
        }
    }
}

#[test]
fn official_stock_composition_covers_trading_dates_and_keeps_dated_tariffs() {
    let catalog = verified_fee_catalog().unwrap();
    let commission = commission("0.0003", "5");
    for (exchange, product) in [
        (Exchange::Shanghai, Product::MainBoardStock),
        (Exchange::Shanghai, Product::StarStock),
        (Exchange::Shenzhen, Product::MainBoardStock),
        (Exchange::Shenzhen, Product::ChiNextStock),
    ] {
        let s = scope(
            exchange,
            product,
            Side::Sell,
            RuleOrigin::Official("verified-fee-catalog".into()),
        );
        let fees = catalog
            .compose(
                s.clone(),
                &range("2022-07-01", "2026-10-10"),
                &commission,
                &CostOverrides::default(),
                &RuleUse::Market,
            )
            .unwrap();
        assert!(fees.config().synthetic_model.is_none());
        // Independent per-order liabilities on opposite sides of the statutory
        // change: 5 + 10 + .10 + .20 + .49; then 5 + 5 + .10 + .20 + .34.
        for (value, total) in [
            ("2023-04-10", "15.79"),
            ("2023-08-25", "15.79"),
            ("2023-08-28", "10.64"),
            ("2026-07-07", "10.64"),
        ] {
            let mut accumulator =
                OrderFeeAccumulator::new("dated-tariff-example", fees.clone()).unwrap();
            assert_eq!(
                accumulator
                    .apply_fill(&s, &date(value), d("10000"))
                    .unwrap()
                    .total,
                d(total)
            );
        }
    }
}

#[test]
fn official_secondary_etf_and_beijing_applicability_has_evidenced_intervals() {
    let catalog = verified_fee_catalog().unwrap();
    for (exchange, products) in [
        (
            Exchange::Shanghai,
            vec![
                Product::EquityEtf,
                Product::BondEtf,
                Product::MoneyEtf,
                Product::GoldEtf,
                Product::CommodityEtf,
                Product::CrossBorderEtf,
            ],
        ),
        (
            Exchange::Shenzhen,
            vec![
                Product::EquityEtf,
                Product::BondEtf,
                Product::MoneyEtf,
                Product::GoldEtf,
                Product::CommodityEtf,
                Product::CrossBorderEtf,
            ],
        ),
        (Exchange::Beijing, vec![Product::BeijingStock]),
    ] {
        for product in products {
            for side in [Side::Buy, Side::Sell] {
                let mut s = scope(
                    exchange,
                    product,
                    side,
                    RuleOrigin::Official("verified-fee-catalog".into()),
                );
                for investor in [
                    InvestorKind::ResidentIndividual,
                    InvestorKind::ResidentEnterprise,
                ] {
                    s.investor = investor;
                    let fees = catalog
                        .compose(
                            s.clone(),
                            &range("2025-05-01", "2026-10-10"),
                            &commission("0.0003", "5"),
                            &CostOverrides::default(),
                            &RuleUse::Market,
                        )
                        .unwrap();
                    assert_eq!(fees.config().components.len(), 4);
                    let mut accumulator =
                        OrderFeeAccumulator::new("applicable-tariff-example", fees).unwrap();
                    let charge = accumulator
                        .apply_fill(&s, &date("2026-07-07"), d("10000"))
                        .unwrap();
                    let total = if product == Product::BeijingStock {
                        if side == Side::Sell { "11.35" } else { "6.35" }
                    } else if matches!(product, Product::BondEtf | Product::MoneyEtf) {
                        "5"
                    } else {
                        "5.40"
                    };
                    assert_eq!(charge.total, d(total));
                    assert!(matches!(
                        catalog
                            .fact(&s, K::RegulatoryFee, &date("2026-07-07"), &RuleUse::Market)
                            .unwrap()
                            .fact,
                        FeeFact::Inapplicable { .. }
                    ));
                }
            }
        }
    }
    let s = scope(
        Exchange::Shanghai,
        Product::EquityEtf,
        Side::Sell,
        RuleOrigin::Official("verified-fee-catalog".into()),
    );
    catalog
        .compose(
            s,
            &range("2023-11-24", "2026-10-10"),
            &commission("0.0003", "5"),
            &CostOverrides::default(),
            &RuleUse::Market,
        )
        .unwrap();
}

#[test]
fn market_composition_still_refuses_unknown_coverage_even_if_commission_includes_it() {
    let catalog = verified_fee_catalog().unwrap();
    for (exchange, product, missing_date) in [
        (Exchange::Shanghai, Product::MainBoardStock, "2022-06-30"),
        (Exchange::Shenzhen, Product::MainBoardStock, "2022-06-30"),
        (Exchange::Beijing, Product::BeijingStock, "2025-04-30"),
        (Exchange::Shanghai, Product::EquityEtf, "2023-11-23"),
        (Exchange::Shenzhen, Product::BondEtf, "2025-04-30"),
    ] {
        let s = scope(
            exchange,
            product,
            Side::Sell,
            RuleOrigin::Official("verified-fee-catalog".into()),
        );
        let mut all_included = commission("0.0003", "5");
        all_included.included_components = REQUIRED_COMPONENTS.to_vec();
        let error = catalog
            .compose(
                s,
                &range(missing_date, "2026-10-10"),
                &all_included,
                &CostOverrides::default(),
                &RuleUse::Market,
            )
            .unwrap_err();
        assert_eq!(error.code, ErrorCode::RuleUnavailable);
        assert_eq!(error.operation, "fee_catalog");
        assert!(error.scope.contains_key("field"));
        assert!(error.scope.contains_key("date"));
    }
    let s = scope(
        Exchange::Shanghai,
        Product::EquityEtf,
        Side::Buy,
        RuleOrigin::Official("verified-fee-catalog".into()),
    );
    code(
        catalog.fact(&s, K::TransferFee, &date("2026-10-11"), &RuleUse::Market),
        ErrorCode::RuleUnavailable,
    );
}

#[test]
fn minimum_commission_is_cumulative_per_order_and_preview_is_non_mutating() {
    let (scope, fees) = fixture("0.01", "5", vec![], R::HalfEven);
    let mut accumulator = OrderFeeAccumulator::new("partial-order", fees.clone()).unwrap();
    let (preview, _) = accumulator
        .preview_fill(&scope, &date("2026-07-07"), d("100"))
        .unwrap();
    assert_eq!(preview.total, d("5"));
    assert_eq!(accumulator.total_paid(), D::ZERO);
    for (amount, expected) in [("100", "5"), ("100", "0"), ("600", "3")] {
        assert_eq!(
            accumulator
                .apply_fill(&scope, &date("2026-07-07"), d(amount))
                .unwrap()
                .commission,
            d(expected)
        );
    }
    let mut single = OrderFeeAccumulator::new("one-fill", fees).unwrap();
    assert_eq!(
        single
            .apply_fill(&scope, &date("2026-07-07"), d("800"))
            .unwrap()
            .total,
        accumulator.total_paid()
    );
    let mut wrong = scope.clone();
    wrong.side = Side::Buy;
    code(
        accumulator.apply_fill(&wrong, &date("2026-07-07"), d("100")),
        ErrorCode::RuleUnavailable,
    );
    code(
        accumulator.apply_fill(&scope, &date("2026-07-06"), d("100")),
        ErrorCode::RuleUnavailable,
    );
    code(
        accumulator.apply_fill(&scope, &date("2026-07-07"), D::ZERO),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(accumulator.total_paid(), d("8"));
}

#[test]
fn overflow_keeps_the_order_state_and_error_scope() {
    let (s, fees) = fixture("0", "0", vec![], R::HalfEven);
    let mut a = OrderFeeAccumulator::new("overflow-order", fees).unwrap();
    let maximum = d("79228162514264337593543950335");
    a.apply_fill(&s, &date("2026-07-07"), maximum).unwrap();
    let error = a.apply_fill(&s, &date("2026-07-07"), D::ONE).unwrap_err();
    assert_eq!(error.code, ErrorCode::NumericRangeUnsupported);
    assert_eq!(error.scope["order_id"], "overflow-order");
    assert_eq!(error.scope["security"], "fee-fixture");
    assert_eq!(a.cumulative_notional(), maximum);
    assert_eq!(a.order_id(), "overflow-order");
}

#[test]
fn component_rounding_is_cumulative_and_settlement_rounding_is_explicit() {
    for (rounding, first) in [(R::HalfEven, "0.00"), (R::HalfUp, "0.01")] {
        let (s, fees) = fixture(
            "0",
            "0",
            vec![component(
                K::HandlingFee,
                "2026-07-01",
                "2026-07-31",
                "0.005",
                false,
            )],
            rounding,
        );
        let mut a = OrderFeeAccumulator::new("rounding-order", fees).unwrap();
        assert_eq!(
            a.apply_fill(&s, &date("2026-07-07"), D::ONE).unwrap().total,
            d(first)
        );
        // .005 + .010 = .015; total liability is .02 under both modes.
        a.apply_fill(&s, &date("2026-07-07"), d("2")).unwrap();
        assert_eq!(a.total_paid(), d("0.02"));
    }
    for (input, expected) in [
        ("0.005", "0.01"),
        ("-0.005", "-0.01"),
        ("0.015", "0.02"),
        ("-0.015", "-0.02"),
    ] {
        assert_eq!(d(input).round(2, R::HalfUp).unwrap(), d(expected));
    }
    assert_eq!(d("0.001").round(2, R::AwayFromZero).unwrap(), d("0.01"));
    assert_eq!(d("-0.001").round(2, R::AwayFromZero).unwrap(), d("-0.01"));
}

#[test]
fn included_items_are_visible_and_not_charged_twice_but_underfunded_commission_refuses() {
    let components = vec![
        component(K::HandlingFee, "2026-07-01", "2026-07-31", "0.001", true),
        component(K::TransferFee, "2026-07-01", "2026-07-31", "0.002", true),
    ];
    let (s, fees) = fixture("0.005", "0", components.clone(), R::HalfEven);
    let mut a = OrderFeeAccumulator::new("included-order", fees).unwrap();
    let charge = a.apply_fill(&s, &date("2026-07-07"), d("1000")).unwrap();
    assert_eq!(charge.total, d("5"));
    assert_eq!(charge.components[0].cumulative_liability, d("1"));
    assert_eq!(charge.components[1].cumulative_liability, d("2"));
    assert!(
        charge
            .components
            .iter()
            .all(|c| c.charged == D::ZERO && c.included_in_commission)
    );
    let (s, fees) = fixture("0.001", "0", components, R::HalfEven);
    let mut a = OrderFeeAccumulator::new("underfunded", fees).unwrap();
    code(
        a.apply_fill(&s, &date("2026-07-07"), d("1000")),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(a.total_paid(), D::ZERO);
}

#[test]
fn gtc_dated_change_keeps_past_liability_and_order_minimum_without_rerating() {
    let components = vec![
        component(K::StampDuty, "2023-08-27", "2023-08-27", "0.001", false),
        component(K::StampDuty, "2023-08-28", "2023-08-28", "0.0005", false),
    ];
    let (s, fees) = fixture("0.001", "5", components, R::HalfEven);
    let mut a = OrderFeeAccumulator::new("dated-partials", fees).unwrap();
    assert_eq!(
        a.apply_fill(&s, &date("2023-08-27"), d("1000"))
            .unwrap()
            .total,
        d("6")
    );
    assert_eq!(
        a.apply_fill(&s, &date("2023-08-28"), d("1000"))
            .unwrap()
            .total,
        d("0.50")
    );
    assert_eq!(a.total_paid(), d("6.50"));
    code(
        a.apply_fill(&s, &date("2023-08-29"), D::ONE),
        ErrorCode::RuleUnavailable,
    );
    assert_eq!(a.total_paid(), d("6.50"));
}

#[test]
fn missing_duplicate_conflicting_and_synthetic_fee_configs_are_rejected() {
    let (s, fees) = fixture("0", "0", vec![], R::HalfEven);
    code(
        ScopedFeeConfig::new(s.clone(), fees.config().clone(), &RuleUse::Market),
        ErrorCode::RuleUnavailable,
    );
    code(
        ScopedFeeConfig::new(
            s.clone(),
            fees.config().clone(),
            &RuleUse::Synthetic("another-model".into()),
        ),
        ErrorCode::RuleUnavailable,
    );
    let mut official = s.clone();
    official.origin = RuleOrigin::Official("asserted-market".into());
    code(
        ScopedFeeConfig::new(official.clone(), fees.config().clone(), &RuleUse::Market),
        ErrorCode::RuleUnavailable,
    );
    let mut missing = fees.config().clone();
    missing.synthetic_model = None;
    missing.components = vec![component(
        K::StampDuty,
        "2026-07-01",
        "2026-07-31",
        "0.001",
        false,
    )];
    let scoped = ScopedFeeConfig::new(official, missing.clone(), &RuleUse::Market).unwrap();
    code(
        scoped.validate_date(&date("2026-07-07")),
        ErrorCode::RuleUnavailable,
    );
    missing.components.push(missing.components[0].clone());
    code(missing.validate(), ErrorCode::RuleUnavailable);
    let mut agreement = commission("0.0003", "5");
    agreement.included_components = vec![K::HandlingFee, K::HandlingFee];
    code(agreement.validate(), ErrorCode::RuleUnavailable);
    agreement = commission("0.0003", "0.005");
    code(agreement.validate(), ErrorCode::RuleUnavailable);
    let override_config = CostOverrides {
        commission_rate: Some(d("0.0002")),
        minimum_commission: Some(d("1")),
    };
    assert_eq!(
        commission("0.0003", "5")
            .with_overrides(&override_config)
            .unwrap()
            .minimum_commission,
        d("1")
    );
    assert!(serde_json::from_str::<CostOverrides>(r#"{"stamp_duty":"0"}"#).is_err());
}

#[test]
fn catalog_rejects_overlap_and_gaps_including_leap_month_boundaries() {
    let s = scope(
        Exchange::Shanghai,
        Product::MainBoardStock,
        Side::Buy,
        RuleOrigin::Synthetic("leap-catalog".into()),
    );
    let record = |kind, from, through| FeeRuleRecord {
        exchange: s.instrument.exchange,
        product: s.instrument.product,
        side: s.side,
        investor: s.investor,
        kind,
        effective: range(from, through),
        origin: s.origin.clone(),
        fact: FeeFact::Inapplicable {
            basis: "synthetic explicit inapplicability".into(),
        },
    };
    let mut records = vec![];
    for kind in REQUIRED_COMPONENTS {
        records.push(record(kind, "2024-02-28", "2024-02-29"));
        records.push(record(kind, "2024-03-01", "2024-03-02"));
    }
    let catalog = FeeCatalog::new(records.clone()).unwrap();
    catalog
        .compose(
            s.clone(),
            &range("2024-02-28", "2024-03-02"),
            &commission("0", "0"),
            &CostOverrides::default(),
            &RuleUse::Synthetic("leap-catalog".into()),
        )
        .unwrap();
    records[0].effective.through = date("2024-02-28");
    code(
        FeeCatalog::new(records.clone()).unwrap().compose(
            s.clone(),
            &range("2024-02-28", "2024-03-02"),
            &commission("0", "0"),
            &CostOverrides::default(),
            &RuleUse::Synthetic("leap-catalog".into()),
        ),
        ErrorCode::RuleUnavailable,
    );
    records.push(records[1].clone());
    code(FeeCatalog::new(records), ErrorCode::RuleUnavailable);
    assert_eq!(date("2024-02-28").next_day().unwrap(), date("2024-02-29"));
    assert_eq!(date("2023-02-28").next_day().unwrap(), date("2023-03-01"));
    assert_eq!(date("2026-12-31").next_day().unwrap(), date("2027-01-01"));
    assert!("2023-02-29".parse::<RuleDate>().is_err());
    assert!("2026-7-6".parse::<RuleDate>().is_err());
}
